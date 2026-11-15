"""FastAPI app. Reads via read-only connections; writes only through finance.ledger / finance.importer."""

from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from types import SimpleNamespace
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode, urlparse

from markupsafe import Markup
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config import ALLOWED_HOSTS, PUBLIC_PORT, UI_HOST, UI_PORT
from finance import analytics
from finance.db import connect_ro
from finance import autosync as autosync_mod
from finance.jobs import JobManager
from finance import users as users_mod, vault
from finance.web import accounts, auth as auth_mod, pwa

HERE = Path(__file__).parent


def today() -> date:
    """Today's date, asked in one place so a test can pin it."""
    return date.today()


def inr(v, dp: int = 0) -> str:
    """Indian digit grouping: 12,34,567."""
    if v is None:
        return "—"
    d = Decimal(str(v))
    neg, d = d < 0, abs(d)
    whole, _, frac = f"{d:.{dp}f}".partition(".")
    if len(whole) > 3:
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", whole[:-3])
        whole = f"{head},{whole[-3:]}"
    return f"{'-' if neg else ''}₹{whole}{'.' + frac if frac else ''}"


def ts(value) -> Markup:
    """UTC timestamp from the DB; the browser rewrites it into local time (base.html)."""
    if not value:
        return Markup("—")
    iso = str(value).replace(" ", "T") + ("Z" if not str(value).endswith("Z") else "")
    return Markup('<time class="ts" datetime="{}">{} UTC</time>').format(iso, str(value))


MAX_FORM_BYTES = 64 * 1024                       # every form and JSON body except a statement upload
MAX_FILE_BYTES = 25 * 1024 * 1024                 # one statement PDF (real ones are well under 10 MB)
MAX_UPLOAD_BYTES = 200 * 1024 * 1024              # a whole upload (up to 25 files)
MAX_IN_FLIGHT = 6                                 # simultaneous requests per profile
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
ANONYMOUS_PATHS = {"/manifest.webmanifest", "/sw.js"}          # fetched by the browser without cookies; contain nothing private


