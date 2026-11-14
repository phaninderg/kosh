"""Profiles through the web UI: first-run setup, invites, sign-in, unlocking, recovery, the Account page, the admin page,
and the guarantee that one person's data is invisible to everyone else, the admin included."""

import contextlib
import re
import time

import pytest
from fastapi.testclient import TestClient

import finance.web.app as webapp
from finance import secrets, users as users_mod, vault
from finance.db import connect_ro
from finance.ledger import Ledger
from finance.mailconfig import MailConfig, MailboxAccount, SenderRule, load_config, save_config
from finance.users import Users
from finance.web import auth
from tests.conftest import PASSWORD, make_cas
from tests.test_web import FakeJobs

PW2 = "another long passphrase"
KEY = re.compile(r"(?:[A-Z2-7]{4}-){7}[A-Z2-7]{4}")


class Site:
    """A server with nobody signed up yet, and a browser (cookie jar) for each person."""

    def __init__(self, tmp_path, scheme="http"):
        self.users = Users(tmp_path / "site" / "users.json")
        self.registry = vault.Registry()
        self.jobs = FakeJobs()
        self.app = webapp.create_app(vault.VAULT_DB, self.jobs, users=self.users, registry=self.registry,
                                     extra_hosts=["kosh.example.com", "testserver"])
        self.scheme, self.tmp = scheme, tmp_path

    def browser(self) -> TestClient:
        host = "testserver" if self.scheme == "http" else "kosh.example.com"
        return TestClient(self.app, base_url=f"{self.scheme}://{host}", follow_redirects=False,
                          headers={"origin": f"{self.scheme}://{host}"})

    def signup(self, browser, name, password=PASSWORD, code=None):
        code = code or self.users.new_setup_code()
        return browser.post("/signup", data={"name": name, "password": password, "confirm": password, "code": code})

    def person(self, name, password=PASSWORD, admin=False):
        """(browser signed in as `name`, recovery key). The first person is the admin; others join with an invite."""
        b = self.browser()
        code = self.users.new_setup_code() if not self.users.has_users() else self.users.make_invite(name)
        r = self.signup(b, name, password, code)
        assert r.status_code == 200, r.text[:300]
        return b, KEY.search(r.text).group(0)

    @contextlib.contextmanager
    def as_user(self, name):
        user = self.users.by_name(name)
        token = vault.current_vault.set(self.registry.get(user["id"]))
        try:
            yield user
        finally:
            vault.current_vault.reset(token)


@pytest.fixture
def site(tmp_path):
    return Site(tmp_path)


# ── first run ────────────────────────────────────────────────────────────────

def test_before_anyone_signs_up_every_page_leads_to_the_signup(site):
    b = site.browser()
    assert b.get("/", headers={"accept": "text/html"}).headers["location"] == "/signup"
    assert b.get("/login").headers["location"] == "/signup"
    page = b.get("/signup").text
    assert "setup code" in page and "admin" in page


def test_the_first_profile_needs_the_setup_code_and_becomes_the_admin(site):
    b = site.browser()
    code = site.users.new_setup_code()
    bad = site.signup(b, "Asha", code="WRONG-CODE-HERE")
    assert bad.status_code == 400 and "setup code is not right" in bad.text and not site.users.has_users()
    ok = site.signup(b, "Asha", code=code)
    assert ok.status_code == 200 and "recovery key" in ok.text.lower() and KEY.search(ok.text)
    (asha,) = site.users.all()
    assert asha["role"] == "admin" and asha["name"] == "Asha"
    assert b.get("/").status_code == 200                                         # signed in and unlocked straight away
    assert site.signup(site.browser(), "Mallory", code=code).status_code == 400  # the setup code is spent


def test_the_recovery_key_is_shown_once_and_stored_nowhere(site):
    b, key = site.person("Asha")
    assert key not in (site.users.path.read_text())
    on_disk = b"".join(p.read_bytes() for p in (vault.DATA_DIR).rglob("*") if p.is_file())
    assert key.encode() not in on_disk and key.replace("-", "").encode() not in on_disk
    assert b.get("/account").status_code == 200 and key not in b.get("/account").text


