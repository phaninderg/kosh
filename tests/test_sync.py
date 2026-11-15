from datetime import date

import pytest

from finance import analytics
from finance.db import connect_ro
from finance.ledger import Ledger
from finance.mailconfig import MailConfig, MailboxAccount, SenderRule
from finance.parsers.base import REGISTRY
from finance.parsers.cas import CasParser
from finance.passwords import Identity
from finance.sources.imap import ImapSource
from finance.sync import SyncRunner
from tests.conftest import make_cas
from tests.mailfix import FakeImap, make_email, make_pdf

IDENT = {"name": "Test Person", "dob": "1990-05-17", "pan": "ABCDE1234F", "password": "s3cret!"}
APP_PW = "app-password-xyz"


def account(name="gmail-main", senders=None, **kw):
    return MailboxAccount(name=name, provider="gmail", username="me@example.invalid",
                          senders=senders or [SenderRule(match="camsonline.com", parser="cas")], **kw)


@pytest.fixture(autouse=True)
def app_password(monkeypatch):
    monkeypatch.setattr("finance.sources.imap.get_secret", lambda key: APP_PW)


@pytest.fixture
def seen_passwords(monkeypatch):
    seen = []
    def fake_parse(path, password):
        assert path.exists()
        seen.append(password)
        return CasParser().from_casdata(make_cas())
    monkeypatch.setattr(REGISTRY["cas"], "parse", fake_parse, raising=False)
    return seen


def runner(db, msgs, identity=IDENT, accounts=None, inbox=None, servers=None):
    cfg = MailConfig(accounts=accounts or [account()])
    servers = servers if servers is not None else []

    def factory(acct, state):
        def connect(host, port):
            srv = FakeImap(msgs)
            servers.append(srv)
            return srv
        return ImapSource(acct, state, connect=connect, today=lambda: date(2025, 6, 1))
    kw = {"inbox_dir": inbox} if inbox else {}
    return SyncRunner(db, cfg, factory, Identity(identity), log=lambda m: None, **kw)


# ── Mailbox agent: read-only, allowlisted, incremental ───────────────────────

def test_end_to_end_cas_from_encrypted_email(db_path, seen_passwords, tmp_path):
    msgs = {1: make_email(pdf=make_pdf("s3cret!"))}
    servers = []
    summary = runner(db_path, msgs, servers=servers, inbox=str(tmp_path / "inbox")).run()
    a = summary.accounts[0]
    assert (a.imported, a.attention, a.error) == (1, 0, "") and a.txn_new == 5
    assert seen_passwords == ["s3cret!"]                       # decrypted password reached the parser, in memory
    assert len(analytics.portfolio(connect_ro(db_path)).rows) == 1
    log = servers[0].log
    assert servers[0].readonly_select is True and any(l.startswith("examine") for l in log)
    assert not (tmp_path / "inbox").exists() or not list((tmp_path / "inbox").glob("*.pdf"))


def test_second_sync_fetches_only_new_mail(db_path, seen_passwords):
    msgs = {1: make_email(pdf=make_pdf("s3cret!"))}
    runner(db_path, msgs).run()
    msgs[2] = make_email(pdf=make_pdf("s3cret!"))
    servers = []
    s = runner(db_path, msgs, servers=servers).run().accounts[0]
    assert s.messages == 1                                     # only uid 2 was loaded
    assert not any(l == "fetch 1" for l in servers[0].log)
    assert any("UID 2:*" in l for l in servers[0].log)


def test_first_sync_uses_lookback_window(db_path, seen_passwords):
    servers = []
    runner(db_path, {1: make_email(pdf=make_pdf("s3cret!"))}, servers=servers).run()
    assert any("SINCE 01-Jun-2024" in l for l in servers[0].log)    # 365 days before the fixed "today"


def test_reset_state_reimport_is_idempotent(db_path, seen_passwords):
    msgs = {1: make_email(pdf=make_pdf("s3cret!"), msgid="<fixed@x>")}
    runner(db_path, msgs).run()
    led = Ledger(db_path); led.conn.execute("DELETE FROM imap_state"); led.conn.commit(); led.close()
    s = runner(db_path, msgs).run().accounts[0]
    assert (s.imported, s.duplicate) == (0, 1)
    assert connect_ro(db_path).execute("select count(*) from transactions").fetchone()[0] == 5


