import sqlite3

from finance import analytics
from finance.db import connect_ro
from finance.ledger import Ledger
from finance.redact import redact
from finance.sources.imap import auth_summary
from tests.test_sync import IDENT, app_password, runner, seen_passwords  # noqa: F401  (fixtures)
from tests.mailfix import make_email, make_pdf


def _docs(db):
    return [dict(r) for r in analytics.list_documents(connect_ro(db))]


def test_redact_masks_identifiers():
    out = redact("Statement for Test Person - ABCDE1234F - mobile 98765 43210 - acct 123456789012",
                 name="Test Person", pan="ABCDE1234F", mobile="+91 98765 43210")
    for leaked in ("Test", "Person", "ABCDE1234F", "98765", "43210", "123456789012"):
        assert leaked not in out
    assert out.count("•••") >= 4 and "Statement for" in out
    assert redact("xyz" * 100, limit=50) == ("xyz" * 100)[:50]
    assert redact("plain statement") == "plain statement"
    assert "ZZZZZ9999Z" not in redact("pan ZZZZZ9999Z")                    # generic PAN shape, even if unknown to us


def test_auth_summary():
    assert auth_summary(["mx.google.com; dkim=pass header.i=@x; spf=pass; dmarc=pass"]) == "dkim=pass spf=pass dmarc=pass"
    assert auth_summary(["a; spf=softfail", "b; spf=pass"]) == "spf=softfail"       # first verdict per mechanism wins
    assert auth_summary([]) == ""


def test_email_details_are_recorded_and_masked(db_path, seen_passwords, tmp_path):
    msgs = {7: make_email(pdf=make_pdf("s3cret!"), msgid="<abc@x>",
                          subject="CAS for Test Person ABCDE1234F", filename="ABCDE1234F_CAS.pdf",
                          date="Mon, 02 Jun 2025 10:00:00 +0530")}
    runner(db_path, msgs, inbox=str(tmp_path / "inbox")).run()
    d = _docs(db_path)[0]
    assert d["account"] == "gmail-main" and d["folder"] == "INBOX" and d["uid"] == 7
    assert d["sender"] == "donotreply@camsonline.com" and d["message_id"] == "<abc@x>"
    assert d["received_at"] == "2025-06-02 04:30:00"                       # +05:30 converted to UTC
    assert d["picked_up_at"] and d["processed_at"] and d["auth"] == "spf=pass dkim=pass"
    assert d["size"] > 0 and d["status"] == "imported" and d["period_to"] == "2025-01-31"
    assert d["subject"] == "CAS for ••• •••" or ("Test" not in d["subject"] and "ABCDE" not in d["subject"])
    assert "ABCDE1234F" not in d["filename"] and d["filename"].endswith("_CAS.pdf")
    dump = "\n".join(sqlite3.connect(db_path).iterdump())
    assert "Test Person" not in dump and "ABCDE1234F" not in dump


def test_retry_keeps_original_pickup_time_and_email_details(db_path, seen_passwords, tmp_path):
    inbox = str(tmp_path / "inbox")
    msgs = {3: make_email(pdf=make_pdf("s3cret!"), subject="Monthly statement")}
    runner(db_path, msgs, identity={}, inbox=inbox).run()                    # locked: needs attention
    before = _docs(db_path)[0]
    assert before["status"] == "needs_attention" and before["subject"] == "Monthly statement"
    led = Ledger(db_path)
    led.conn.execute("UPDATE source_documents SET picked_up_at='2025-06-03 01:00:00'"); led.conn.commit(); led.close()
    assert runner(db_path, {}, identity=IDENT, inbox=inbox).retry(before["id"]) == "imported"
    after = _docs(db_path)
    assert len(after) == 1 and after[0]["status"] == "imported"
    assert after[0]["picked_up_at"] == "2025-06-03 01:00:00"               # not reset by the retry
    assert (after[0]["subject"], after[0]["uid"], after[0]["received_at"]) == ("Monthly statement", 3, "2025-06-02 04:30:00")


def test_rescan_backfills_documents_stored_before_details_existed(db_path, seen_passwords):
    msgs = {4: make_email(pdf=make_pdf("s3cret!"), msgid="<old@x>", subject="Old statement")}
    runner(db_path, msgs).run()
    led = Ledger(db_path)                                                    # simulate a phase-2 era row
    led.conn.execute("UPDATE source_documents SET subject='', received_at=NULL, uid=0, account='', filename='', size=0, auth=''")
    led.reset_imap_state("gmail-main"); led.conn.commit(); led.close()
    s = runner(db_path, msgs).run().accounts[0]
    assert s.duplicate == 1 and s.imported == 0                              # nothing re-imported...
    d = _docs(db_path)[0]
    assert (d["subject"], d["uid"], d["received_at"], d["account"]) == ("Old statement", 4, "2025-06-02 04:30:00", "gmail-main")
    assert connect_ro(db_path).execute("select count(*) from transactions").fetchone()[0] == 5


def test_rejected_mail_review_item_names_mailbox_and_date(db_path, seen_passwords):
    runner(db_path, {1: make_email(pdf=make_pdf("s3cret!"), auth="mx; dkim=fail")}).run()
    reason = analytics.open_review_items(connect_ro(db_path))[0]["reason"]
    assert "gmail-main" in reason and "2025-06-02" in reason and "dkim=fail" in reason


def test_schema_v2_database_upgrades_to_v3(tmp_path):
    from finance import db
    path = str(tmp_path / "v2.db")
    raw = sqlite3.connect(path)
    raw.executescript(db.SCHEMA); raw.executescript(db.MIGRATIONS[2]); raw.execute("PRAGMA user_version = 2")
    raw.execute("INSERT INTO source_documents (source, sha256, status, created_at) VALUES ('imap','h','imported','2025-01-02 03:04:05')")
    raw.commit(); raw.close()
    conn = db.connect_rw(path)
    row = conn.execute("SELECT picked_up_at, processed_at, subject, uid FROM source_documents").fetchone()
    assert tuple(row) == ("2025-01-02 03:04:05", "2025-01-02 03:04:05", "", 0)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