def test_signup_validates_the_password_and_the_name(site):
    b = site.browser()
    code = site.users.new_setup_code()
    assert "at least 12" in site.signup(b, "Asha", "short", code).text
    assert "differ" in b.post("/signup", data={"name": "Asha", "password": PASSWORD, "confirm": PW2, "code": code}).text
    assert "1 to 40" in site.signup(b, "   ", PASSWORD, code).text
    assert not site.users.has_users()


# ── invites ───────────────────────────────────────────────────────────────────

def test_only_the_admin_can_make_invites_and_each_works_once(site):
    admin, _ = site.person("Asha")
    code = re.search(r"(?:[A-HJ-NP-Z2-9]{4}-){2}[A-HJ-NP-Z2-9]{4}", admin.post("/admin/invite", data={"note": "Ravi"}).text).group(0)
    ravi = site.browser()
    assert site.signup(ravi, "Ravi", code=code).status_code == 200
    assert site.users.by_name("Ravi")["role"] == "member"
    assert "invite code is not valid" in site.signup(site.browser(), "Sneaky", code=code).text          # one use only


def test_an_invite_expires_and_can_be_revoked(site, monkeypatch):
    admin, _ = site.person("Asha")
    old = site.users.make_invite("late")
    revoked = site.users.make_invite("gone")
    short = next(i["hash"] for i in site.users.invites() if i["note"] == "gone")
    assert admin.post(f"/admin/invite/{short}/revoke").status_code == 303
    assert "not valid" in site.signup(site.browser(), "Nobody", code=revoked).text
    real = time.time
    monkeypatch.setattr("time.time", lambda: real() + users_mod.INVITE_TTL + 5)
    assert "not valid" in site.signup(site.browser(), "Late", code=old).text


def test_members_cannot_reach_the_admin_page_or_its_actions(site):
    admin, _ = site.person("Asha")
    ravi, _ = site.person("Ravi")
    assert ravi.get("/admin").headers["location"] == "/"
    assert ravi.post("/admin/invite", data={}).headers["location"] == "/"
    uid = site.users.by_name("Asha")["id"]
    assert ravi.post(f"/admin/users/{uid}/disable").headers["location"] == "/"
    assert not site.users.get(uid)["disabled"]
    assert "People" in admin.get("/").text and "People" not in ravi.get("/").text


# ── signing in, locking, unlocking ───────────────────────────────────────────────

def test_a_wrong_password_gets_one_vague_message_and_no_session(site):
    site.person("Asha")
    b = site.browser()
    for name, pw in (("Asha", "not the password"), ("Nobody", PASSWORD)):
        r = b.post("/login", data={"name": name, "password": pw})
        assert r.status_code == 401 and "Incorrect name or password." in r.text and "set-cookie" not in r.headers
    assert b.get("/", headers={"accept": "text/html"}).status_code == 303


def test_signing_in_opens_the_data_and_signing_out_ends_the_session_only(site):
    b, _ = site.person("Asha")
    c = site.browser()
    r = c.post("/login", data={"name": "asha", "password": PASSWORD, "next": "/accounts"})          # names ignore case
    assert r.status_code == 303 and r.headers["location"] == "/accounts" and c.get("/accounts").status_code == 200
    c.post("/logout")
    assert c.get("/accounts").status_code == 401
    assert b.get("/accounts").status_code == 200                                    # another device is unaffected
    assert site.registry.unlocked(site.users.by_name("Asha")["id"])                 # and the data stays unlocked