def test_uidvalidity_change_restarts_safely(db_path, seen_passwords):
    msgs = {5: make_email(pdf=make_pdf("s3cret!"), msgid="<a@x>")}
    runner(db_path, msgs).run()
    servers = []
    cfg_runner = runner(db_path, msgs, servers=servers)
    orig = cfg_runner.source_factory
    cfg_runner.source_factory = lambda a, st: _with_validity(orig(a, st), 99)
    s = cfg_runner.run().accounts[0]
    assert s.duplicate == 1 and s.imported == 0


def _with_validity(src, v):
    real = src._connect
    src._connect = lambda h, p: _set(real(h, p), v)
    return src


def _set(srv, v):
    srv.uidvalidity = v
    return srv


def test_allowlist_is_strict_and_spoofs_are_ignored(db_path, seen_passwords):
    msgs = {
        1: make_email(sender="x@camsonline.com.evil.net", pdf=make_pdf("s3cret!")),
        2: make_email(sender="x@notcamsonline.com", pdf=make_pdf("s3cret!")),
        3: make_email(sender="friend@gmail.com", pdf=make_pdf("s3cret!")),
    }
    s = runner(db_path, msgs).run().accounts[0]
    assert s.imported == 0 and s.attention == 0 and seen_passwords == []


def test_failed_sender_authentication_is_rejected_and_flagged(db_path, seen_passwords):
    msgs = {1: make_email(pdf=make_pdf("s3cret!"), auth="mx.google.com; dkim=fail spf=pass")}
    s = runner(db_path, msgs).run().accounts[0]
    assert (s.rejected, s.imported) == (1, 0)
    assert [i["kind"] for i in analytics.open_review_items(connect_ro(db_path))] == ["rejected_mail"]


@pytest.mark.parametrize("auth, rejected", [
    ("mx.google.com; dkim=fail spf=pass dmarc=pass", False),     # DMARC passed: SPF aligned, so the sender is verified
    ("mx.google.com; dkim=pass spf=pass dmarc=fail", True),       # DMARC failed: not the domain's own mail, whatever else passed
    ("mx.google.com; dkim=fail spf=pass", True),                  # no DMARC result: any explicit failure still rejects
    ("mx.google.com; dkim=pass spf=fail", True),
    ("mx.google.com; dkim=pass spf=pass dmarc=pass", False),
    ("mx.google.com; dkim=fail spf=fail dmarc=bestguesspass", False),
    ("mx.google.com; dkim=none spf=pass dmarc=none", False),
])
def test_dmarc_decides_when_present_else_any_explicit_failure_rejects(db_path, seen_passwords, auth, rejected):
    s = runner(db_path, {1: make_email(pdf=make_pdf("s3cret!"), auth=auth)}).run().accounts[0]
    assert (s.rejected, s.messages) == (1 if rejected else 0, 1)


def test_mail_without_pdf_is_skipped_quietly(db_path, seen_passwords):
    s = runner(db_path, {1: make_email(pdf=None)}).run().accounts[0]
    assert (s.messages, s.imported, s.attention) == (1, 0, 0)


def test_write_commands_are_impossible():
    srv = FakeImap({})
    for cmd in ("store", "copy", "expunge", "delete", "append"):
        with pytest.raises(AssertionError):
            getattr(srv, cmd)


# ── Decrypt + needs-attention + retry ────────────────────────────────────────

def test_locked_pdf_goes_to_attention_then_retry_succeeds(db_path, seen_passwords, tmp_path):
    inbox = str(tmp_path / "inbox")
    msgs = {1: make_email(pdf=make_pdf("s3cret!"))}
    s = runner(db_path, msgs, identity={"name": "Test Person"}, inbox=inbox).run().accounts[0]
    assert (s.imported, s.attention) == (1 - 1, 1)
    items = analytics.open_review_items(connect_ro(db_path))
    assert items[0]["kind"] == "needs_attention" and "password" in items[0]["reason"]
    assert len(list((tmp_path / "inbox").glob("*.pdf"))) == 1       # encrypted copy kept for retry
    doc_id = int(items[0]["ref"].split(":")[1])

    # the user fixes the secret, then retries from the saved PDF — no network involved
    r = runner(db_path, {}, identity=IDENT, inbox=inbox)
    assert r.retry(doc_id) == "imported"
    assert analytics.open_review_items(connect_ro(db_path)) == []
    assert list((tmp_path / "inbox").glob("*.pdf")) == []
    assert len(analytics.portfolio(connect_ro(db_path)).rows) == 1