def create_app(db_path: str = vault.VAULT_DB, jobs: JobManager | None = None, mail_config_path: str = vault.VAULT_FILES,
               imap_connect=None, users: users_mod.Users | None = None, registry: vault.Registry | None = None,
               extra_hosts: list[str] | None = None) -> FastAPI:
    """Every page except the sign-in pages needs a signed-in profile whose data is unlocked; the ledger, secrets and mailbox
    settings that a request reads and writes are that profile's own, sealed in its vault."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["inr"] = inr
    templates.env.filters["ts"] = ts
    templates.env.filters["ts_epoch"] = lambda v: ts(time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(int(v)))) if v else Markup("—")
    jobs = jobs or JobManager()
    users, registry = users or users_mod.Users(), registry or vault.Registry()
    throttle, challenges = auth_mod.Throttle(), accounts.Challenges()
    allowed_hosts = {f"{h}:{p}" for h in (UI_HOST, "localhost", "127.0.0.1") for p in (UI_PORT, PUBLIC_PORT)}
    allowed_hosts |= set(ALLOWED_HOSTS if extra_hosts is None else extra_hosts)       # the domain a hosted instance answers to
    pwa.register(app)
    in_flight: dict[str, threading.BoundedSemaphore] = {}              # requests running at once, per profile
    app.state.autosync = autosync_mod.AutoSync(registry, jobs)         # started by the server, never by create_app (tests call tick())

    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # pages anyone may open: signing in or up, recovering, and the files a browser fetches without cookies
    open_paths = {"/login", "/signup", "/recover", "/auth/passkey/login/options", "/auth/passkey/login/verify",
                  "/auth/link/options", "/auth/link/verify", "/logout"}
    # pages a signed-in person may use while their data is still locked
    locked_paths = {"/unlock", "/lock", "/logout"}

    def sign_in_target(request: Request) -> RedirectResponse | PlainTextResponse:
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse("/signup" if not users.has_users() else f"/login?{urlencode({'next': target})}", status_code=303)
        return PlainTextResponse("sign in required", status_code=401)

    async def check(request: Request, call_next):
        path = request.url.path
        if request.headers.get("host", "").lower() not in allowed_hosts:          # DNS-rebinding defence
            return PlainTextResponse("bad host", status_code=400)
        if request.method == "POST":                                    # cross-site form posts
            origin = request.headers.get("origin") or request.headers.get("referer")
            if not origin or urlparse(origin).netloc.lower() != request.headers["host"].lower():   # a browser always sends one
                return PlainTextResponse("bad origin", status_code=403)
            size = request.headers.get("content-length", "")           # refuse oversized bodies before anything reads them
            if not size.isdigit():
                return PlainTextResponse("a request body with a length is required", status_code=411)
            if int(size) > (MAX_UPLOAD_BYTES if path == "/import" else MAX_FORM_BYTES):
                return PlainTextResponse("request too large", status_code=413)
        if path in ANONYMOUS_PATHS or path.startswith("/static/"):
            return await call_next(request)
        session = auth_mod.read_session(auth_mod.cookie_value(request.headers.get("cookie", ""),
                                                              auth_mod.cookie_name(request.url.scheme == "https")))
        user = users.get(session[0]) if session else None
        if user and (user["disabled"] or user["sv"] != session[1]):
            user = None                                                   # signed out everywhere, password changed, or disabled
        if path in open_paths and not (user and path in ("/login", "/signup")):
            if user:
                request.state.user = user
            return await call_next(request)
        if not user:
            return sign_in_target(request)
        request.state.user = user
        if path in ("/login", "/signup"):                               # already signed in
            return RedirectResponse("/", status_code=303)
        unlocked = registry.get(user["id"])
        if not unlocked:
            if path in locked_paths:
                return await call_next(request)
            if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                return RedirectResponse(f"/unlock?{urlencode({'next': path})}", status_code=303)
            return PlainTextResponse("data is locked: unlock it with your password", status_code=423)
        slot = in_flight.setdefault(user["id"], threading.BoundedSemaphore(MAX_IN_FLIGHT))
        if not slot.acquire(blocking=False):                             # one profile cannot occupy every worker thread
            return PlainTextResponse("too many requests at once for your profile; try again in a moment", status_code=429)
        token = vault.current_vault.set(unlocked)                        # from here on, "the database" means this person's
        try:
            return await call_next(request)
        finally:
            vault.current_vault.reset(token)
            slot.release()

    @app.exception_handler(vault.VaultBusy)
    async def vault_busy(request: Request, exc: Exception):
        return PlainTextResponse("Your data is busy (a mail sync or import is writing). Wait a moment and try again.", status_code=503)

    @app.exception_handler(vault.VaultLocked)
    async def vault_locked(request: Request, exc: Exception):
        return PlainTextResponse("Your data is locked. Sign in again.", status_code=423)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        resp = await check(request, call_next)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        # NOT "no-referrer": with that policy browsers send `Origin: null` on form posts, which the origin check (rightly) refuses,
        # so no form would ever work. "same-origin" sends Origin to this site and nothing to any other site.
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = CSP
        if not request.url.path.startswith("/static/") and "cache-control" not in resp.headers:
            resp.headers["Cache-Control"] = "no-store"             # figures and statements are never kept by the browser
        if request.url.scheme == "https":
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        return resp

    def open_review_count() -> int:
        conn = connect_ro(db_path)
        try:
            return len(analytics.open_review_items(conn))
        finally:
            conn.close()

    def render(request: Request, name: str, status_code: int = 200, **ctx):
        return templates.TemplateResponse(request, name, {"mb": make_labeler(), "review_open": open_review_count(),
                                                          "me": request.state.user, **ctx}, status_code=status_code)

    def after_unlock(v: vault.Vault) -> None:
        """Queue (never apply) duplicate-folio / duplicate-transaction findings so the Portfolio page can warn."""
        import contextvars
        import threading

        def scan() -> None:
            from finance.ledger import Ledger
            try:
                led = Ledger(db_path)
                try:
                    led.flag_duplicate_accounts()
                    led.flag_duplicate_transactions()
                finally:
                    led.close()
            except Exception:                                     # a failed scan must never get in anyone's way
                pass

        ctx = contextvars.copy_context()
        ctx.run(vault.current_vault.set, v)
        threading.Thread(target=ctx.run, args=(scan,), daemon=True).start()

    accounts.register(app, SimpleNamespace(users=users, registry=registry, throttle=throttle, templates=templates,
                                           challenges=challenges, render=render, after_unlock=after_unlock))

    # ── overview (the landing page) and the mutual fund portfolio ────────────────

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        conn = connect_ro(db_path)
        try:
            data = analytics.overview(conn, today())
            review_count = len(analytics.open_review_items(conn))
        finally:
            conn.close()
        return render(request, "overview.html", ov=data, review_count=review_count, page="home")

    @app.get("/portfolio", response_class=HTMLResponse)
    def home(request: Request):
        conn = connect_ro(db_path)
        try:
            view = analytics.portfolio(conn)
            redeemed = analytics.redemptions(conn)
            open_items = analytics.open_review_items(conn)
        finally:
            conn.close()
        review_count = len(open_items)
        unreliable = sum(i["kind"] in ("duplicate_folio", "duplicate_txns") for i in open_items)
        return render(request, "portfolio.html", view=view, review_count=review_count, unreliable=unreliable, page="portfolio", redeemed=redeemed,
                      categories=analytics.share_breakdown(view.by_category),
                      cat_chart=[[k, float(pct)] for k, _, pct in analytics.share_breakdown(view.by_category)],
                      amcs=analytics.share_breakdown(view.by_amc, keep=1000))

    @app.get("/transactions", response_class=HTMLResponse)
    def transactions(request: Request, q: str = "", kind: str = "", date_from: str = "", date_to: str = "",
                     account_type: str = "", sort: str = "date_desc"):
        conn = connect_ro(db_path)
        try:
            rows = analytics.search_transactions(conn, text=q, kind=kind, date_from=date_from, date_to=date_to,
                                                 account_type=account_type, sort=sort)
            kinds = analytics.txn_kinds(conn, account_type)
            types = analytics.account_types(conn)
        finally:
            conn.close()
        return render(request, "transactions.html", rows=rows, kinds=kinds, types=types, page="txns",
                      q=q, kind=kind, date_from=date_from, date_to=date_to, account_type=account_type,
                      sort=sort if sort in analytics.TXN_SORTS else "date_desc", sorts=analytics.TXN_SORTS)

    @app.get("/accounts", response_class=HTMLResponse)
    def accounts_page(request: Request):
        conn = connect_ro(db_path)
        try:
            overview = analytics.accounts_overview(conn)
            flow = analytics.cash_flow_by_month(conn)
            card_flow = analytics.card_spending_by_month(conn)
            nps = analytics.nps_overview(conn)
        finally:
            conn.close()
        return render(request, "accounts.html", page="accounts", flow=flow, card_flow=card_flow, nps=nps,
                      today=today().isoformat(), **overview)

    # ── import ────────────────────────────────────────────────────────────────

    from finance.importer import STATEMENT_TYPES, TYPE_LABELS
    PARSER_LABELS = {"cas": "mutual fund CAS", "icici": "ICICI account statement", "icici_card": "ICICI credit card",
                     "hdfc_card": "HDFC credit card", "axis_card": "Axis credit card", "tata_card": "Tata credit card",
                     "nps": "NPS statement"}
    PARSER_CHOICES = [(k, TYPE_LABELS[k] + ("" if STATEMENT_TYPES[k] else " (not supported yet)")) for k in STATEMENT_TYPES]

    def import_context(**extra):
        conn = connect_ro(db_path)
        try:
            docs = analytics.list_documents(conn, source="upload")         # manual imports only; mail has Documents
        finally:
            conn.close()
        return dict(docs=docs, page="import", outcomes=[], summary=None, choices=PARSER_CHOICES,
                    selected="bank", parser_labels=PARSER_LABELS, **extra)

    @app.get("/import", response_class=HTMLResponse)
    def import_page(request: Request):
        return render(request, "import.html", **import_context())

    MAX_UPLOAD_FILES = 25

    @app.post("/import", response_class=HTMLResponse)
    def import_upload(request: Request, file: list[UploadFile] = File(...), password: str = Form(""),
                      parser: str = Form("bank")):
        from finance.importer import import_any
        from finance.passwords import DecryptAgent, Identity
        if parser not in STATEMENT_TYPES:
            parser = "bank"
        cfg = load_config(mail_config_path)
        templates = list(dict.fromkeys(t for a in cfg.accounts for r in a.senders for t in r.templates))
        typed = [x for x in (password,) if x]

        def import_one(upload: UploadFile) -> dict:
            """One file: unlock it the way mailbox sync does (a typed password first, then the Identity templates from
            your sender rules and your default password), then import it. Passwords stay in memory; nothing is stored or echoed."""
            out = {"name": upload.filename or "statement.pdf", "result": None, "used": None, "error": None}
            data = upload.file.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES or not data.startswith(b"%PDF"):
                out["error"] = "Not a PDF (or larger than 25 MB)."
                return out
            try:
                unlocked = DecryptAgent(Identity()).unlock(data, "upload", templates, priority=typed)
            except Exception:                    # not something pdfminer can inspect: let the parser say what is wrong
                unlocked = None
            if unlocked is not None and not unlocked.ok:
                out["error"] = unlocked.reason + " Type the PDF password above."
                return out
            pw = unlocked.password.get_secret_value() if unlocked and unlocked.password else (password or None)
            fd, tmp = tempfile.mkstemp(suffix=".pdf")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                out["result"], out["used"] = import_any(tmp, parser, pw, db_path=db_path, original_name=upload.filename)
            except Exception as exc:
                out["error"] = f"Import failed: {type(exc).__name__}: {exc}"
            finally:
                Path(tmp).unlink(missing_ok=True)
            return out

        uploads = file[:MAX_UPLOAD_FILES]
        outcomes = [import_one(u) for u in uploads]
        if len(file) > MAX_UPLOAD_FILES:
            outcomes.append({"name": "", "result": None, "used": None,
                             "error": f"Only the first {MAX_UPLOAD_FILES} files were imported; upload the rest again."})
        summary = {"imported": sum(1 for o in outcomes if o["result"] and o["result"].status != "duplicate"),
                   "known": sum(1 for o in outcomes if o["result"] and o["result"].status == "duplicate"),
                   "failed": sum(1 for o in outcomes if o["error"])}
        ctx = import_context()
        ctx.update(outcomes=outcomes, summary=summary, selected=parser)
        return render(request, "import.html", **ctx)

    PROVIDER_LABEL = {"gmail": "Gmail", "yahoo": "Yahoo Mail"}

    def make_labeler():
        """Friendly mailbox names for templates: 'Gmail · gmail-main'. Reads the config once per page."""
        cfg = load_config(mail_config_path)

        def label(name: str, folder: str = "", detail: bool = False) -> str:
            if name == "upload":
                return "Manual upload"
            if not name:
                return "—"
            acct = cfg.account(name)
            provider = (PROVIDER_LABEL.get(acct.provider) or acct.imap_host) if acct else "Mailbox (removed)"
            text = f"{provider} · {name}"
            if detail and acct:
                text = f"{provider} · {acct.username} ({name})"
            if folder:
                text += f" · {'Inbox' if folder.upper() == 'INBOX' else folder}"
            return text
        return label

    # ── documents: which email / file produced what ───────────────────────────

    @app.get("/documents", response_class=HTMLResponse)
    def documents_page(request: Request, q: str = "", status: str = "", account: str = ""):
        conn = connect_ro(db_path)
        try:
            docs = analytics.list_documents(conn, 300, status=status, account=account, q=q)
            statuses, accounts = analytics.document_filters(conn)
        finally:
            conn.close()
        return render(request, "documents.html", docs=docs, statuses=statuses, accounts=accounts, page="documents",
                      q=q, status=status, account=account)

    # ── mail sync runs (started from the Mailboxes page) ────────────────────────

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str):
        meta = jobs.get(job_id)
        if not meta:
            return PlainTextResponse("no such job", status_code=404)
        return render(request, "job.html", job=meta, log=jobs.log(job_id), page="mailboxes")

    @app.get("/jobs/{job_id}/log", response_class=HTMLResponse)
    def job_log(request: Request, job_id: str):
        meta = jobs.get(job_id)
        if not meta:
            return PlainTextResponse("no such job", status_code=404)
        return templates.TemplateResponse(request, "_joblog.html", {"job": meta, "log": jobs.log(job_id)})

    # ── mailboxes & identity (secrets are write-only: sealed in your own vault, never shown) ──

    from finance import secrets as secret_store
    from finance.mailconfig import (MailboxAccount, default_cas_rules, format_sender_lines, load_config,
                                    parse_sender_lines, save_config)

    def mailboxes_ctx(request: Request, *, edit: str = "", flash: str = "", error: str = "", form: dict | None = None):
        cfg = load_config(mail_config_path)
        conn = connect_ro(db_path)
        try:
            states = {r["account"]: r for r in conn.execute("SELECT * FROM imap_state")}
        finally:
            conn.close()
        state, now = autosync_mod.load_state(), time.time()
        accounts = [dict(a=a, auto_next=autosync_mod.next_run(a.auto_sync_hours, state.get(a.name), now),
                         password_set=secret_store.has_secret(a.secret_key),
                         last_sync=(states.get(a.name) or {"last_sync": None})["last_sync"] if a.name in states else None,
                         senders=format_sender_lines(a.senders)) for a in cfg.accounts]
        current = next((x for x in accounts if x["a"].name == edit), None)
        form = form or ({"name": current["a"].name, "provider": current["a"].provider,
                         "username": current["a"].username, "host": current["a"].host or "",
                         "folder": current["a"].folder, "lookback_days": current["a"].lookback_days,
                         "senders": current["senders"], "require_auth": current["a"].require_auth,
                         "auto_sync_hours": current["a"].auto_sync_hours}
                        if current else {"name": "", "provider": "gmail", "username": "", "host": "",
                                         "folder": "INBOX", "lookback_days": 365,
                                         "senders": format_sender_lines(default_cas_rules()), "require_auth": True,
                                         "auto_sync_hours": 0})
        return dict(page="mailboxes", accounts=accounts, flash=flash, error=error, form=form, editing=bool(current),
                    running=jobs.running(), auto_choices=autosync_mod.AUTO_SYNC_CHOICES,
                    idle_hours=registry.idle / 3600)

    @app.get("/mailboxes", response_class=HTMLResponse)
    def mailboxes_page(request: Request, edit: str = "", flash: str = ""):
        return render(request, "mailboxes.html", **mailboxes_ctx(request, edit=edit, flash=flash))

    @app.post("/mailboxes/save", response_class=HTMLResponse)
    def mailboxes_save(request: Request, name: str = Form(...), provider: str = Form(...), username: str = Form(...),
                       host: str = Form(""), folder: str = Form("INBOX"), lookback_days: int = Form(365),
                       senders: str = Form(""), app_password: str = Form(""), require_auth: str = Form(""),
                       auto_sync_hours: int = Form(0)):
        form = dict(name=name, provider=provider, username=username, host=host, folder=folder, auto_sync_hours=auto_sync_hours,
                    lookback_days=lookback_days, senders=senders, require_auth=bool(require_auth))
        try:
            acct = MailboxAccount(name=name, provider=provider, username=username.strip(), host=host.strip() or None,
                                  folder=folder.strip() or "INBOX", lookback_days=lookback_days,
                                  require_auth=bool(require_auth), senders=parse_sender_lines(senders),
                                  auto_sync_hours=auto_sync_hours)
            acct.imap_host
            if not acct.senders:
                raise ValueError("add at least one allowed sender")
            cfg = load_config(mail_config_path)
            previous = cfg.account(acct.name)
            cfg.accounts = [a for a in cfg.accounts if a.name != acct.name] + [acct]
            if app_password:
                secret_store.set_secret(acct.secret_key, app_password.strip())
            elif previous and not acct.same_server(previous):
                secret_store.delete_secret(acct.secret_key)               # a saved password never follows a changed server or username
                raise ValueError("you changed the server or username, so enter the app password again")
            elif not secret_store.has_secret(acct.secret_key):
                raise ValueError("enter the app password (it is sealed in your own vault, never shown again)")
            save_config(cfg, mail_config_path)
        except ValueError as exc:
            return render(request, "mailboxes.html", **mailboxes_ctx(request, error=str(exc), form=form))
        return RedirectResponse(f"/mailboxes?flash=Saved+{acct.name}", status_code=303)

    @app.post("/mailboxes/{name}/test", response_class=HTMLResponse)
    def mailboxes_test(request: Request, name: str):
        from finance.sources.base import MailboxError
        from finance.sources.imap import ImapSource, _default_connect
        acct = load_config(mail_config_path).account(name)
        if not acct:
            return PlainTextResponse("no such mailbox", status_code=404)

        class _NoState:
            def get(self, *a): return None
            def set(self, *a): pass
        try:
            n = ImapSource(acct, _NoState(), connect=imap_connect or _default_connect).check()
            return render(request, "mailboxes.html", **mailboxes_ctx(
                request, flash=f"{name}: connected read-only; {n} message(s) from your allowed senders are visible."))
        except MailboxError as exc:
            return render(request, "mailboxes.html", **mailboxes_ctx(request, error=f"{name}: {exc}"))

    @app.post("/mailboxes/{name}/delete")
    def mailboxes_delete(name: str):
        cfg = load_config(mail_config_path)
        acct = cfg.account(name)
        if acct:
            cfg.accounts = [a for a in cfg.accounts if a.name != name]
            save_config(cfg, mail_config_path)
            secret_store.delete_secret(acct.secret_key)
        return RedirectResponse(f"/mailboxes?flash=Removed+{name}+(imported+data+is+kept)", status_code=303)

    @app.post("/mailboxes/{name}/sync")
    def mailboxes_sync(name: str):
        try:
            return RedirectResponse(f"/jobs/{jobs.start('sync', {'accounts': [name]})}", status_code=303)
        except RuntimeError as exc:
            return PlainTextResponse(str(exc), status_code=409)

    @app.post("/mailboxes/{name}/rescan")
    def mailboxes_rescan(name: str):
        """Forget the saved position so the next sync re-reads allowed mail. Idempotent; also refreshes email details."""
        from finance.ledger import Ledger
        if not load_config(mail_config_path).account(name):
            return PlainTextResponse("no such mailbox", status_code=404)
        ledger = Ledger(db_path)
        try:
            ledger.reset_imap_state(name)
        finally:
            ledger.close()
        try:
            return RedirectResponse(f"/jobs/{jobs.start('sync', {'accounts': [name]})}", status_code=303)
        except RuntimeError as exc:
            return PlainTextResponse(str(exc), status_code=409)

    IDENTITY_LABELS = {"name": "Full name", "dob": "Date of birth", "pan": "PAN", "mobile": "Mobile number",
                       "password": "Default PDF password (tried on every statement: CAS, bank, card, NPS)"}

    def identity_ctx(flash: str = "", error: str = ""):
        from finance.passwords import Identity
        ident = Identity()
        return dict(page="identity", flash=flash, error=error,
                    extra_count=len(ident.extra_passwords()),
                    fields=[(k, v, secret_store.has_secret(f"id:{k}")) for k, v in IDENTITY_LABELS.items()])

    @app.get("/identity", response_class=HTMLResponse)
    def identity_page(request: Request, flash: str = ""):
        return render(request, "identity.html", **identity_ctx(flash=flash))

    @app.post("/identity", response_class=HTMLResponse)
    async def identity_save(request: Request):
        form = await request.form()
        dob = str(form.get("dob", "")).strip()
        if dob:                                               # validate before touching any secret
            try:
                from datetime import date as _d
                if _d.fromisoformat(dob) > _d.today():
                    raise ValueError
            except ValueError:
                return render(request, "identity.html", **identity_ctx(error="Date of birth must be a valid date in the past."))
        if form.get("clear_extra"):
            secret_store.delete_secret("id:extra_passwords")
        for key in IDENTITY_LABELS:
            value = str(form.get(key, "")).strip()
            if form.get(f"clear_{key}"):
                secret_store.delete_secret(f"id:{key}")
                if key == "password":
                    secret_store.delete_secret("id:cas_password")      # the old name; otherwise it would reappear
            elif value:
                secret_store.set_secret(f"id:{key}", value)
        return RedirectResponse("/identity?flash=Saved", status_code=303)

    # ── review queue ──────────────────────────────────────────────────────────

    def _review_page(request: Request, note: str = ""):
        conn = connect_ro(db_path)
        try:
            items = analytics.review_items_detailed(conn)
        finally:
            conn.close()
        return render(request, "review.html", items=items, page="review", note=note)

    @app.post("/review/scan-duplicates", response_class=HTMLResponse)
    def review_scan_duplicates(request: Request):
        from finance.ledger import Ledger
        ledger = Ledger(db_path)
        try:
            found = ledger.flag_duplicate_accounts() + ledger.flag_duplicate_transactions()
        finally:
            ledger.close()
        return _review_page(request, f"Scan finished: {len(found)} new finding(s) queued below." if found
                            else "Scan finished: nothing new found.")

    @app.post("/review/{item_id}/duplicate-txns", response_class=HTMLResponse)
    def review_duplicate_txns(request: Request, item_id: int):
        from finance.ledger import Ledger
        conn = connect_ro(db_path)
        try:
            row = conn.execute("SELECT id FROM review_queue WHERE id=? AND kind='duplicate_txns' AND status='open'",
                               (item_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            return PlainTextResponse("no such open item", status_code=404)
        ledger = Ledger(db_path)
        try:
            backup = ledger.backup()
            n = ledger.remove_duplicate_transactions()
        finally:
            ledger.close()
        return _review_page(request, f"Removed {n} duplicate transaction(s). A sealed backup of your ledger was saved first.")

    @app.post("/review/merge-all-duplicates", response_class=HTMLResponse)
    def review_merge_all(request: Request):
        from finance.ledger import Ledger
        ledger = Ledger(db_path)
        try:
            backup = ledger.backup()
            res = ledger.merge_all_duplicates()
        finally:
            ledger.close()
        return _review_page(request, f"Merged {res['merged']} folio pair(s): {res['moved']} transaction(s) moved, "
                                     f"{res['dropped']} duplicate(s) dropped. A sealed backup of your ledger was saved first.")

    @app.post("/review/{item_id}/duplicate/{decision}", response_class=HTMLResponse)
    def review_duplicate(request: Request, item_id: int, decision: str):
        from finance.ledger import Ledger
        conn = connect_ro(db_path)
        try:
            row = conn.execute("SELECT * FROM review_queue WHERE id=? AND kind='duplicate_folio' AND status='open'",
                               (item_id,)).fetchone()
        finally:
            conn.close()
        if not row or decision not in ("merge", "separate"):
            return PlainTextResponse("no such open item", status_code=404)
        ledger = Ledger(db_path)
        try:
            if decision == "separate":
                ledger.keep_separate(item_id)
                note = "Kept as separate folios."
            else:
                _, pa, pb = row["ref"].split(":")
                backup = ledger.backup()                     # always snapshot before changing data
                res = ledger.merge_accounts(int(pa), int(pb))
                note = (f"Merged: {res['moved']} transaction(s) moved, {res['dropped']} duplicate(s) dropped. "
                        f"A sealed backup of your ledger was saved first.")
        except ValueError as exc:
            note = f"Could not merge: {exc}"
        finally:
            ledger.close()
        return _review_page(request, note)

    @app.post("/review/{item_id}/attention/{decision}", response_class=HTMLResponse)
    def review_attention(request: Request, item_id: int, decision: str, password: str = Form(""),
                         remember: str = Form(""), apply_all: str = Form("")):
        from finance.sync import SyncRunner
        conn = connect_ro(db_path)
        try:
            row = conn.execute("SELECT * FROM review_queue WHERE id=? AND kind='needs_attention' AND status='open'",
                               (item_id,)).fetchone()
            others = conn.execute("SELECT * FROM review_queue WHERE kind='needs_attention' AND status='open' "
                                  "AND id != ?", (item_id,)).fetchall()
        finally:
            conn.close()
        if not row or decision not in ("retry", "dismiss"):
            return PlainTextResponse("no such open item", status_code=404)
        runner = SyncRunner(db_path=db_path, config=load_config(mail_config_path), log=lambda m: None)
        doc_id = int(row["ref"].split(":")[1])
        pw = password or None                              # typed password: used in memory, never echoed or logged
        if decision == "dismiss":
            runner.dismiss(doc_id)
            note = "Dismissed."
        else:
            note = runner.retry(doc_id, password=pw, remember=bool(remember))
            if apply_all and pw:
                results = [runner.retry(int(o["ref"].split(":")[1]), password=pw) for o in others]
                ok = sum(r in ("imported", "duplicate") for r in results)
                note += f" · tried the same password on {len(results)} other waiting statement(s): {ok} unlocked."
                if ok and remember:
                    runner.remember_password(pw)
            if pw and remember and note.split(" ")[0] in ("imported", "duplicate"):
                note += " · password remembered in your vault."
        conn = connect_ro(db_path)
        try:
            items = analytics.review_items_detailed(conn)
        finally:
            conn.close()
        return render(request, "review.html", items=items, page="review", note=note)

    @app.get("/review", response_class=HTMLResponse)
    def review_page(request: Request):
        conn = connect_ro(db_path)
        try:
            items = analytics.review_items_detailed(conn)
        finally:
            conn.close()
        return render(request, "review.html", items=items, page="review")

    @app.post("/review/{item_id}/folio/{decision}")
    def review_folio(item_id: int, decision: str):
        from finance.ledger import Ledger
        if decision not in ("close", "keep"):
            return PlainTextResponse("bad decision", status_code=404)
        ledger = Ledger(db_path)
        try:
            ledger.resolve_folio_missing(item_id, close=(decision == "close"))
        except ValueError:
            return PlainTextResponse("no such open item", status_code=404)
        finally:
            ledger.close()
        return RedirectResponse("/review", status_code=303)

    @app.post("/review/{item_id}/resolve")
    def review_resolve(item_id: int):
        from finance.ledger import Ledger
        ledger = Ledger(db_path)
        try:
            ledger.resolve_review(item_id)
        finally:
            ledger.close()
        return RedirectResponse("/review", status_code=303)

    return app
