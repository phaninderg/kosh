"""Credit card statement parsers (ICICI, HDFC, Axis): parsing, integrity gates, ledger import, transfer matching.
Synthetic PDFs only, laid out like the statements the parsers were written against."""

from datetime import date
from decimal import Decimal

import pytest

from finance.db import connect_ro
from finance.ledger import Ledger
from finance.parsers.axis_card import AxisCardParser
from finance.parsers.hdfc_card import HdfcCardParser
from finance.parsers.icici_card import IciciCardParser
from finance.parsers.tata_card import TataCardParser
from tests.cardfix import axis_card, crow, hdfc_card, icici_card, tata_card
from tests.pdffix import encrypt

PREV = "2000.00"


def rows():
    return [
        crow("02/10/2026", "SHOP ONE MUMBAI", "1200.50", tail="IN"),
        crow("05/10/2026", "UPI 123456789012 COFFEE", "300"),
        crow("06/10/2026", "Payment received", "1500", credit=True),
        crow("12/10/2026", "REFUND SHOP ONE", "200", credit=True),
        crow("20/10/2026", "BOOKS ONLINE", "999.99"),
    ]


def parse(parser, pdf, tmp_path, password=None):
    path = tmp_path / "s.pdf"
    path.write_bytes(pdf)
    return parser.parse(path, password)


ISSUERS = [
    pytest.param(IciciCardParser(), lambda **kw: icici_card(PREV, rows(), **kw), "ICICI Bank", id="icici"),
    pytest.param(HdfcCardParser(), lambda **kw: hdfc_card(PREV, rows(), **kw), "HDFC Bank", id="hdfc"),
    pytest.param(AxisCardParser(), lambda **kw: axis_card(PREV, rows(), **kw), "Axis Bank", id="axis"),
    pytest.param(TataCardParser(), lambda **kw: tata_card(PREV, rows(), **kw), "Tata Neu", id="tata"),
]
TOTAL = Decimal("2000.00") + Decimal("1200.50") + 300 + Decimal("999.99") - 1500 - 200        # 2800.49


# ── parsing ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("parser, build, institution", ISSUERS)
def test_parses_summary_rows_and_signs(parser, build, institution, tmp_path):
    st = parse(parser, build(), tmp_path)
    (a,) = st.accounts
    assert (a.type, a.institution, a.label) == ("card", institution, "Credit Card")
    assert (a.opening_balance, a.closing_balance) == (Decimal("-2000.00"), -TOTAL)            # a card owes: negative
    assert a.integrity_errors() == [] and st.warnings == []
    assert [t.amount for t in a.txns] == [Decimal("-1200.50"), Decimal("-300"), Decimal("1500"), Decimal("200"),
                                          Decimal("-999.99")]
    assert [t.kind for t in a.txns] == ["DEBIT", "DEBIT", "PAYMENT", "CREDIT", "DEBIT"]
    assert [t.date for t in a.txns][:2] == [date(2026, 10, 2), date(2026, 10, 5)]
    assert a.identifier[-2:] in ("90", "34", "99")                       # Tata prints only the last two digits
    assert a.min_due == Decimal("500.00") and a.credit_limit in (Decimal("100000"), Decimal("100000.00"))
    assert a.statement_date == date(2026, 10, 31) and a.due_date > a.statement_date
    assert a.period_to == date(2026, 10, 31)


@pytest.mark.parametrize("parser, build, institution", ISSUERS)
def test_long_numbers_in_descriptions_are_masked(parser, build, institution, tmp_path):
    (a,) = parse(parser, build(), tmp_path).accounts
    coffee = next(t for t in a.txns if "COFFEE" in t.description)
    assert "123456789012" not in coffee.description and "•••" in coffee.description


@pytest.mark.parametrize("parser, build, institution", ISSUERS)
def test_encrypted_pdf_opens_with_password(parser, build, institution, tmp_path):
    st = parse(parser, encrypt(build(), "abcd0101"), tmp_path, password="abcd0101")
    assert len(st.accounts[0].txns) == 5


