"""ICICI combined statement: parser, integrity gates, ledger import, transfer matching. Synthetic PDFs only."""

from datetime import date
from decimal import Decimal

import pytest

from finance import analytics
from finance.db import connect_ro
from finance.ledger import Ledger
from finance.models import AccountTxn, ParsedAccount, ParsedAccountStatement
from finance.parsers.icici import IciciStatementParser
from finance.reconcile import match_transfers
from tests.pdffix import FD_TEXT, encrypt, icici_statement, row, section

SAV = "XXXXXXXX1234"
PPF = "XXXXXXXX5678"


def savings_rows():
    return [
        row("01-10-2026", "-9000", head=["ACH/BD-SIPCO/ICIC700/TKO"], tail=["T8337919"]),
        # two rows whose wrapped lines sit exactly midway: nearest-line assignment would give the second
        # row's first header line to the first row
        row("05-10-2026", "200000", mode="NET BANKING", head=["ACME TRADERS LIMITED"], tail=["INF/INFT/046141613811/goods"]),
        row("05-10-2026", "-22000", head=["CRED Club", "UPI/CRED Club/XXclub@axisb/payment on/AXIS"],
            tail=["BANK/664414543345/ACDcb0c287186c341889dcd580f632ca", "8e3"]),
        row("07-10-2026", "-500", head=["ACH/PPFAS 07102026"], own="CAMS/ICIC700/5914812162552 11106293", tail=["TAILMARK"]),
        row("15-10-2026", "-5000", own="TO PPF 004000000000_SR123456789"),
        row("19-10-2026", "10568.01", mode="CMS TXN", glue=True, own="CMS/ 10H REFUND/BANK CREDIT CARD WITHDR"),
        row("24-10-2026", "50000", head=["A PAYER"], tail=["BANK/245617959646/AXLbe18d"]),
    ]


def statement_pdf(**kw):
    ppf_rows = [row("15-10-2026", "5000", own="FRM SB 236000000000_SR123456789")]
    return icici_statement(
        [section("PPF", PPF, "100000.00", ppf_rows), section("Savings", SAV, "300000.00", savings_rows())],
        fds=[dict(linked=SAV, text=FD_TEXT)], **kw)


def parse(pdf, tmp_path, password=None):
    path = tmp_path / "s.pdf"
    path.write_bytes(pdf)
    return IciciStatementParser().parse(path, password)


# ── parser ────────────────────────────────────────────────────────────────────

def test_parses_accounts_balances_and_signs(tmp_path):
    st = parse(statement_pdf(), tmp_path)
    assert {a.type for a in st.accounts} == {"ppf", "bank"}
    sav = next(a for a in st.accounts if a.type == "bank")
    assert (sav.opening_balance, sav.closing_balance) == (Decimal("300000.00"), Decimal("300000.00") + sum(
        Decimal(r["amount"]) for r in savings_rows()))
    assert len(sav.txns) == 7 and sav.integrity_errors() == []
    by_amount = {t.amount: t for t in sav.txns}
    assert by_amount[Decimal("-9000")].kind == "DEBIT" and by_amount[Decimal("200000")].kind == "CREDIT"
    assert (st.period_from, st.period_to) == (date(2026, 10, 1), date(2026, 10, 31))
    assert st.warnings == []


def test_pdf_spanning_pages_chains_balances_and_totals(tmp_path):
    st = parse(statement_pdf(rows_per_page=2), tmp_path)           # several pages, a Total row on each
    sav = next(a for a in st.accounts if a.type == "bank")
    assert len(sav.txns) == 7 and sav.integrity_errors() == []