def test_attention_is_not_requeued_every_sync(db_path, seen_passwords, tmp_path):
    msgs = {1: make_email(pdf=make_pdf("nobody-knows"))}
    r = runner(db_path, msgs, inbox=str(tmp_path / "inbox"))
    r.run(); r.run()
    n = connect_ro(db_path).execute("select count(*) from review_queue where kind='needs_attention'").fetchone()[0]
    assert n == 1


def test_parser_failure_goes_to_attention_and_dismiss_is_final(db_path, monkeypatch, tmp_path):
    def boom(path, password): raise RuntimeError("unrecognised layout")
    monkeypatch.setattr(REGISTRY["cas"], "parse", boom, raising=False)
    inbox = str(tmp_path / "inbox")
    r = runner(db_path, {1: make_email(pdf=make_pdf("s3cret!"))}, inbox=inbox)
    assert r.run().accounts[0].attention == 1
    item = analytics.open_review_items(connect_ro(db_path))[0]
    assert "unrecognised layout" in item["reason"]
    doc_id = int(item["ref"].split(":")[1])
    r.dismiss(doc_id)
    assert analytics.open_review_items(connect_ro(db_path)) == [] and list((tmp_path / "inbox").glob("*.pdf")) == []
    assert r.run().accounts[0].attention == 0                            # never nagged again


def test_working_template_is_cached_per_sender(db_path, seen_passwords):
    acct = account(senders=[SenderRule(match="camsonline.com", parser="cas", templates=["{pan}", "{name4}{ddmm}"])])
    runner(db_path, {1: make_email(pdf=make_pdf("test1705"))}, accounts=[acct]).run()
    assert Ledger(db_path).password_templates() == {"donotreply@camsonline.com": "{name4}{ddmm}"}


# ── resilience & secrecy ─────────────────────────────────────────────────────

def test_one_failing_mailbox_does_not_stop_others(db_path, seen_passwords, monkeypatch):
    good, bad = account("good"), account("bad")
    r = runner(db_path, {1: make_email(pdf=make_pdf("s3cret!"))}, accounts=[bad, good])
    inner = r.source_factory
    def factory(acct, state):
        if acct.name == "bad":
            src = inner(acct, state)
            src._connect = lambda h, p: (_ for _ in ()).throw(OSError("network down"))
            return src
        return inner(acct, state)
    r.source_factory = factory
    out = r.run()
    assert "cannot sign in" in out.accounts[0].error and out.accounts[1].imported == 1


def test_missing_app_password_is_a_clear_error(db_path, monkeypatch):
    monkeypatch.setattr("finance.sources.imap.get_secret", lambda key: None)
    out = runner(db_path, {}).run()
    assert "no app password stored" in out.accounts[0].error


def test_no_secret_ever_reaches_the_database_or_summary(db_path, seen_passwords, tmp_path):
    msgs = {1: make_email(pdf=make_pdf("s3cret!")), 2: make_email(pdf=make_pdf("locked-one"))}
    out = runner(db_path, msgs, inbox=str(tmp_path / "inbox")).run()
    dump = "\n".join(connect_ro(db_path).iterdump()) + out.render()
    assert "INSERT INTO" in dump                                                  # the dump is of a real ledger, not an empty file
    for secret in ("s3cret!", APP_PW, "ABCDE1234F", "me@example.invalid", "Test Person", "1990-05-17", "test1705"):
        assert secret not in dump, secret


# ── one-off / remembered passwords for statements requested with a custom password ──

def _queue_two_custom(db_path, tmp_path):
    inbox = str(tmp_path / "inbox")
    msgs = {1: make_email(pdf=make_pdf("custom-A")), 2: make_email(pdf=make_pdf("custom-A"))}
    r = runner(db_path, msgs, identity=IDENT, inbox=inbox)
    assert r.run().accounts[0].attention == 2
    ids = [int(i["ref"].split(":")[1]) for i in analytics.open_review_items(connect_ro(db_path))]
    return inbox, ids