def test_icici_wrapped_line_and_second_page(tmp_path):
    st = parse(IciciCardParser(), icici_card(PREV, rows(), per_first_page=2), tmp_path)        # table continues on page 2
    (a,) = st.accounts
    assert len(a.txns) == 5 and a.integrity_errors() == []
    assert a.txns[0].description == "SHOP ONE MUMBAI IN"              # the wrapped tail belongs to its row
    assert not any("International" in t.description or t.description.startswith("#") for t in a.txns)


def test_icici_due_date_is_the_single_panel_date_after_the_period(tmp_path):
    (a,) = parse(IciciCardParser(), icici_card(PREV, rows(), due="November 18, 2026"), tmp_path).accounts
    assert a.due_date == date(2026, 11, 18)


def test_icici_ambiguous_due_date_is_left_empty_with_a_warning(tmp_path):
    st = parse(IciciCardParser(), icici_card(PREV, rows(), extra_date="November 25, 2026"), tmp_path)
    assert st.accounts[0].due_date is None and any("ambiguous" in w for w in st.warnings)


def test_hdfc_credit_marker_ref_dropped_and_emi_tag_kept(tmp_path):
    rs = rows()
    rs[1] = crow("05/10/2026", "COFFEE SHOP", "300", tag="EMI")
    (a,) = parse(HdfcCardParser(), hdfc_card(PREV, rs), tmp_path).accounts
    assert a.txns[2].amount == Decimal("1500") and a.txns[1].amount == Decimal("-300")       # '+' marks a credit
    assert all("Ref#" not in t.description and "1234567890" not in t.description for t in a.txns)
    assert a.txns[1].description.startswith("EMI COFFEE")
    assert a.txns[0].description == "SHOP ONE MUMBAI IN"              # the wrapped line belongs to its row
    assert a.due_date == date(2026, 11, 18)


def test_hdfc_table_on_a_second_page_with_other_columns(tmp_path):
    (a,) = parse(HdfcCardParser(), hdfc_card(PREV, rows(), per_first_page=2), tmp_path).accounts
    assert len(a.txns) == 5 and a.integrity_errors() == []


def test_hdfc_finance_charges_are_part_of_the_summary(tmp_path):
    rs = [crow("02/10/2026", "SHOP", "100"), crow("03/10/2026", "FINANCE CHARGES", "25")]
    (a,) = parse(HdfcCardParser(), hdfc_card("1000.00", rs, finance="0.00"), tmp_path).accounts
    assert a.closing_balance == Decimal("-1125.00")
    with pytest.raises(ValueError, match="does not add up"):
        parse(HdfcCardParser(), hdfc_card("1000.00", rs, finance="25.00", total="1125.00"), tmp_path)   # 1,150 expected


def test_axis_credit_balance_is_negative_dues(tmp_path):
    rs = [crow("02/10/2026", "SHOP", "100"), crow("03/10/2026", "Payment received", "400", credit=True)]
    (a,) = parse(AxisCardParser(), axis_card("100.00", rs), tmp_path).accounts
    assert a.closing_balance == Decimal("200.00")                     # total due -200: the card is in credit
    assert a.integrity_errors() == []


# ── integrity gates: a statement that does not add up is never imported ──────

@pytest.mark.parametrize("parser, build, institution", ISSUERS)
def test_wrong_total_is_refused(parser, build, institution, tmp_path):
    with pytest.raises(ValueError, match="does not add up"):
        parse(parser, build(total="2900.00"), tmp_path)


@pytest.mark.parametrize("parser, build, institution", ISSUERS)
def test_a_dropped_row_is_refused(parser, build, institution, tmp_path):
    with pytest.raises(ValueError, match="statement says"):
        parse(parser, build(skip=(1,)), tmp_path)