def test_wrapped_lines_go_to_the_right_row(tmp_path):
    sav = next(a for a in parse(statement_pdf(), tmp_path).accounts if a.type == "bank")
    by_amount = {t.amount: t.description for t in sav.txns}
    assert "CRED Club" in by_amount[Decimal("-22000")]
    assert "CRED" not in by_amount[Decimal("200000")] and "ACME TRADERS" in by_amount[Decimal("200000")]
    assert by_amount[Decimal("200000")].startswith("NET BANKING")
    assert "PPFAS" in by_amount[Decimal("-500")] and "TAILMARK" in by_amount[Decimal("-500")]   # own-line text + head + tail
    assert by_amount[Decimal("10568.01")].startswith("CMS TXN")                      # date glued to the mode


def test_references_extracted_and_long_numbers_masked(tmp_path):
    sav = next(a for a in parse(statement_pdf(), tmp_path).accounts if a.type == "bank")
    by_amount = {t.amount: t for t in sav.txns}
    assert by_amount[Decimal("-5000")].reference == "SR123456789"
    assert by_amount[Decimal("-22000")].reference == "664414543345"                           # UPI reference
    assert "004000000000" not in by_amount[Decimal("-5000")].description                      # own account number masked
    assert "664414543345" not in by_amount[Decimal("-22000")].description


def test_fixed_deposit_and_linked_account(tmp_path):
    st = parse(statement_pdf(), tmp_path)
    (fd,) = st.deposits
    assert (fd.principal, fd.rate, fd.maturity_amount, fd.current_balance) == (
        Decimal("50000.00"), Decimal("6.75"), Decimal("53400.00"), Decimal("50000.00"))
    assert (fd.open_date, fd.maturity_date, fd.tenure, fd.linked_account) == (
        date(2026, 3, 5), date(2027, 3, 5), "12 Mths 0 Days", SAV)


def test_statement_without_deposits_is_fine(tmp_path):
    st = parse(icici_statement([section("Savings", SAV, "1000.00", [row("02-10-2026", "-100")])]), tmp_path)
    assert st.deposits == [] and len(st.accounts) == 1


def test_encrypted_pdf_opens_with_password(tmp_path):
    st = parse(encrypt(statement_pdf(), "abcd0101"), tmp_path, password="abcd0101")
    assert len(st.accounts) == 2


# ── integrity gates: a statement that does not add up is never imported ──────

def test_wrong_printed_balance_is_refused(tmp_path):
    rows = [row("02-10-2026", "-100"), row("03-10-2026", "-200", bal="650.00")]    # should read 700.00
    with pytest.raises(ValueError, match="not the printed balance"):
        parse(icici_statement([section("Savings", SAV, "1000.00", rows)]), tmp_path)


def test_amount_in_the_wrong_column_is_refused(tmp_path):
    rows = [row("02-10-2026", "-100", col="dep")]                                  # a debit printed under DEPOSITS
    with pytest.raises(ValueError, match="not the printed balance"):
        parse(icici_statement([section("Savings", SAV, "1000.00", rows)]), tmp_path)


def test_page_total_mismatch_is_refused(tmp_path):
    with pytest.raises(ValueError, match="page totals"):
        parse(icici_statement([section("Savings", SAV, "1000.00", [row("02-10-2026", "-100")])],
                              total_dep_error="5.00"), tmp_path)


def test_closing_balance_in_total_row_must_match(tmp_path):
    with pytest.raises(ValueError, match="page totals"):
        parse(icici_statement([section("Savings", SAV, "1000.00", [row("02-10-2026", "-100")], total_bal="999.00")]),
              tmp_path)


def test_not_a_statement_is_refused(tmp_path):
    from tests.pdffix import build_pdf
    with pytest.raises(ValueError, match="no account transactions"):
        parse(build_pdf([[(40, 60, "Hello world")]]), tmp_path)


def test_unsupported_account_type_warns_instead_of_silently_skipping(tmp_path):
    pdf = icici_statement([section("Savings", SAV, "1000.00", [row("02-10-2026", "-100")]),
                           section("Recurring", "XXXXXXXX9999", "10.00", [row("02-10-2026", "5")])])
    st = parse(pdf, tmp_path)
    assert len(st.accounts) == 1 and any("not supported" in w for w in st.warnings)