def test_typed_password_unlocks_and_is_not_remembered_by_default(db_path, seen_passwords, tmp_path):
    from finance import secrets
    inbox, ids = _queue_two_custom(db_path, tmp_path)
    r = runner(db_path, {}, identity=IDENT, inbox=inbox)
    assert r.retry(ids[0], password="wrong") .startswith("still needs attention")
    assert r.retry(ids[0], password="custom-A") == "imported"
    assert seen_passwords[-1] == "custom-A"
    assert secrets.get_secret("id:extra_passwords") is None                 # one-off: nothing persisted
    dump = "\n".join(connect_ro(db_path).iterdump())
    assert "INSERT INTO" in dump                                                  # the dump is of a real ledger, not an empty file
    assert "custom-A" not in dump and "wrong" not in dump


def test_remembered_password_unlocks_future_statements_automatically(db_path, seen_passwords, tmp_path):
    from finance import secrets
    inbox, ids = _queue_two_custom(db_path, tmp_path)
    r = runner(db_path, {}, identity=IDENT, inbox=inbox)
    assert r.retry(ids[0], password="custom-A", remember=True) == "imported"
    assert "custom-A" in secrets.get_secret("id:extra_passwords")           # in the vault, nowhere else
    assert r.retry(ids[1]) == "imported"                                    # second doc: no typing needed
    new = runner(db_path, {5: make_email(pdf=make_pdf("custom-A"))}, identity=IDENT, inbox=inbox).run().accounts[0]
    assert new.imported == 1 and new.attention == 0                         # and so is a brand-new email


def test_failed_remember_does_not_store_a_wrong_password(db_path, seen_passwords, tmp_path):
    from finance import secrets
    inbox, ids = _queue_two_custom(db_path, tmp_path)
    runner(db_path, {}, identity=IDENT, inbox=inbox).retry(ids[0], password="typo", remember=True)
    assert secrets.get_secret("id:extra_passwords") is None


# ── sender authentication: only the receiving server's own verdict counts, and it must be about the From domain ──

@pytest.mark.parametrize("headers, rejected", [
    (["mx.google.com; dkim=pass header.i=@evil.example; spf=pass smtp.mailfrom=evil.example"], True),   # passes, but for the attacker's domain
    (["mx.google.com; dkim=pass header.d=bank.example"], False),                                          # passes for the From domain
    (["mx.google.com; spf=pass smtp.mailfrom=mail.bank.example"], False),                                 # a subdomain of it
    (["mx.google.com; spf=softfail"], True), (["mx.google.com; dkim=none spf=none"], True),               # nobody vouched for it
    ([], True),                                                                                           # no header at all
    (["mx.google.com; dkim=fail spf=fail", "attacker; dmarc=pass"], True),                                # a forged lower header is ignored
    (["mx.google.com; spf=softfail", "attacker; dkim=pass header.d=bank.example"], True),
    (["mx.google.com; dkim=pass header.i=@evil.example; dmarc=pass header.from=bank.example"], False),    # DMARC says it is aligned
])
def test_sender_authentication_needs_a_pass_for_the_from_domain(headers, rejected):
    from finance.sources.imap import auth_failed
    assert auth_failed(headers, "bank.example") is rejected


def test_the_imap_connection_verifies_the_servers_certificate(monkeypatch):
    import ssl
    from finance.sources import imap
    seen = {}
    monkeypatch.setattr(imap.imaplib, "IMAP4_SSL", lambda host, port, **kw: seen.update(kw))
    monkeypatch.setattr(imap.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 993))])
    imap._default_connect("imap.example", 993)
    ctx = seen["ssl_context"]
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


# ── hostile mail and unsafe servers ───────────────────────────────────────────────────

def _nested(depth: int) -> bytes:
    """A message from an allowed, authenticated sender whose MIME parts are nested `depth` deep."""
    head = (b"From: donotreply@camsonline.com\r\nSubject: x\r\nAuthentication-Results: mx.google.com; dmarc=pass\r\n"
            b"Content-Type: multipart/mixed; boundary=a\r\n\r\n")
    return head + b"--a\r\nContent-Type: multipart/mixed; boundary=a\r\n\r\n" * depth