@pytest.mark.parametrize("parser, build, institution", ISSUERS)
def test_wrong_direction_marker_is_refused(parser, build, institution, tmp_path):
    with pytest.raises(ValueError, match="statement says"):
        parse(parser, build(flip=(0,)), tmp_path)


@pytest.mark.parametrize("parser", [IciciCardParser(), HdfcCardParser(), AxisCardParser(), TataCardParser()])
def test_not_a_card_statement_is_refused(parser, tmp_path):
    from tests.pdffix import build_pdf
    with pytest.raises(ValueError, match="layout not recognised"):
        parse(parser, build_pdf([[(40, 60, "Hello world")]]), tmp_path)


# ── ledger ────────────────────────────────────────────────────────────────────

@pytest.fixture
def icici_stmt(tmp_path):
    return parse(IciciCardParser(), icici_card(PREV, rows()), tmp_path)


def test_import_records_the_card_as_a_liability_with_its_dues(db_path, icici_stmt):
    led = Ledger(db_path)
    res = led.import_statement(icici_stmt, sha256="a" * 64)
    assert (res.status, res.txn_new) == ("imported", 5)
    assert led.import_statement(icici_stmt, sha256="a" * 64).status == "duplicate"
    assert led.import_statement(icici_stmt, sha256="b" * 64).txn_new == 0                  # same rows, other file
    led.close()
    conn = connect_ro(db_path)
    card = conn.execute("SELECT * FROM accounts").fetchone()
    assert (card["type"], card["liability"], card["mask"]) == ("card", 1, "1234")
    assert "4315XXXXXXXX1234" not in str(dict(card).values())                              # hashed, not stored
    assert conn.execute("SELECT balance FROM account_balances").fetchone()[0] == "-2800.49"
    s = conn.execute("SELECT * FROM statement_summaries").fetchone()
    assert (s["total_due"], s["min_due"], s["due_date"], s["statement_date"]) == (
        "2800.49", "500", "2026-11-18", "2026-10-31")
    assert conn.execute("SELECT COUNT(*) FROM statement_summaries").fetchone()[0] == 1


def test_next_statement_adds_its_own_summary_and_keeps_the_account(db_path, icici_stmt, tmp_path):
    nov = parse(IciciCardParser(), icici_card("2800.49", [crow("03/11/2026", "SHOP TWO", "100")],
                                              period=("November 01, 2026", "November 30, 2026"),
                                              due="December 18, 2026"), tmp_path)
    led = Ledger(db_path)
    led.import_statement(icici_stmt, sha256="a" * 64)
    led.import_statement(nov, sha256="b" * 64)
    assert led.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
    assert [r[0] for r in led.conn.execute("SELECT total_due FROM statement_summaries ORDER BY statement_date")] == [
        "2800.49", "2900.49"]


def test_two_cards_stay_separate_accounts(db_path, tmp_path):
    other = parse(IciciCardParser(), icici_card(PREV, rows(), card="4315XXXXXXXX5678"), tmp_path)
    led = Ledger(db_path)
    led.import_statement(parse(IciciCardParser(), icici_card(PREV, rows()), tmp_path), sha256="a" * 64)
    led.import_statement(other, sha256="b" * 64)
    assert led.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 2


def test_card_bill_payment_links_to_the_bank_debit(db_path, tmp_path, icici_stmt):
    from tests.pdffix import icici_statement, row, section
    bank = tmp_path / "bank.pdf"
    bank.write_bytes(icici_statement([section("Savings", "XXXXXXXX1234", "50000.00",
                                              [row("07-10-2026", "-1500", own="UPI/CRED Club/payment")])]))
    from finance.parsers.icici import IciciStatementParser
    led = Ledger(db_path)
    led.import_statement(IciciStatementParser().parse(bank, None), sha256="a" * 64)
    res = led.import_statement(icici_stmt, sha256="c" * 64)                                # 1,500 payment received 06-10
    assert [dict(r) for r in led.conn.execute("SELECT method, confidence FROM transfer_links")] == [
        {"method": "amount+window", "confidence": "high"}]
    assert res.warnings == []


