import pytest
from fastapi.testclient import TestClient

import finance.web.app as webapp
from finance.parsers.base import REGISTRY
from finance.parsers.cas import CasParser
from tests.conftest import make_cas
from tests.mailfix import FakeImap, make_email, make_pdf

TOKEN = "tok"


class FakeJobs:
    def __init__(self): self.started = []
    def running(self): return None
    def recent(self, n=10): return []
    def start(self, action, params=None):
        self.started.append((action, params)); return "job-1"
    def get(self, jid): return None


@pytest.fixture
def client(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(REGISTRY["cas"], "parse",
                        lambda path, password: CasParser().from_casdata(make_cas()), raising=False)
    jobs = FakeJobs()
    c = TestClient(webapp.create_app(TOKEN, db_path, jobs, mail_config_path=str(tmp_path / "mb.json"),
                                     imap_connect=lambda h, p: FakeImap({1: make_email(pdf=make_pdf())})),
                   base_url="http://testserver", follow_redirects=False,
                   headers={"origin": "http://testserver"})
    c.jobs, c.cfg_path = jobs, tmp_path / "mb.json"
    return c


def authed(c):
    assert c.get(f"/?token={TOKEN}").status_code == 303
    return c


def test_requires_token(client):
    assert client.get("/").status_code == 401
    assert client.get("/?token=wrong").status_code == 401
    assert client.get("/", headers={"host": "evil.example"}).status_code == 400


def test_cross_origin_post_rejected(client):
    authed(client)
    r = client.post("/mailboxes/gmail-main/sync", headers={"origin": "http://evil.example"})
    assert r.status_code == 403 and client.jobs.started == []


def test_import_then_portfolio_and_transactions(client):
    authed(client)
    r = client.post("/import", files={"file": ("cas.pdf", b"%PDF-1.4 fake", "application/pdf")},
                    data={"password": "never-stored", "parser": "cas"})
    assert r.status_code == 200 and "5 new transactions" in r.text
    again = client.post("/import", files={"file": ("cas.pdf", b"%PDF-1.4 fake", "application/pdf")}, data={"parser": "cas"})
    assert "already imported" in again.text
    home = client.get("/portfolio").text
    assert "Synthetic Flexi Cap Fund" in home and "₹24,150" in home
    assert "Synthetic Flexi Cap" in client.get("/transactions?q=Flexi").text
    assert "never-stored" not in home + r.text


def test_rejects_non_pdf(client):
    authed(client)
    r = client.post("/import", files={"file": ("x.pdf", b"hello", "application/pdf")}, data={"parser": "cas"})
    assert "Not a PDF" in r.text






def test_inr_grouping():
    assert webapp.inr(1234567.5, 2) == "₹12,34,567.50" and webapp.inr(-999) == "-₹999" and webapp.inr(None) == "—"


def test_review_folio_decision_via_ui(client, db_path):
    from finance.ledger import Ledger
    led = Ledger(db_path)
    led.import_statement(CasParser().from_casdata(make_cas()), sha256="a")
    led.import_statement(CasParser().from_casdata(make_cas(period=("2024-01-01", "2025-06-30"),
                                                           folio="9999990001", variant=1, schemes=("A",))), sha256="b")
    led.close()
    authed(client)
    page = client.get("/review").text
    assert "Folio missing from a newer statement" in page and "Mark closed" in page
    assert "Keep as held" in page and "…7890" in page
    held = lambda: client.get("/portfolio").text.split("Redeemed units")[0]                # the holdings, not the redeemed list
    assert held().count("Synthetic Flexi Cap Fund") == 2
    r = client.post("/review/1/folio/close")
    assert r.status_code == 303
    page = client.get("/portfolio").text
    assert page.split("Redeemed units")[0].count("Synthetic Flexi Cap Fund") == 1
    assert "dropped from a newer statement" in page and "Synthetic Flexi Cap Fund" in page.split("Redeemed units")[1]
    assert client.post("/review/1/folio/close").status_code == 404           # already resolved
    assert client.post("/review/1/folio/bogus").status_code == 404



# ── phase 2: mailboxes, identity, needs-attention ────────────────────────────

MB = dict(name="gmail-main", provider="gmail", username="me@example.invalid", host="", folder="INBOX",
          lookback_days=365, senders="camsonline.com | cas | {name4}{ddmm}\nkfintech.com", require_auth="1")


def test_identity_is_write_only(client):
    from finance import secrets
    authed(client)
    r = client.post("/identity", data={"name": "Test Person", "pan": "ABCDE1234F", "dob": "1990-05-17",
                                       "password": "s3cret!", "mobile": ""})
    assert r.status_code == 303
    page = client.get("/identity").text
    assert page.count("stored ✓") == 4 and "not set" in page
    for v in ("Test Person", "ABCDE1234F", "1990-05-17", "s3cret!"):
        assert v not in page
    assert secrets.get_secret("id:pan") == "ABCDE1234F"
    client.post("/identity", data={"clear_pan": "1"})
    assert secrets.get_secret("id:pan") is None
    assert "valid date" in client.post("/identity", data={"dob": "not-a-date"}).text


def test_mailbox_save_keeps_password_out_of_files_and_pages(client):
    from finance import secrets
    authed(client)
    r = client.post("/mailboxes/save", data={**MB, "app_password": "app-pw-123"})
    assert r.status_code == 303
    assert secrets.get_secret("imap:gmail-main") == "app-pw-123"
    assert "app-pw-123" not in client.cfg_path.read_text()
    assert oct(client.cfg_path.stat().st_mode & 0o777) == "0o600"
    page = client.get("/mailboxes").text
    assert "gmail-main" in page and "✓ stored" in page and "app-pw-123" not in page
    edit = client.get("/mailboxes?edit=gmail-main").text
    assert "app-pw-123" not in edit and "{name4}{ddmm}" in edit


def test_mailbox_validation_errors(client):
    authed(client)
    assert "app password" in client.post("/mailboxes/save", data={**MB, "app_password": ""}).text
    assert "unknown token" in client.post("/mailboxes/save", data={**MB, "app_password": "x",
                                          "senders": "camsonline.com | cas | {bogus}"}).text
    assert "domain or email" in client.post("/mailboxes/save", data={**MB, "app_password": "x",
                                            "senders": 'evil" OR ALL'}).text
    assert "host" in client.post("/mailboxes/save", data={**MB, "provider": "custom", "app_password": "x"}).text


def test_mailbox_test_sync_delete(client):
    from finance import secrets
    authed(client)
    client.post("/mailboxes/save", data={**MB, "app_password": "app-pw-123"})
    assert "connected read-only; 1 message" in client.post("/mailboxes/gmail-main/test").text
    r = client.post("/mailboxes/gmail-main/sync")
    assert r.headers["location"] == "/jobs/job-1" and client.jobs.started[-1] == ("sync", {"accounts": ["gmail-main"]})
    client.post("/mailboxes/gmail-main/delete")
    assert secrets.get_secret("imap:gmail-main") is None and "gmail-main" not in client.get("/mailboxes").text


def test_attention_retry_and_dismiss_from_ui(client, db_path, monkeypatch, tmp_path):
    from finance import analytics
    from finance.db import connect_ro
    from finance.mailconfig import MailConfig, MailboxAccount, SenderRule, save_config
    from tests.test_sync import runner
    monkeypatch.setattr("finance.sources.imap.get_secret", lambda k: "pw")
    monkeypatch.setattr(REGISTRY["cas"], "parse", lambda path, password: CasParser().from_casdata(make_cas()), raising=False)
    acct = MailboxAccount(name="m", provider="gmail", username="me@example.invalid",
                          senders=[SenderRule(match="camsonline.com")])
    save_config(MailConfig(accounts=[acct]), str(client.cfg_path))
    runner(db_path, {1: make_email(pdf=make_pdf("s3cret!")), 2: make_email(pdf=make_pdf("other"))},
           identity={}, accounts=[acct]).run()
    authed(client)
    page = client.get("/review").text
    assert page.count("Statement needs attention") == 2 and "Retry" in page and "password" in page
    ids = [i["id"] for i in analytics.open_review_items(connect_ro(db_path))]
    still = client.post(f"/review/{ids[0]}/attention/retry").text
    assert "still needs attention" in still
    client.post("/identity", data={"password": "s3cret!"})
    done = [client.post(f"/review/{i}/attention/retry").text for i in ids]
    assert sum("flash ok\">imported" in t for t in done) == 1       # only the PDF that s3cret! opens
    left = analytics.open_review_items(connect_ro(db_path))
    assert len(left) == 1                                         # the "other" password PDF can never open
    assert "Dismissed" in client.post(f"/review/{left[0]['id']}/attention/dismiss").text
    assert analytics.open_review_items(connect_ro(db_path)) == []
    assert client.post("/review/999/attention/retry").status_code == 404


def test_mailboxes_page_works_on_a_phase1_database(tmp_path, monkeypatch):
    import sqlite3
    from finance import db
    path = str(tmp_path / "p1.db")
    raw = sqlite3.connect(path); raw.executescript(db.SCHEMA); raw.execute("PRAGMA user_version = 1"); raw.commit(); raw.close()
    c = TestClient(webapp.create_app(TOKEN, path, FakeJobs(), mail_config_path=str(tmp_path / "mb.json")),
                   base_url="http://testserver", follow_redirects=False)
    authed(c)
    assert c.get("/mailboxes").status_code == 200 and c.get("/review").status_code == 200


def test_review_typed_password_apply_all_and_remember(client, db_path, monkeypatch, tmp_path):
    from finance import analytics, secrets
    from finance.db import connect_ro
    from finance.mailconfig import MailConfig, MailboxAccount, SenderRule, save_config
    from tests.test_sync import runner
    monkeypatch.setattr("finance.sources.imap.get_secret", lambda k: "pw")
    monkeypatch.setattr(REGISTRY["cas"], "parse", lambda path, password: CasParser().from_casdata(make_cas()), raising=False)
    acct = MailboxAccount(name="m", provider="gmail", username="me@example.invalid",
                          senders=[SenderRule(match="camsonline.com")])
    save_config(MailConfig(accounts=[acct]), str(client.cfg_path))
    runner(db_path, {1: make_email(pdf=make_pdf("custom-A")), 2: make_email(pdf=make_pdf("custom-A")),
                     3: make_email(pdf=make_pdf("custom-B"))}, identity={}, accounts=[acct]).run()
    authed(client)
    items = analytics.open_review_items(connect_ro(db_path))
    assert len(items) == 3 and 'type="password" name="password"' in client.get("/review").text
    a_item = items[1]                                                          # newest-first: [custom-B, custom-A, custom-A]
    r = client.post(f"/review/{a_item['id']}/attention/retry",
                    data={"password": "custom-A", "remember": "1", "apply_all": "1"})
    assert "custom-A" not in r.text                                          # never echoed back
    assert "imported" in r.text and "1 unlocked" in r.text and "remembered" in r.text
    assert len(analytics.open_review_items(connect_ro(db_path))) == 1        # only custom-B still waits
    assert "custom-A" in secrets.get_secret("id:extra_passwords")
    assert "1 saved" in client.get("/identity").text
    client.post("/identity", data={"clear_extra": "1"})
    assert secrets.get_secret("id:extra_passwords") is None


def test_documents_page_shows_email_details_and_filters(client, db_path, seen_pw=None):
    from finance.ledger import Ledger
    led = Ledger(db_path)
    led.import_statement(CasParser().from_casdata(make_cas()), sha256="up1", meta={"filename": "cas.pdf", "size": 2048,
                                                                                    "account": "upload"})
    led.record_attention(sha256="e1", message_id="<m1@x>", sender="donotreply@camsonline.com", parser="cas",
                         reason="locked", meta={"account": "gmail-main", "folder": "INBOX", "uid": 9,
                                                "received_at": "2025-06-02 04:30:00", "subject": "Your CAS",
                                                "filename": "cas.pdf", "size": 4096, "auth": "spf=pass"})
    led.close()
    authed(client)
    page = client.get("/documents").text
    assert "donotreply@camsonline.com" in page and "Your CAS" in page and "manual upload" in page
    assert 'datetime="2025-06-02T04:30:00Z"' in page and "&lt;m1@x&gt;" in page and "IMAP UID 9" in page
    only = client.get("/documents?status=needs_attention").text
    assert "Your CAS" in only and "manual upload" not in only
    assert "Your CAS" not in client.get("/documents?account=upload").text
    assert "Your CAS" in client.get("/documents?q=m1@x").text
    review = client.get("/review").text
    assert "gmail-main" in review and "Your CAS" in review and "4 KB" in review and "Mailbox (removed)" in review


def test_rescan_resets_position_and_starts_sync(client, db_path):
    from finance.ledger import Ledger
    from finance.mailconfig import MailConfig, MailboxAccount, SenderRule, save_config
    save_config(MailConfig(accounts=[MailboxAccount(name="m", provider="gmail", username="me@example.invalid",
                                                    senders=[SenderRule(match="camsonline.com")])]), str(client.cfg_path))
    led = Ledger(db_path); led.set_imap_state("m", "INBOX", 7, 42); led.close()
    authed(client)
    assert 'action="/mailboxes/m/rescan"' in client.get("/mailboxes").text
    r = client.post("/mailboxes/m/rescan")
    assert r.status_code == 303 and client.jobs.started[-1] == ("sync", {"accounts": ["m"]})
    assert Ledger(db_path).get_imap_state("m", "INBOX") is None
    assert client.post("/mailboxes/zzz/rescan").status_code == 404


def test_duplicate_folio_merge_from_the_ui(client, db_path, tmp_path, monkeypatch):
    from tests.test_duplicates import _scenario
    monkeypatch.setattr(webapp, "BACKUP_DIR", str(tmp_path / "bk"))
    _scenario(db_path)
    authed(client)
    page = client.get("/review").text
    assert "Possible duplicate folio" in page and "Merge into one folio" in page and "Different folios" in page
    assert "Scan for duplicate folios" in page
    assert client.get("/portfolio").text.count("Synthetic Flexi Cap Fund") >= 1
    from finance import analytics
    from finance.db import connect_ro
    dup_id = next(i["id"] for i in analytics.open_review_items(connect_ro(db_path)) if i["kind"] == "duplicate_folio")
    r = client.post(f"/review/{dup_id}/duplicate/merge")
    assert r.status_code == 200 and "Merged: 0 transaction(s) moved, 3 duplicate(s) dropped" in r.text
    assert list((tmp_path / "bk").glob("finance-*.db"))                       # backup taken first
    page = client.get("/portfolio").text
    assert "Synthetic Flexi Cap Fund" not in page.split("Redeemed units")[0]    # no longer held ...
    assert "Synthetic Flexi Cap Fund" in page.split("Redeemed units")[1]        # ... but still listed as redeemed
    assert client.post(f"/review/{dup_id}/duplicate/merge").status_code == 404   # already resolved
    assert "nothing new found" in client.post("/review/scan-duplicates").text


def test_duplicate_transactions_cleanup_from_the_ui(client, db_path, tmp_path, monkeypatch):
    from finance import analytics
    from finance.db import connect_ro
    from tests.test_duplicates import seed_same_account_duplicates
    monkeypatch.setattr(webapp, "BACKUP_DIR", str(tmp_path / "bk"))
    seed_same_account_duplicates(db_path)
    authed(client)
    assert "Duplicate transactions" in client.get("/review").text
    item = next(i for i in analytics.open_review_items(connect_ro(db_path)) if i["kind"] == "duplicate_txns")
    r = client.post(f"/review/{item['id']}/duplicate-txns")
    assert "Removed 3 duplicate transaction(s)" in r.text and list((tmp_path / "bk").glob("finance-*.db"))
    assert client.post(f"/review/{item['id']}/duplicate-txns").status_code == 404


def test_merge_all_duplicates_button(client, db_path, tmp_path, monkeypatch):
    from finance import analytics
    from finance.db import connect_ro
    from tests.test_duplicates import ALT, _imp
    monkeypatch.setattr(webapp, "BACKUP_DIR", str(tmp_path / "bk"))
    _imp(db_path, make_cas(), "a"); _imp(db_path, make_cas(folio=ALT), "b")                 # folio X, spelled twice
    _imp(db_path, make_cas(folio="7777777777", variant=2), "c")
    _imp(db_path, make_cas(folio="7777777777 / 0", variant=2), "d")                         # folio Y, spelled twice
    authed(client)
    page = client.get("/review").text
    assert "Merge all 2" in page
    r = client.post("/review/merge-all-duplicates")
    assert "Merged 2 folio pair(s)" in r.text and list((tmp_path / "bk").glob("finance-*.db"))
    assert connect_ro(db_path).execute("select count(*) from accounts").fetchone()[0] == 2
    assert not [i for i in analytics.open_review_items(connect_ro(db_path)) if i["kind"] == "duplicate_folio"]


def test_portfolio_warns_when_duplicates_make_totals_unreliable(client, db_path):
    from tests.test_duplicates import _scenario
    _scenario(db_path)
    authed(client)
    assert "not reliable yet" in client.get("/portfolio").text                      # flagged at import time
    led = __import__("finance.ledger", fromlist=["Ledger"]).Ledger(db_path); led.merge_all_duplicates(); led.close()
    assert "not reliable yet" not in client.get("/portfolio").text


def test_startup_scan_flags_but_changes_no_data(db_path, monkeypatch):
    from finance.db import connect_ro
    from finance.ledger import Ledger
    from tests.test_duplicates import seed_same_account_duplicates
    import finance.web.server as server
    seed_same_account_duplicates(db_path)
    led = Ledger(db_path); led.conn.execute("DELETE FROM review_queue"); led.conn.commit(); led.close()   # as in an old DB
    before = connect_ro(db_path).execute("select count(*) from transactions").fetchone()[0]
    monkeypatch.setattr("config.DB_PATH", db_path)
    server.scan_for_duplicates()
    conn = connect_ro(db_path)
    assert conn.execute("select count(*) from transactions").fetchone()[0] == before
    assert conn.execute("select count(*) from review_queue where kind='duplicate_txns' and status='open'").fetchone()[0] == 1


def test_portfolio_page_explains_incomplete_history(client, db_path):
    from finance.ledger import Ledger
    from tests.conftest import make_summary
    led = Ledger(db_path)
    led.import_statement(CasParser().from_casdata(make_cas(schemes=("A",))), sha256="d")
    led.import_statement(CasParser().from_casdata(make_summary(folios=[
        ("1234567890", [("INF000SYN001", "Synthetic Flexi Cap Fund - Direct Growth", "140", "250", None)])])), sha256="s")
    led.close()
    authed(client)
    page = client.get("/portfolio").text
    assert "incomplete history" in page and "0 of 1 funds" in page and "Invested excludes 1 fund" in page
    assert "Transactions explain 115.000 of 140.000 units" in page


def test_documents_show_provider_and_mailbox_name_not_folder_and_uid(client, db_path):
    from finance.ledger import Ledger
    from finance.mailconfig import MailConfig, MailboxAccount, SenderRule, save_config
    save_config(MailConfig(accounts=[
        MailboxAccount(name="gmail-main", provider="gmail", username="me@example.invalid",
                       senders=[SenderRule(match="camsonline.com")]),
        MailboxAccount(name="old-yahoo", provider="yahoo", username="old@example.invalid", folder="Statements",
                       senders=[SenderRule(match="kfintech.com")])]), str(client.cfg_path))
    led = Ledger(db_path)
    for sha, acct, folder in (("g", "gmail-main", "INBOX"), ("y", "old-yahoo", "Statements")):
        led.record_attention(sha256=sha, message_id=f"<{sha}@x>", sender="s@x.com", parser="cas", reason="r",
                             meta={"account": acct, "folder": folder, "uid": 5, "subject": f"mail {sha}"})
    led.close()
    authed(client)
    page = client.get("/documents").text
    assert "Gmail · gmail-main" in page and "Yahoo Mail · old-yahoo" in page
    assert "Gmail · me@example.invalid (gmail-main) · Inbox" in page          # details: provider, address, friendly folder
    assert "Yahoo Mail · old@example.invalid (old-yahoo) · Statements" in page
    assert "Folder: INBOX" not in page and "Technical: IMAP UID 5" in page
    assert '<option value="gmail-main" >Gmail · gmail-main</option>' in page or "Gmail · gmail-main</option>" in page


def test_reports_chat_and_the_old_profile_page_are_not_part_of_the_app(client):
    authed(client)
    for path in ("/profile", "/actions", "/chat", "/reports/report.html", "/chat/reset"):
        assert client.get(path).status_code in (404, 405), path
    assert client.post("/actions/run/data").status_code in (404, 405) and client.post("/chat/send", data={"message": "x"}).status_code in (404, 405)
    nav = client.get("/").text
    assert 'href="/identity"' in nav and 'href="/actions"' not in nav and 'href="/chat"' not in nav and 'href="/profile"' not in nav
    assert "Reports" not in nav and ">Chat<" not in nav


# ── Identity page: personal details only ────────────────────────────────────

def test_identity_page_is_personal_details_only_and_never_shows_a_stored_value(client):
    from finance import secrets
    authed(client)
    page = client.get("/identity").text
    assert "<h1>Identity</h1>" in page and "Personal details" in page
    for gone in ("Investor profile", "risk_appetite", "Risk profile", "Monthly SIP budget", "horizon_years", "Goal"):
        assert gone not in page, gone
    r = client.post("/identity", data={"dob": "1990-05-17", "pan": "ABCDE1234F"})
    assert r.status_code == 303 and secrets.get_secret("id:pan") == "ABCDE1234F" and secrets.get_secret("id:dob") == "1990-05-17"
    page = client.get("/identity").text
    assert "stored ✓" in page and "1990-05-17" not in page and "ABCDE1234F" not in page


def test_a_future_date_of_birth_is_refused_and_nothing_is_saved(client):
    from finance import secrets
    authed(client)
    r = client.post("/identity", data={"dob": "2999-01-01", "pan": "ZZZZZ9999Z"})
    assert r.status_code == 200 and "valid date in the past" in r.text
    assert secrets.get_secret("id:pan") is None and secrets.get_secret("id:dob") is None


def test_no_profile_file_or_module_is_part_of_the_app():
    import importlib.util
    assert importlib.util.find_spec("finance.profile") is None
    import config
    assert not hasattr(config, "PROFILE_PATH") and not hasattr(config, "USER_PROFILE")


# ── Mailboxes: no default-templates setting ──────────────────────────────────

def test_mailboxes_page_has_no_default_templates_setting(client):
    authed(client)
    page = client.get("/mailboxes").text
    assert "Default password templates" not in page and "/mailboxes/defaults" not in page
    assert client.post("/mailboxes/defaults", data={"templates": "{pan}"}).status_code in (404, 405)


def test_an_old_mailboxes_file_with_default_templates_still_loads(client):
    client.cfg_path.write_text('{"accounts": [], "default_templates": ["{password}", "{pan}"]}')
    authed(client)
    assert client.get("/mailboxes").status_code == 200


# ── the Review menu entry appears only while something needs a decision ───────

def test_review_is_in_the_menu_only_while_there_is_something_to_review(client, db_path):
    from finance.ledger import Ledger
    authed(client)
    assert 'href="/review"' not in client.get("/").text and 'href="/review"' not in client.get("/portfolio").text
    assert client.get("/review").status_code == 200                                                  # still reachable by its address
    led = Ledger(db_path)
    led.add_review("needs_attention", "x", "something is waiting")
    led.add_review("needs_attention", "y", "and another")
    led.close()
    page = client.get("/").text
    assert 'href="/review"' in page and 'class="badge">2<' in page                                   # with how many
    item = Ledger(db_path)
    ids = [r["id"] for r in item.conn.execute("SELECT id FROM review_queue WHERE status='open'")]
    for i in ids:
        item.resolve_review(i)
    item.close()
    assert 'href="/review"' not in client.get("/").text                                              # gone again when resolved


def test_the_review_page_keeps_its_own_menu_entry_even_when_empty(client):
    authed(client)
    page = client.get("/review").text
    assert 'href="/review"' in page and "badge" not in page.split("<main>")[0]


# ── the generic Import page ───────────────────────────────────────────────────

def _card_pdf(**kw):
    from tests.cardfix import crow, icici_card
    return icici_card("2000.00", [crow("02/10/2026", "SHOP", "100"), crow("06/10/2026", "Payment received", "500", credit=True)], **kw)


def test_import_page_is_generic_and_lists_every_statement_type(client):
    authed(client)
    page = client.get("/import").text
    assert "Import a statement" in page and "Import CAS statement" not in page
    for label in ("Bank statement", "Consolidated CAS", "Credit card", "EPF (not supported yet)", "NPS"):
        assert label in page
    assert "NPS (not supported yet)" not in page and page.count("<option") == 5 and "Detect automatically" not in page
    assert '<option value="bank" selected>' in page


def test_import_detects_a_card_statement(client):
    authed(client)
    r = client.post("/import", files={"file": ("c.pdf", _card_pdf(), "application/pdf")}, data={"parser": "card"})
    assert "Imported as <b>ICICI credit card</b>" in r.text and "2 new transactions" in r.text
    assert "Accounts" in client.get("/accounts").text and "…1234" in client.get("/accounts").text
    assert "already imported" in client.post("/import", files={"file": ("c.pdf", _card_pdf(), "application/pdf")},
                                             data={"parser": "card"}).text




def test_wrong_type_or_unbalanced_statement_is_refused_with_the_reason(client, monkeypatch):
    monkeypatch.setattr(REGISTRY["cas"], "parse", lambda path, password: (_ for _ in ()).throw(ValueError("not a CAS")))
    authed(client)
    wrong = client.post("/import", files={"file": ("c.pdf", _card_pdf(), "application/pdf")}, data={"parser": "bank"})
    assert "Import failed" in wrong.text and "no account transactions found; is this an ICICI account statement?" in wrong.text
    from tests.cardfix import crow, icici_card
    bad = icici_card("2000.00", [crow("02/10/2026", "SHOP", "100")], total="999.00")
    typed = client.post("/import", files={"file": ("c.pdf", bad, "application/pdf")}, data={"parser": "card"})
    assert "does not add up" in typed.text                             # shows the card parser that recognised it
    nothing = client.post("/import", files={"file": ("c.pdf", b"%PDF-1.4 hello", "application/pdf")}, data={"parser": "card"})
    assert "not recognised as the selected statement type (Credit card)" in nothing.text


def test_locked_statement_opens_with_a_typed_password_that_is_never_echoed(client):
    from tests.pdffix import encrypt
    authed(client)
    locked = encrypt(_card_pdf(), "typed-secret-1")
    no_pw = client.post("/import", files={"file": ("c.pdf", locked, "application/pdf")}, data={"parser": "card"})
    assert "password-protected" in no_pw.text and "Type the PDF password" in no_pw.text
    ok = client.post("/import", files={"file": ("c.pdf", locked, "application/pdf")}, data={"password": "typed-secret-1", "parser": "card"})
    assert "Imported as" in ok.text and "typed-secret-1" not in ok.text


def test_epf_is_listed_but_refused_until_supported(client):
    authed(client)
    r = client.post("/import", files={"file": ("c.pdf", _card_pdf(), "application/pdf")}, data={"parser": "epf"})
    assert "EPF statements are not supported yet" in r.text
    r = client.post("/import", files={"file": ("c.pdf", _card_pdf(), "application/pdf")}, data={"parser": "nps"})
    assert "Import failed" in r.text and "layout not recognised" in r.text            # a card PDF is not an NPS statement


def test_an_unknown_statement_type_falls_back_to_bank_and_cannot_pick_a_parser(client):
    authed(client)
    r = client.post("/import", files={"file": ("c.pdf", _card_pdf(), "application/pdf")}, data={"parser": "../../etc"})
    assert "Import failed" in r.text and "is this an ICICI account statement?" in r.text


def test_import_history_lists_only_manual_uploads(client, db_path, tmp_path):
    from finance import analytics
    from finance.db import connect_ro
    from finance.ledger import Ledger
    from finance.parsers.icici_card import IciciCardParser
    from tests.cardfix import crow, icici_card
    authed(client)
    client.post("/import", files={"file": ("c.pdf", _card_pdf(), "application/pdf")}, data={"parser": "card"})
    mailed = tmp_path / "m.pdf"
    mailed.write_bytes(icici_card("9000.00", [crow("03/11/2026", "OTHER", "10")], card="4315XXXXXXXX9999",
                                  period=("November 01, 2026", "November 30, 2026"), due="December 18, 2026"))
    led = Ledger(db_path)
    led.import_statement(IciciCardParser().parse(mailed, None), sha256="e" * 64, source="imap", message_id="<m@x>",
                         meta={"account": "gmail"})
    led.close()
    conn = connect_ro(db_path)
    assert len(analytics.list_documents(conn)) == 2 and len(analytics.list_documents(conn, source="upload")) == 1
    page = client.get("/import").text
    assert "Manual import history" in page and page.count("<tr><td>") == 1          # the mailed statement is not listed


@pytest.mark.parametrize("issuer, label", [("icici_card", "ICICI credit card"), ("hdfc_card", "HDFC credit card"),
                                           ("axis_card", "Axis credit card"), ("tata_card", "Tata credit card")])
def test_credit_card_type_tries_every_issuer(client, issuer, label):
    from tests import cardfix
    authed(client)
    pdf = getattr(cardfix, issuer)("2000.00", [cardfix.crow("02/10/2026", "SHOP", "100")])
    r = client.post("/import", files={"file": ("c.pdf", pdf, "application/pdf")}, data={"parser": "card"})
    assert f"Imported as <b>{label}</b>" in r.text


def test_portfolio_page_has_a_redeemed_units_section_and_no_ledger_disclaimer(client):
    authed(client)
    client.post("/import", files={"file": ("cas.pdf", b"%PDF-1.4 fake", "application/pdf")}, data={"parser": "cas"})
    home = client.get("/portfolio").text
    assert "Redeemed units" in home and "Synthetic Liquid Fund" in home and "Fully redeemed" in home
    assert "not the LLM" not in home and "XIRR covers current holdings" in home


# ── several files at once ─────────────────────────────────────────────────────

def _upload(*items):
    return [("file", (name, data, "application/pdf")) for name, data in items]


def _cards():
    from tests.cardfix import crow, icici_card
    kw = dict(due="December 18, 2026")
    oct_ = icici_card("0.00", [crow("02/10/2026", "SHOP ONE", "100")], **kw)
    nov = icici_card("100.00", [crow("03/11/2026", "SHOP TWO", "50")], period=("November 01, 2026", "November 30, 2026"), **kw)
    return oct_, nov


def test_import_page_accepts_several_files(client):
    authed(client)
    assert "multiple" in client.get("/import").text


def test_several_statements_import_together_and_repeats_are_recognised(client):
    authed(client)
    oct_, nov = _cards()
    r = client.post("/import", files=_upload(("oct.pdf", oct_), ("nov.pdf", nov), ("oct-again.pdf", oct_)), data={"parser": "card"})
    assert "2 imported, 1 already imported, 0 failed" in r.text and "(of 3 files)" in r.text
    assert "<b>oct.pdf</b>" in r.text and "<b>nov.pdf</b>" in r.text and "<b>oct-again.pdf</b>" in r.text
    assert r.text.count("This exact file was already imported") == 1
    assert "SHOP ONE" in client.get("/transactions?q=SHOP").text and "SHOP TWO" in client.get("/transactions?q=SHOP").text


def test_a_bad_file_does_not_stop_the_others(client):
    authed(client)
    oct_, nov = _cards()
    r = client.post("/import", files=_upload(("oct.pdf", oct_), ("notes.pdf", b"hello"), ("nov.pdf", nov)), data={"parser": "card"})
    assert "2 imported, 0 already imported, 1 failed" in r.text
    assert "<b>notes.pdf</b> — Not a PDF" in r.text and "SHOP TWO" in client.get("/transactions?q=SHOP").text


def test_locked_files_share_the_typed_password_and_it_is_never_echoed(client):
    from tests.pdffix import encrypt
    authed(client)
    oct_, nov = _cards()
    files = _upload(("oct.pdf", encrypt(oct_, "typed-secret-2")), ("nov.pdf", encrypt(nov, "typed-secret-2")))
    r = client.post("/import", files=files, data={"parser": "card", "password": "typed-secret-2"})
    assert "2 imported, 0 already imported, 0 failed" in r.text and "typed-secret-2" not in r.text


def test_a_file_with_the_wrong_password_fails_alone(client):
    from tests.pdffix import encrypt
    authed(client)
    oct_, nov = _cards()
    files = _upload(("oct.pdf", encrypt(oct_, "typed-secret-2")), ("nov.pdf", nov))
    r = client.post("/import", files=files, data={"parser": "card", "password": "typed-secret-2"})
    assert "2 imported" in r.text                                     # an unencrypted file ignores the password
    r = client.post("/import", files=_upload(("locked.pdf", encrypt(oct_, "other"))), data={"parser": "card", "password": "wrong"})
    assert "password-protected" in r.text


def test_at_most_25_files_are_taken_per_upload(client):
    authed(client)
    r = client.post("/import", files=_upload(*[(f"f{i}.pdf", b"hello") for i in range(26)]), data={"parser": "card"})
    assert "Only the first 25 files were imported" in r.text and "(of 26 files)" in r.text


def test_nps_statements_import_together(client):
    from tests.npsfix import NAMES, nps_statement, nrow, nscheme
    authed(client)
    one = [nscheme(NAMES[0], "0", "50.0000", [nrow("05-Jan-26", "5000.00", "50.0000")])]
    two = [nscheme(NAMES[0], "100.0000", "55.0000", [nrow("05-Feb-26", "5500.00", "55.0000", month="February,2026")])]
    r = client.post("/import", data={"parser": "nps"}, files=_upload(
        ("jan.pdf", nps_statement(one)), ("feb.pdf", nps_statement(two, period=("Feb 01, 2026", "Feb 28, 2026")))))
    assert "2 imported, 0 already imported, 0 failed" in r.text


def test_times_are_sent_as_utc_for_the_browser_to_show_in_the_viewers_own_timezone(client):
    authed(client)
    oct_, _ = _cards()
    client.post("/import", files=_upload(("oct.pdf", oct_)), data={"parser": "card"})
    for page in ("/import", "/documents"):
        html = client.get(page).text
        assert 'class="ts" datetime="20' in html and 'Z">' in html            # ISO UTC; base.html rewrites it per locale and timezone
    assert "toLocaleString([]" in html                                           # no fixed timezone or locale: the browser's own


def test_identity_has_one_generic_password_field_not_a_cas_one(client):
    authed(client)
    page = client.get("/identity").text
    assert "Default PDF password" in page and "CAS PDF" not in page and 'name="password"' in page


def test_transactions_can_be_sorted_and_the_choice_survives_the_account_tabs(client, db_path):
    from decimal import Decimal
    from finance import analytics
    from finance.db import connect_ro
    authed(client)
    from tests.cardfix import crow, icici_card
    stmt = icici_card("2000.00", [crow("02/10/2026", "SHOP ONE", "1200.50"), crow("05/10/2026", "COFFEE", "300"),
                                  crow("06/10/2026", "Payment received", "1500", credit=True), crow("20/10/2026", "BOOKS", "999.99")])
    client.post("/import", files=_upload(("oct.pdf", stmt)), data={"parser": "card"})
    conn = connect_ro(db_path)
    rows = lambda sort: analytics.search_transactions(conn, account_type="card", sort=sort)
    asc, desc = [r["date"] for r in rows("date_asc")], [r["date"] for r in rows("date_desc")]
    assert len(asc) > 2 and asc == sorted(asc) and desc == sorted(desc, reverse=True)
    big, small = [abs(Decimal(r["amount"])) for r in rows("amount_desc")], [abs(Decimal(r["amount"])) for r in rows("amount_asc")]
    assert big == sorted(big, reverse=True) and small == sorted(small) and big[0] == small[-1] and big[0] != big[-1]
    names = [(r["description"] or "").lower() for r in rows("desc_asc")]
    assert names == sorted(names)
    conn.close()
    page = client.get("/transactions?sort=amount_asc").text
    assert 'value="amount_asc" selected' in page and "sort=amount_asc" in page            # chosen in the form and kept by the tabs
    assert 'value="date_desc" selected' in client.get("/transactions?sort=nonsense").text   # an unknown choice falls back


def test_a_post_with_no_origin_or_referer_is_rejected(client):
    authed(client)
    client.headers.pop("origin")
    assert client.post("/mailboxes/gmail-main/sync").status_code == 403 and client.jobs.started == []