def test_after_a_restart_a_valid_session_still_needs_the_password_to_unlock_the_data(site):
    b, _ = site.person("Asha")
    uid = site.users.by_name("Asha")["id"]
    site.registry.lock(uid)                                                          # the server restarted
    r = b.get("/transactions", headers={"accept": "text/html"})
    assert r.status_code == 303 and r.headers["location"].startswith("/unlock")
    assert b.get("/transactions").status_code == 423                                 # and nothing is served meanwhile
    assert b.post("/unlock", data={"password": "wrong password here"}).status_code == 401
    ok = b.post("/unlock", data={"password": PASSWORD, "next": "/transactions"})
    assert ok.status_code == 303 and ok.headers["location"] == "/transactions"
    assert b.get("/transactions").status_code == 200


def test_data_locks_itself_after_being_idle_and_on_request(site):
    b, _ = site.person("Asha")
    uid = site.users.by_name("Asha")["id"]
    site.registry.idle, now = 60, [time.time()]
    site.registry._clock = lambda: now[0]
    site.registry.get(uid)
    now[0] += 61
    assert b.get("/", headers={"accept": "text/html"}).headers["location"].startswith("/unlock")
    b.post("/unlock", data={"password": PASSWORD})
    assert b.post("/lock").headers["location"] == "/unlock" and not site.registry.unlocked(uid)


def test_guessing_the_password_is_throttled(site):
    site.person("Asha")
    b = site.browser()
    for _ in range(5):
        assert b.post("/login", data={"name": "Asha", "password": "nope nope nope nope"}).status_code == 401
    r = b.post("/login", data={"name": "Asha", "password": PASSWORD})
    assert r.status_code == 429 and "Too many attempts" in r.text and int(r.headers["retry-after"]) > 0


def test_login_cannot_bounce_to_another_site(site):
    site.person("Asha")
    b = site.browser()
    assert b.post("/login", data={"name": "Asha", "password": PASSWORD, "next": "//evil.example"}).headers["location"] == "/"
    assert b.post("/login", data={"name": "Asha", "password": PASSWORD, "next": "https://evil.example"}).headers["location"] == "/"


# ── recovery and password change ──────────────────────────────────────────────────

def test_the_recovery_key_resets_a_forgotten_password_and_keeps_the_data(site):
    b, key = site.person("Asha")
    with site.as_user("Asha"):
        secrets.set_secret("id:pan", "ABCDE1234F")
        led = Ledger(vault.VAULT_DB); led.import_statement(__import__("finance.parsers.cas", fromlist=["CasParser"]).CasParser().from_casdata(make_cas()), sha256="a"); led.close()
    site.registry.lock(site.users.by_name("Asha")["id"])
    c = site.browser()
    bad = c.post("/recover", data={"name": "Asha", "recovery_key": "AAAA-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA", "password": PW2, "confirm": PW2})
    assert bad.status_code == 401 and "do not match" in bad.text
    ok = c.post("/recover", data={"name": "Asha", "recovery_key": key, "password": PW2, "confirm": PW2})
    assert ok.status_code == 200 and KEY.search(ok.text).group(0) != key              # a new key is issued
    assert c.get("/").status_code == 200
    with site.as_user("Asha"):
        assert secrets.get_secret("id:pan") == "ABCDE1234F"                           # nothing was lost
        assert connect_ro(vault.VAULT_DB).execute("select count(*) from accounts").fetchone()[0] >= 1
    assert site.browser().post("/login", data={"name": "Asha", "password": PASSWORD}).status_code == 401   # old password gone
    assert site.browser().post("/login", data={"name": "Asha", "password": PW2}).status_code == 303
    again = site.browser().post("/recover", data={"name": "Asha", "recovery_key": key, "password": PASSWORD, "confirm": PASSWORD})
    assert again.status_code == 401                                                   # the old recovery key is spent