# ── ledger ────────────────────────────────────────────────────────────────────

@pytest.fixture
def stmt(tmp_path):
    return parse(statement_pdf(), tmp_path)


def test_import_is_idempotent_and_records_balances_and_fds(db_path, stmt):
    led = Ledger(db_path)
    res = led.import_statement(stmt, sha256="a" * 64)
    assert (res.status, res.txn_new, res.txn_dup) == ("imported", 8, 0)
    assert led.import_statement(stmt, sha256="a" * 64).status == "duplicate"
    again = led.import_statement(stmt, sha256="b" * 64)                    # same rows, different file: nothing new
    assert (again.txn_new, again.txn_dup) == (0, 8)
    led.close()
    conn = connect_ro(db_path)
    accts = {r["type"]: r for r in conn.execute("SELECT * FROM accounts")}
    assert accts["bank"]["liability"] == 0 and accts["bank"]["mask"] == "1234"
    assert "XXXXXXXX1234" not in str(dict(accts["bank"]).values())          # the identifier is hashed, not stored
    assert conn.execute("SELECT COUNT(*) FROM account_balances").fetchone()[0] == 2
    fd = conn.execute("SELECT * FROM fixed_deposits").fetchone()
    assert fd["mask"] == "0001" and fd["linked_account_id"] == accts["bank"]["id"]
    assert conn.execute("SELECT COUNT(*) FROM fixed_deposits").fetchone()[0] == 1


def test_overlapping_statements_do_not_duplicate_rows(db_path, tmp_path):
    first = parse(icici_statement([section("Savings", SAV, "1000.00",
                                           [row("02-10-2026", "-100"), row("03-10-2026", "-200")])]), tmp_path)
    second = parse(icici_statement([section("Savings", SAV, "900.00",
                                            [row("03-10-2026", "-200"), row("04-10-2026", "-50")])]), tmp_path)
    led = Ledger(db_path)
    led.import_statement(first, sha256="a" * 64)
    res = led.import_statement(second, sha256="b" * 64)
    assert (res.txn_new, res.txn_dup) == (1, 1)
    assert led.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 3


def test_identical_rows_on_one_day_are_kept_distinct(db_path, tmp_path):
    rows = [row("02-10-2026", "-100"), row("02-10-2026", "100"), row("02-10-2026", "-100")]
    led = Ledger(db_path)
    res = led.import_statement(parse(icici_statement([section("Savings", SAV, "1000.00", rows)]), tmp_path),
                               sha256="a" * 64)
    assert res.txn_new == 3


def test_ledger_refuses_a_statement_that_does_not_balance(db_path):
    bad = ParsedAccountStatement(parser="x", accounts=[ParsedAccount(
        type="bank", institution="Bank", identifier="XXXX0001", period_to=date(2026, 10, 31),
        opening_balance=Decimal("100"), closing_balance=Decimal("999"),
        txns=[AccountTxn(date=date(2026, 10, 2), amount=Decimal("-10"), balance=Decimal("90"), kind="DEBIT")])])
    led = Ledger(db_path)
    with pytest.raises(ValueError, match="statement says 999"):
        led.import_statement(bad, sha256="a" * 64)
    assert led.conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 0      # nothing was written


def test_redactor_masks_names_in_descriptions(db_path, stmt):
    led = Ledger(db_path)
    led.import_statement(stmt, sha256="a" * 64, redactor=lambda t: t.replace("A PAYER", "•••"))
    descs = [r[0] for r in led.conn.execute("SELECT description FROM transactions")]
    assert not any("A PAYER" in d for d in descs) and any("•••" in d for d in descs)


def test_mutual_fund_logic_ignores_bank_rows(db_path, stmt):
    led = Ledger(db_path)
    led.import_statement(stmt, sha256="a" * 64)
    assert led.flag_duplicate_accounts() == [] and led.flag_duplicate_transactions() == []
    assert analytics.portfolio(connect_ro(db_path)).rows == []