def test_hostile_mail_is_rejected_once_and_never_blocks_the_mailbox(db_path, seen_passwords):
    from tests.mailfix import make_email
    msgs = {1: _nested(60), 2: _nested(3000), 3: make_email(pdf=make_pdf("s3cret!"))}      # too complex; crashes the parser; fine
    (acct,) = runner(db_path, msgs).run().accounts
    assert acct.rejected == 2 and acct.imported == 1 and not acct.error                    # the good message still came through
    assert runner(db_path, msgs).run().accounts[0].messages == 0                           # and the bad ones are never fetched again


def test_an_attachment_that_breaks_the_pdf_reader_is_queued_not_fatal(db_path, monkeypatch):
    from finance import passwords
    monkeypatch.setattr(passwords.DecryptAgent, "unlock", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = runner(db_path, {1: make_email(pdf=make_pdf("s3cret!"))}).run()
    assert out.accounts[0].attention == 1 and not out.accounts[0].error


@pytest.mark.parametrize("provider, host, serv, rejected", [
    ("gmail", None, "mx.google.com", False), ("gmail", None, "attacker.example", True), ("gmail", None, "", True),
    ("gmail", None, "mx.google.com.evil.example", True), ("yahoo", None, "mta1.mail.yahoo.com", False),
    ("custom", "imap.mybank-mail.example", "mx.mybank-mail.example", False), ("custom", "imap.mybank-mail.example", "mx.other.example", True),
])
def test_only_the_receiving_servers_own_authentication_header_is_believed(provider, host, serv, rejected):
    from finance.mailconfig import MailboxAccount
    from finance.sources.imap import auth_failed, trusted_authserv
    acct = MailboxAccount(name="m", provider=provider, username="u@x.invalid", host=host)
    header = f"{serv}; dmarc=pass header.from=camsonline.com"
    assert auth_failed([header], "camsonline.com", trusted_authserv(acct)) is rejected


@pytest.mark.parametrize("host, addresses", [("localhost", ["127.0.0.1"]), ("internal.corp", ["10.1.2.3"]), ("metadata", ["169.254.169.254"]),
                                             ("mixed", ["93.184.216.34", "192.168.0.5"]), ("v6", ["::1"])])
def test_imap_will_not_connect_to_private_or_internal_addresses(monkeypatch, host, addresses):
    import socket
    from finance.sources import imap
    from finance.sources.base import MailboxError
    monkeypatch.setattr(imap.socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, 1, 6, "", (a, 993)) for a in addresses])
    with pytest.raises(MailboxError, match="not a public mail server"):
        imap._default_connect(host, 993)


def test_rescan_reads_again_a_document_an_older_parser_revision_imported(db_path, seen_passwords):
    msgs = {1: make_email(pdf=make_pdf("s3cret!"), msgid="<fixed@x>")}
    runner(db_path, msgs).run()
    led = Ledger(db_path)
    led.conn.execute("UPDATE source_documents SET parse_rev=1"); led.conn.execute("DELETE FROM imap_state"); led.conn.commit(); led.close()
    s = runner(db_path, msgs).run().accounts[0]
    assert (s.imported, s.duplicate, s.attention) == (1, 0, 0)               # read again, not skipped as known
    s = runner(db_path, msgs).run().accounts[0]
    assert (s.imported, s.attention) == (0, 0)                               # and now current
    assert connect_ro(db_path).execute("select count(*) from transactions").fetchone()[0] == 5


def test_a_failed_reread_does_not_flag_an_earlier_good_import(db_path, seen_passwords):
    msgs = {1: make_email(pdf=make_pdf("s3cret!"), msgid="<fixed@x>")}
    runner(db_path, msgs).run()
    led = Ledger(db_path)
    led.conn.execute("UPDATE source_documents SET parse_rev=1"); led.conn.execute("DELETE FROM imap_state"); led.conn.commit(); led.close()
    import finance.sync as sync_mod
    real = sync_mod.SyncRunner._import
    sync_mod.SyncRunner._import = lambda self, ledger, stmt, doc: (_ for _ in ()).throw(ValueError("refused now"))
    try:
        s = runner(db_path, msgs).run().accounts[0]
    finally:
        sync_mod.SyncRunner._import = real
    assert s.attention == 0
    row = connect_ro(db_path).execute("select status from source_documents").fetchone()
    assert row["status"] == "imported"