def test_changing_the_password_needs_the_current_one_and_signs_other_devices_out(site):
    phone, _ = site.person("Asha")
    laptop = site.browser()
    laptop.post("/login", data={"name": "Asha", "password": PASSWORD})
    assert laptop.post("/account/password", data={"current": "wrong wrong wrong", "new": PW2, "confirm": PW2}).status_code == 401
    assert laptop.post("/account/password", data={"current": PASSWORD, "new": "short", "confirm": "short"}).status_code == 400
    assert laptop.post("/account/password", data={"current": PASSWORD, "new": PW2, "confirm": PW2}).status_code == 303
    assert laptop.get("/").status_code == 200                                          # this device stays signed in
    assert phone.get("/", headers={"accept": "text/html"}).status_code == 303          # the other one does not
    assert site.browser().post("/login", data={"name": "Asha", "password": PW2}).status_code == 303
    with site.as_user("Asha"):
        secrets.set_secret("id:pan", "X")                                              # the data is still writable


def test_a_new_recovery_key_needs_the_password_and_retires_the_old_one(site):
    b, old = site.person("Asha")
    assert b.post("/account/recovery-key", data={"current": "wrong wrong wrong"}).status_code == 401
    new = KEY.search(b.post("/account/recovery-key", data={"current": PASSWORD}).text).group(0)
    assert new != old
    c = site.browser()
    assert c.post("/recover", data={"name": "Asha", "recovery_key": old, "password": PW2, "confirm": PW2}).status_code == 401
    assert c.post("/recover", data={"name": "Asha", "recovery_key": new, "password": PW2, "confirm": PW2}).status_code == 200


def test_sign_out_everywhere_ends_every_session_and_locks_the_data(site):
    a, _ = site.person("Asha")
    b = site.browser(); b.post("/login", data={"name": "Asha", "password": PASSWORD})
    a.post("/logout", data={"everywhere": "1"})
    assert b.get("/", headers={"accept": "text/html"}).status_code == 303 and a.get("/").status_code == 401
    assert not site.registry.unlocked(site.users.by_name("Asha")["id"])


# ── isolation: nobody sees anybody else's data, the admin included ─────────────────────

def seed(site, name, marker):
    """Give `name` a bank statement whose rows carry `marker`, a saved secret and a mailbox, all in their own vault."""
    from finance.parsers.icici import IciciStatementParser
    from tests.pdffix import icici_statement, row, section
    pdf = site.tmp / f"{marker}.pdf"
    pdf.write_bytes(icici_statement([section("Savings", f"XXXXXXXX{abs(hash(marker)) % 9000 + 1000}", "1000.00",
                                             [row("02-10-2026", "-100", own=f"UPI/{marker}/Coffee"), row("03-10-2026", "500", own=f"NEFT-{marker}")])]))
    with site.as_user(name):
        led = Ledger(vault.VAULT_DB)
        led.import_statement(IciciStatementParser().parse(pdf, None), sha256=marker.ljust(64, "0")[:64])
        led.close()
        secrets.set_secret("id:pan", f"PAN-{marker}")
        save_config(MailConfig(accounts=[MailboxAccount(name=f"box-{marker}", provider="gmail", username=f"{marker}@example.invalid",
                                                         senders=[SenderRule(match="camsonline.com")])]))


def test_each_person_sees_only_their_own_ledger_secrets_and_mailboxes(site):
    admin, _ = site.person("Asha")
    ravi, _ = site.person("Ravi")
    seed(site, "Asha", "ALPHA"); seed(site, "Ravi", "BRAVO")
    for browser, mine, theirs in ((admin, "ALPHA", "BRAVO"), (ravi, "BRAVO", "ALPHA")):
        txns, mailboxes = browser.get("/transactions").text, browser.get("/mailboxes").text
        assert mine in txns and f"box-{mine}" in mailboxes
        for page in (browser.get("/accounts").text, mailboxes, txns, browser.get("/identity").text, browser.get("/documents").text):
            assert theirs not in page and f"PAN-{theirs}" not in page
    with site.as_user("Ravi"):
        assert secrets.get_secret("id:pan") == "PAN-BRAVO" and load_config().accounts[0].name == "box-BRAVO"