def test_schema_v4_database_upgrades_to_v5(tmp_path):
    import sqlite3
    from finance import db
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(db.SCHEMA)
    for v in (2, 3, 4):
        raw.executescript(db.MIGRATIONS[v])
    raw.execute("PRAGMA user_version = 4")
    raw.execute("INSERT INTO accounts (type, institution, name, mask, ref_hash) VALUES ('mutual_fund','X','X','1','h')")
    raw.commit(); raw.close()
    conn = connect_ro(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1                  # data kept
    conn.execute("SELECT reference FROM transactions LIMIT 1")
    conn.execute("SELECT * FROM account_balances LIMIT 1"); conn.execute("SELECT * FROM fixed_deposits LIMIT 1")


# ── transfer matching ─────────────────────────────────────────────────────────

def card_statement(rows, ident="XXXX9999", sha_tag="c"):
    """rows: [(date, amount, kind)]; cards print no running balance, so rows carry none."""
    txns = [AccountTxn(date=d, amount=Decimal(a), kind=k, description="PAYMENT RECEIVED" if k == "PAYMENT" else "SHOP")
            for d, a, k in rows]
    closing = sum(t.amount for t in txns)
    return ParsedAccountStatement(parser="card", period_to=date(2026, 10, 31), accounts=[ParsedAccount(
        type="card", institution="Test Card", identifier=ident, period_to=date(2026, 10, 31),
        opening_balance=Decimal(0), closing_balance=closing, txns=txns)])


def links(led):
    return [dict(r) for r in led.conn.execute("SELECT method, confidence FROM transfer_links")]


def test_own_account_transfer_is_linked_by_reference(db_path, stmt):
    led = Ledger(db_path)
    led.import_statement(stmt, sha256="a" * 64)
    rows = led.conn.execute(
        "SELECT o.amount oa, i.amount ia FROM transfer_links l JOIN transactions o ON o.id=l.txn_out "
        "JOIN transactions i ON i.id=l.txn_in").fetchall()
    assert [(r["oa"], r["ia"]) for r in rows] == [("-5000", "5000")]
    assert links(led) == [{"method": "reference", "confidence": "high"}]
    led.import_statement(stmt, sha256="b" * 64)                             # re-running never links twice
    assert len(links(led)) == 1


def test_reference_on_both_legs_inside_one_account_is_not_a_transfer(db_path, tmp_path):
    rows = [row("02-10-2026", "-100", own="X/SR555555555"), row("03-10-2026", "100", own="Y/SR555555555")]
    led = Ledger(db_path)
    led.import_statement(parse(icici_statement([section("Savings", SAV, "1000.00", rows)]), tmp_path), sha256="a" * 64)
    assert links(led) == []


def test_card_payment_linked_by_amount_and_window(db_path, stmt):
    led = Ledger(db_path)
    led.import_statement(stmt, sha256="a" * 64)                              # bank debit of 22,000 to CRED on 05-10
    res = led.import_statement(card_statement([(date(2026, 10, 7), "22000", "PAYMENT")]), sha256="c" * 64)
    assert {"method": "amount+window", "confidence": "high"} in links(led)
    assert res.warnings == []


def test_card_payment_outside_window_or_wrong_kind_is_not_linked(db_path, stmt):
    led = Ledger(db_path)
    led.import_statement(stmt, sha256="a" * 64)
    res = led.import_statement(card_statement([(date(2026, 10, 12), "22000", "PAYMENT"),
                                               (date(2026, 10, 5), "22000", "REFUND")]), sha256="c" * 64)
    assert [m for m in links(led) if m["method"] == "amount+window"] == []
    assert any("no matching bank debit" in w for w in res.warnings)


def test_bank_debit_that_does_not_look_like_a_card_payment_is_ignored(db_path, tmp_path):
    bank = parse(icici_statement([section("Savings", SAV, "50000.00", [row("05-10-2026", "-22000", own="UPI/SHOP/pay")])]),
                 tmp_path)
    led = Ledger(db_path)
    led.import_statement(bank, sha256="a" * 64)
    led.import_statement(card_statement([(date(2026, 10, 5), "22000", "PAYMENT")]), sha256="c" * 64)
    assert links(led) == []


def test_ambiguous_card_payment_goes_to_review_not_guessed(db_path, tmp_path):
    two = parse(icici_statement([section("Savings", SAV, "90000.00", [
        row("05-10-2026", "-22000", own="UPI/CRED Club/payment"), row("06-10-2026", "-22000", own="UPI/CRED Club/payment")])]),
        tmp_path)
    led = Ledger(db_path)
    led.import_statement(two, sha256="a" * 64)
    res = led.import_statement(card_statement([(date(2026, 10, 6), "22000", "PAYMENT")]), sha256="c" * 64)
    assert links(led) == [] and any("2 bank debits" in w for w in res.warnings)
    open_items = [r["kind"] for r in led.conn.execute("SELECT kind FROM review_queue WHERE status='open'")]
    assert open_items == ["transfer_ambiguous"]
    match_transfers(led)                                                      # idempotent: no second item
    assert led.conn.execute("SELECT COUNT(*) FROM review_queue WHERE kind='transfer_ambiguous'").fetchone()[0] == 1


def test_two_card_payments_for_one_debit_are_ambiguous(db_path, tmp_path):
    one = parse(icici_statement([section("Savings", SAV, "90000.00", [row("05-10-2026", "-22000", own="UPI/CRED Club/pay")])]),
                tmp_path)
    led = Ledger(db_path)
    led.import_statement(one, sha256="a" * 64)
    led.import_statement(card_statement([(date(2026, 10, 5), "22000", "PAYMENT"), (date(2026, 10, 6), "22000", "PAYMENT")]),
                         sha256="c" * 64)
    assert links(led) == []
    assert led.conn.execute("SELECT COUNT(*) FROM review_queue WHERE kind='transfer_ambiguous'").fetchone()[0] >= 1


# ── end to end through the mailbox pipeline ───────────────────────────────────

def _sync(db_path, tmp_path, pdf, monkeypatch):
    from tests.mailfix import make_email
    from tests.test_sync import APP_PW, IDENT, account, runner
    from finance.mailconfig import SenderRule
    monkeypatch.setattr("finance.sources.imap.get_secret", lambda key: APP_PW)
    acct = account(senders=[SenderRule(match="icici.example", parser="icici", templates=["{name4}{ddmm}"])])
    msgs = {1: make_email(sender="statements@icici.example", pdf=pdf)}
    return runner(db_path, msgs, accounts=[acct], inbox=str(tmp_path / "inbox")).run().accounts[0]


def test_encrypted_statement_email_is_imported_and_transfer_linked(db_path, tmp_path, monkeypatch):
    pdf = encrypt(statement_pdf(), "test1705")                       # {name4}{ddmm} for the test identity: test + 17 May
    a = _sync(db_path, tmp_path, pdf, monkeypatch)
    assert (a.imported, a.attention, a.error, a.txn_new) == (1, 0, "", 8)
    conn = connect_ro(db_path)
    assert conn.execute("SELECT COUNT(*) FROM transfer_links").fetchone()[0] == 1
    assert conn.execute("SELECT parser FROM source_documents").fetchone()[0] == "icici"
    assert not list((tmp_path / "inbox").glob("*.pdf"))              # nothing left waiting


def test_statement_that_does_not_add_up_waits_in_review_and_imports_nothing(db_path, tmp_path, monkeypatch):
    bad = encrypt(icici_statement([section("Savings", SAV, "1000.00", [row("02-10-2026", "-100", bal="650.00")])]), "test1705")
    a = _sync(db_path, tmp_path, bad, monkeypatch)
    assert (a.imported, a.attention) == (0, 1)
    conn = connect_ro(db_path)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    reason = conn.execute("SELECT reason FROM review_queue WHERE kind='needs_attention'").fetchone()[0]
    assert "icici parser failed" in reason and "printed balance" in reason


# ── analytics and pages ───────────────────────────────────────────────────────

def test_cash_flow_balances_add_up(db_path, stmt):
    led = Ledger(db_path)
    led.import_statement(stmt, sha256="a" * 64)
    led.import_statement(card_statement([(date(2026, 10, 7), "22000", "PAYMENT"), (date(2026, 10, 8), "-1500", "PURCHASE")]),
                         sha256="c" * 64)
    (m,) = analytics.cash_flow_by_month(connect_ro(db_path))
    assert m["month"] == "2026-10" and m["opening"] == Decimal("300000.00")
    assert m["money_in"] == Decimal("260568.01")
    assert m["money_out"] == Decimal("-36500")                       # includes the 5,000 PPF top-up and 22,000 card bill
    assert "card_spending" not in m                                  # cards have their own section
    assert m["opening"] + m["money_in"] + m["money_out"] == m["closing"] == Decimal("524068.01")


def test_search_covers_every_column_and_all_words(db_path, stmt):
    led = Ledger(db_path)
    led.import_statement(stmt, sha256="a" * 64)
    conn = connect_ro(db_path)
    found = lambda q, **kw: analytics.search_transactions(conn, text=q, **kw)
    assert len(found("club")) == 1 and len(found("CLUB")) == 1       # description, any case
    assert len(found("22,000")) == 1 and len(found("22000")) == 1    # amount, with or without separators
    assert len(found("2026-10-19")) == 1                             # date
    assert len(found("debit")) > 1 and len(found("ppf")) == 3        # kind; account name, description (incl. PPFAS)
    assert len(found("club debit")) == 1 and found("club credit") == []   # every word must match
    assert len(found("savings", account_type="bank")) == 7
    assert len(found("50%")) == 0                                    # wildcard characters are literal
    assert [r["linked"] for r in found("TO PPF")] == [1]


def test_accounts_overview_lists_latest_balance_and_deposits(db_path, stmt):
    led = Ledger(db_path)
    led.import_statement(stmt, sha256="a" * 64)
    ov = analytics.accounts_overview(connect_ro(db_path))
    assert {a["type"]: a["balance"] for a in ov["accounts"]}["ppf"] == "105000"
    assert [d["mask"] for d in ov["deposits"]] == ["0001"]


from tests.test_web import authed, client  # noqa: E402,F401  (fixtures for the page tests)


def test_accounts_page_and_transaction_filter(client, db_path, stmt):
    led = Ledger(db_path)
    led.import_statement(stmt, sha256="a" * 64)
    led.close()
    authed(client)
    page = client.get("/accounts").text
    assert "ICICI Bank Savings …1234" in page and "Fixed deposits" in page and "…0001" in page
    assert "Bank balance by month" in page and "2026-10" in page and "Opening balance" in page
    bank = client.get("/transactions?account_type=bank").text
    assert "TO PPF" in bank and "ICICI Bank" in bank and "transfer between your accounts" in bank
    assert "<th>Description</th>" in bank and "Search fund" not in bank
    assert "TO PPF" not in client.get("/transactions?account_type=mutual_fund").text
    assert "Accounts" in client.get("/").text                        # nav entry


def test_card_spending_by_month_is_separate_and_names_its_statements(db_path, tmp_path):
    from tests.cardfix import crow, icici_card
    from finance.parsers.icici_card import IciciCardParser
    led = Ledger(db_path)

    def card(tag, prev, rows, period, card_no="4315XXXXXXXX1234"):
        path = tmp_path / f"{tag}.pdf"
        path.write_bytes(icici_card(prev, rows, card=card_no, period=period, due="December 18, 2026"))
        led.import_statement(IciciCardParser().parse(path, None), sha256=tag * 64)

    card("a", "0.00", [crow("02/10/2026", "SHOP", "1000"), crow("05/10/2026", "REFUND", "200", credit=True),
                       crow("06/10/2026", "Payment received", "300", credit=True)],
         ("October 01, 2026", "October 31, 2026"))
    card("b", "500.00", [crow("03/11/2026", "SHOP TWO", "400")], ("November 01, 2026", "November 30, 2026"))
    card("c", "0.00", [crow("04/10/2026", "OTHER CARD", "50")], ("October 01, 2026", "October 15, 2026"), "4315XXXXXXXX9999")
    oct_, nov = sorted(analytics.card_spending_by_month(connect_ro(db_path)), key=lambda m: m["month"])
    assert (oct_["purchases"], oct_["refunds"], oct_["net"]) == (Decimal("1050.00"), Decimal("200.00"), Decimal("850.00"))
    assert nov["net"] == Decimal("400.00")                                           # the payment is never spending
    assert len(oct_["statements"]) == 2 and {s["card"][-4:] for s in oct_["statements"]} == {"1234", "9999"}
    assert sorted(s["rows"] for s in oct_["statements"]) == [1, 2]                  # the payment row is not counted
    assert nov["statements"][0]["statement_date"] == "2026-11-30"


def test_accounts_page_shows_card_spending_in_its_own_section(client, db_path, tmp_path):
    from tests.cardfix import crow, icici_card
    from finance.parsers.icici_card import IciciCardParser
    path = tmp_path / "c.pdf"
    path.write_bytes(icici_card("0.00", [crow("02/10/2026", "SHOP", "1000")]))
    led = Ledger(db_path)
    led.import_statement(IciciCardParser().parse(path, None), sha256="a" * 64)
    led.close()
    authed(client)
    page = client.get("/accounts").text
    assert "Card spending by month" in page and "Statements considered" in page and "ICICI Bank Credit Card …1234" in page
    assert "<th class=\"n\">Card spending</th>" not in page and "Bank balance by month" not in page    # no bank rows here


def test_account_type_is_a_tab_strip_that_scopes_everything_below(client, db_path, stmt):
    led = Ledger(db_path)
    led.import_statement(stmt, sha256="a" * 64)
    led.close()
    authed(client)
    page = client.get("/transactions?account_type=bank&q=club&kind=DEBIT&date_from=2026-10-01").text
    assert '<div class="tabs">' in page and "All accounts" in page and ">Bank<" in page and ">PPF<" in page
    assert '<select name="account_type"' not in page                      # no longer a field in the filter form
    assert '<input type="hidden" name="account_type" value="bank">' in page  # the form keeps the chosen account type
    assert 'class="on" href="/transactions?account_type=bank' in page      # the current tab is marked
    import re
    ppf_tab = re.search(r'<a class="[^"]*" href="([^"]+)">PPF</a>', page).group(1)
    assert "q=club" in ppf_tab and "date_from=2026-10-01" in ppf_tab and "kind" not in ppf_tab    # search kept, type reset
    assert "account_type=ppf" in ppf_tab and "Search" in page


def test_type_options_only_list_kinds_of_the_chosen_account(db_path, stmt, tmp_path):
    from tests.cardfix import crow, icici_card
    from finance.parsers.icici_card import IciciCardParser
    led = Ledger(db_path)
    led.import_statement(stmt, sha256="a" * 64)
    p = tmp_path / "c.pdf"
    p.write_bytes(icici_card("0.00", [crow("02/10/2026", "SHOP", "100"), crow("03/10/2026", "REFUND", "10", credit=True)]))
    led.import_statement(IciciCardParser().parse(p, None), sha256="c" * 64)
    conn = connect_ro(db_path)
    assert set(analytics.txn_kinds(conn, "card")) == {"DEBIT", "CREDIT"}
    assert "PAYMENT" not in analytics.txn_kinds(conn, "bank") and "CREDIT" in analytics.txn_kinds(conn, "bank")
    assert set(analytics.txn_kinds(conn)) >= {"DEBIT", "CREDIT"}