# ── end to end through the mailbox pipeline ───────────────────────────────────

def _sync(db_path, tmp_path, pdf, parser, monkeypatch):
    from finance.mailconfig import SenderRule
    from tests.mailfix import make_email
    from tests.test_sync import APP_PW, account, runner
    monkeypatch.setattr("finance.sources.imap.get_secret", lambda key: APP_PW)
    acct = account(senders=[SenderRule(match="cards.example", parser=parser, templates=["{name4}{ddmm}"])])
    msgs = {1: make_email(sender="statements@cards.example", pdf=pdf)}
    return runner(db_path, msgs, accounts=[acct], inbox=str(tmp_path / "inbox")).run().accounts[0]


@pytest.mark.parametrize("parser, build", [("icici_card", lambda: icici_card(PREV, rows())),
                                           ("hdfc_card", lambda: hdfc_card(PREV, rows())),
                                           ("axis_card", lambda: axis_card(PREV, rows())),
                                           ("tata_card", lambda: tata_card(PREV, rows()))])
def test_encrypted_card_statement_email_is_imported(db_path, tmp_path, monkeypatch, parser, build):
    a = _sync(db_path, tmp_path, encrypt(build(), "test1705"), parser, monkeypatch)
    assert (a.imported, a.attention, a.error, a.txn_new) == (1, 0, "", 5)
    conn = connect_ro(db_path)
    assert conn.execute("SELECT parser FROM source_documents").fetchone()[0] == parser
    assert conn.execute("SELECT liability FROM accounts").fetchone()[0] == 1


def test_card_statement_that_does_not_add_up_waits_in_review(db_path, tmp_path, monkeypatch):
    a = _sync(db_path, tmp_path, encrypt(icici_card(PREV, rows(), skip=(0,)), "test1705"), "icici_card", monkeypatch)
    assert (a.imported, a.attention) == (0, 1)
    conn = connect_ro(db_path)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert "icici_card parser failed" in conn.execute("SELECT reason FROM review_queue").fetchone()[0]


def test_money_accepts_each_issuers_currency_prefix():
    from finance.parsers.cardlib import money, whole_money
    assert money("`9,99,999.99") == Decimal("999999.99") and money("C1,234.50") == Decimal("1234.50")
    assert money("₹10.00") == Decimal("10.00") and money("-5.25") == Decimal("-5.25")
    assert money("Chennai") is None and money("1,234") is None and money("Cr") is None
    assert whole_money("C1,00,000") == Decimal("100000") and whole_money("1,00,000.00") == Decimal("100000")


@pytest.mark.parametrize("parser, build", [(TataCardParser(), tata_card), (HdfcCardParser(), hdfc_card)])
def test_total_rounded_to_the_rupee_is_accepted_and_the_exact_figure_used(parser, build, tmp_path):
    st = parse(parser, build(PREV, rows(), total="2,800.00"), tmp_path)                          # components give 2,800.49
    assert st.accounts[0].closing_balance == -TOTAL and any("rounded" in w for w in st.warnings)
    assert st.accounts[0].integrity_errors() == []
    for wrong in ("2,801.00", "2,800.30"):                                                       # not the nearest rupee
        with pytest.raises(ValueError, match="does not add up"):
            parse(parser, build(PREV, rows(), total=wrong), tmp_path)


# ── Tata: the statement's own codes (legend: C D EN FP EMD BT M T), memo lines, tax lines, payable vs outstanding ──

def _tata(prev, rs, **kw):
    return tata_card(prev, rs, **kw)


@pytest.mark.parametrize("code", ["D", "EN", "FP", "EMD", "BT"])
def test_tata_debit_codes_mean_money_owed(code, tmp_path):
    rs = [crow("02/10/2026", "SHOP", "100"), crow("03/10/2026", "CASH", "250", tag=code)]
    (a,) = parse(TataCardParser(), _tata("1000.00", rs), tmp_path).accounts
    assert [t.amount for t in a.txns] == [Decimal("-100"), Decimal("-250")] and a.closing_balance == Decimal("-1350.00")