def test_the_admin_has_no_page_route_or_key_that_reaches_someone_elses_data(site):
    admin, _ = site.person("Asha")
    ravi, _ = site.person("Ravi")
    seed(site, "Ravi", "BRAVO")
    page = admin.get("/admin").text
    assert "Ravi" in page and "BRAVO" not in page and "PAN-" not in page
    ravi_id = site.users.by_name("Ravi")["id"]
    for path in (f"/admin/users/{ravi_id}", f"/users/{ravi_id}", f"/admin/data/{ravi_id}", f"/admin/ledger/{ravi_id}"):
        assert admin.get(path).status_code in (404, 405)
    stored = site.users.path.read_text()                                              # what the server keeps to sign people in
    assert "BRAVO" not in stored and "PAN-" not in stored
    asha_dek = site.registry.get(site.users.by_name("Asha")["id"])._dek
    ravi_vault = site.registry.get(ravi_id)
    assert vault.Vault(ravi_id, asha_dek, ravi_vault.root).get("mailboxes.json") is None       # her key does not even find his files
    with pytest.raises(vault.VaultError):
        vault.unseal(asha_dek, (ravi_vault.root / "ledger.enc").read_bytes(), aad=b"ledger")        # the admin's key opens nothing of Ravi's


def test_a_stolen_copy_of_the_whole_data_folder_holds_no_readable_figures_or_secrets(site):
    site.person("Asha"); site.person("Ravi")
    seed(site, "Asha", "ZZALPHA"); seed(site, "Ravi", "ZZBRAVO")
    blob = b"".join(p.read_bytes() for p in vault.DATA_DIR.rglob("*") if p.is_file() and p.name != "audit.log" and p.suffix != ".pdf")
    for marker in (b"ZZALPHA", b"ZZBRAVO", b"PAN-ZZ", b"example.invalid", b"camsonline", b"SQLite format", b"Coffee"):
        assert marker not in blob, marker


def test_one_persons_background_job_is_invisible_to_another(site, monkeypatch):
    from finance.jobs import JobManager
    jobs = JobManager()
    monkeypatch.setattr("finance.jobs.run_sync", lambda params, log: (log("SECRET-LOG-LINE"), 0)[1])
    site.person("Asha"); site.person("Ravi")
    with site.as_user("Asha"):
        job = jobs.start("sync", {})
        for _ in range(100):
            if (jobs.get(job) or {}).get("status") == "done":
                break
            time.sleep(0.02)
        assert "SECRET-LOG-LINE" in jobs.log(job) and jobs.get(job)["status"] == "done"
    with site.as_user("Ravi"):
        assert jobs.get(job) is None and jobs.log(job) == "" and jobs.recent() == []
    with site.as_user("Asha"):
        assert [j["id"] for j in jobs.recent()] == [job]
    sealed = b"".join(p.read_bytes() for p in vault.DATA_DIR.rglob("*.enc"))
    assert b"SECRET-LOG-LINE" not in sealed


# ── admin controls ──────────────────────────────────────────────────────────────────

def test_disabling_a_profile_ends_its_sessions_and_locks_its_data(site):
    admin, _ = site.person("Asha")
    ravi, _ = site.person("Ravi")
    rid = site.users.by_name("Ravi")["id"]
    assert admin.post(f"/admin/users/{rid}/disable").status_code == 303
    assert ravi.get("/", headers={"accept": "text/html"}).status_code == 303 and not site.registry.unlocked(rid)
    assert site.browser().post("/login", data={"name": "Ravi", "password": PASSWORD}).status_code == 401
    admin.post(f"/admin/users/{rid}/enable")
    assert site.browser().post("/login", data={"name": "Ravi", "password": PASSWORD}).status_code == 303