@pytest.mark.parametrize("code", ["C", "T"])
def test_tata_credit_codes_reduce_the_dues(code, tmp_path):
    rs = [crow("02/10/2026", "SHOP", "100"), crow("03/10/2026", "TEMP CREDIT", "40", credit=True, tag=code)]
    (a,) = parse(TataCardParser(), _tata("1000.00", rs), tmp_path).accounts
    assert a.txns[1].amount == Decimal("40") and a.closing_balance == Decimal("-1060.00")


def test_tata_unknown_code_is_refused(tmp_path):
    with pytest.raises(ValueError, match="unknown code 'ZZ'"):
        parse(TataCardParser(), _tata("1000.00", [crow("02/10/2026", "SHOP", "100", tag="ZZ")]), tmp_path)


@pytest.mark.parametrize("tag", ["M", "-"])
def test_tata_instalment_and_uncoded_lines_are_memo_not_movements(tag, tmp_path):
    rs = [crow("02/10/2026", "SHOP", "100"), crow("03/10/2026", "EMI LINE", "999", tag=tag)]
    st = parse(TataCardParser(), _tata("1000.00", rs), tmp_path)
    (a,) = st.accounts
    assert [t.description for t in a.txns] == ["SHOP"] and a.closing_balance == Decimal("-1100.00")
    assert any("left out" in w for w in st.warnings)


def test_tata_memo_lines_cannot_hide_a_real_charge(tmp_path):
    rs = [crow("02/10/2026", "SHOP", "100"), crow("03/10/2026", "REAL CHARGE", "75", tag="-")]
    pdf = _tata("1000.00", rs)                                    # printed totals leave the 75 out, so it is memo
    assert parse(TataCardParser(), pdf, tmp_path).accounts[0].closing_balance == Decimal("-1100.00")
    with pytest.raises(ValueError, match="statement says"):       # but if the totals include it, leaving it out is refused
        parse(TataCardParser(), _tata("1000.00", [crow("02/10/2026", "SHOP", "100")], total="1,175.00"), tmp_path)


def test_tata_tax_line_without_a_date_is_its_own_row(tmp_path):
    rs = [crow("02/10/2026", "SHOP", "100", after=("IGST @ 18%", "18.00")), crow("03/10/2026", "SHOP TWO", "50")]
    (a,) = parse(TataCardParser(), _tata("1000.00", rs), tmp_path).accounts
    assert [(t.date, t.description, t.amount) for t in a.txns][:2] == [
        (date(2026, 10, 2), "SHOP", Decimal("-100")), (date(2026, 10, 2), "IGST @ 18%", Decimal("-18.00"))]
    assert a.closing_balance == Decimal("-1168.00")


def test_tata_amount_payable_can_be_below_the_outstanding_balance(tmp_path):
    rs = [crow("02/10/2026", "SHOP", "100")]
    (a,) = parse(TataCardParser(), _tata("1000.00", rs, payable="300.00"), tmp_path).accounts
    assert a.closing_balance == Decimal("-1100.00") and a.amount_due == Decimal("300.00")


def test_amount_due_is_stored_next_to_the_balance(db_path, tmp_path):
    rs = [crow("02/10/2026", "SHOP", "100")]
    led = Ledger(db_path)
    led.import_statement(parse(TataCardParser(), _tata("1000.00", rs, payable="300.00"), tmp_path), sha256="t" * 64)
    row = led.conn.execute("SELECT total_due, amount_due FROM statement_summaries").fetchone()
    assert (row["total_due"], row["amount_due"]) == ("1100", "300")
    led.import_statement(parse(IciciCardParser(), icici_card(PREV, rows()), tmp_path), sha256="i" * 64)
    assert led.conn.execute("SELECT amount_due FROM statement_summaries WHERE total_due='2800.49'").fetchone()[0] == "2800.49"