def test_removing_a_profile_deletes_its_data_for_good_and_needs_their_name_typed(site):
    admin, _ = site.person("Asha")
    ravi, _ = site.person("Ravi")
    seed(site, "Ravi", "BRAVO")
    rid = site.users.by_name("Ravi")["id"]
    root = vault.USERS_DIR / rid
    assert root.is_dir()
    assert "type their name" in admin.post(f"/admin/users/{rid}/delete", data={"confirm": "wrong"}).text and root.is_dir()
    assert admin.post(f"/admin/users/{rid}/delete", data={"confirm": "ravi"}).status_code == 303
    assert not root.exists() and site.users.get(rid) is None
    assert ravi.get("/", headers={"accept": "text/html"}).status_code == 303


def test_the_last_admin_cannot_be_disabled_or_removed(site):
    admin, _ = site.person("Asha")
    aid = site.users.by_name("Asha")["id"]
    assert "only admin" in admin.post(f"/admin/users/{aid}/disable").text
    assert "only admin" in admin.post(f"/admin/users/{aid}/delete", data={"confirm": "Asha"}).text
    assert site.users.get(aid) is not None


# ── cookies, headers, hosts ────────────────────────────────────────────────────────────

def test_on_https_the_cookie_is_host_only_secure_and_the_host_must_match(tmp_path):
    site = Site(tmp_path, scheme="https")
    b = site.browser()
    r = site.signup(b, "Asha")
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-kosh_session=") and "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Domain" not in cookie and "Path=/" in cookie
    assert b.get("/").status_code == 200
    assert TestClient(site.app, base_url="https://evil.example").get("/login").status_code == 400
    assert b.post("/logout", headers={"origin": "https://evil.example"}).status_code == 403
    assert b.post("/logout", headers={"origin": ""}).status_code == 403                      # a form post must say where it came from


def test_every_response_carries_security_headers_and_pages_are_never_cached(tmp_path):
    site = Site(tmp_path, scheme="https")
    b = site.browser(); site.signup(b, "Asha")
    for path in ("/", "/login", "/account", "/transactions"):
        h = b.get(path).headers
        assert h["cache-control"] == "no-store" and h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
        assert h["referrer-policy"] == "no-referrer" and "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["strict-transport-security"].startswith("max-age=")
    assert "no-store" not in b.get("/static/style.css").headers.get("cache-control", "")


def test_two_session_cookies_with_one_name_are_both_refused(site):
    b, _ = site.person("Asha")
    good = b.cookies.get("kosh_session")
    b.cookies.clear()
    assert b.get("/", headers={"cookie": f"kosh_session={good}; kosh_session=planted", "accept": "text/html"}).status_code == 303
    assert b.get("/", headers={"cookie": f"kosh_session={good}"}).status_code == 200


def test_a_forged_or_expired_session_is_refused(site):
    b, _ = site.person("Asha")
    uid = site.users.by_name("Asha")["id"]
    b.cookies.clear()
    for forged in (f"{uid}.9999999999.0.deadbeef", "x", "", f"{uid}.1.0." + "0" * 64):
        assert b.get("/", headers={"cookie": f"kosh_session={forged}"}).status_code == 401
    assert auth.read_session(auth.issue_session(uid, 0), now=2 ** 34) is None


def test_the_manifest_and_service_worker_load_without_signing_in(site):
    b = site.browser()
    m = b.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.json()["display"] == "standalone"
    assert b.get("/sw.js").status_code == 200 and b.get("/static/icons/icon-192.png").status_code == 200
    assert b.get("/static/passkey.js").status_code == 200
    assert b.get("/transactions").status_code == 401                                          # while the pages themselves stay closed


def test_a_profile_marked_disabled_is_refused_even_if_its_session_version_was_not_bumped(site):
    b, _ = site.person("Asha")
    ravi, _ = site.person("Ravi")
    import json
    data = json.loads(site.users.path.read_text())
    next(u for u in data["users"].values() if u["name"] == "Ravi")["disabled"] = True        # as if edited by hand
    site.users.path.write_text(json.dumps(data))
    assert ravi.get("/", headers={"accept": "text/html"}).status_code == 303
    assert b.get("/").status_code == 200
