"""The ICICI statement you request from net banking (a financial year or any period): same tables as the emailed combined
statement, but the header repeats on every page, one TOTAL row closes a section, and each page starts with lines that
are not part of the table. Synthetic PDFs only."""

from datetime import date
from decimal import Decimal

import pytest

from finance import analytics
from finance.db import connect_ro
from finance.ledger import Ledger
from finance.parsers.icici import IciciStatementParser
from tests.pdffix import encrypt, icici_requested_statement, icici_statement, row, section

SAV = "123456789012"
OTHER = "999900001111"
PERIOD = ("April 01, 2025", "March 31, 2026")


def rows(n=7):
    base = [
        row("05-04-2025", "-500", head=["UPI/100000000001/Coffee"], tail=["UPI/shop@bank/Bank"]),
        row("06-04-2025", "25000", head=["NEFT-ACME-SALARY"], tail=["REF 777"]),
        row("07-04-2025", "-1200.50", own="IMPS/200000000002/Rent"),
        row("08-04-2025", "-99", head=["UPI/300000000003/Snacks"], tail=["tail line one", "tail line two"]),
        row("09-04-2025", "150", own="INT.PD:01-01-2025 to 31-03-2025"),
        row("10-04-2025", "-5000", head=["CRED Club"], tail=["UPI/CRED Club/pay"], own="BIL/400000000004/CardBill"),
        row("11-04-2025", "10000", own="NEFT-REFUND"),
    ]
    return base[:n]


def parse(pdf, tmp_path, password=None):
    path = tmp_path / "r.pdf"
    path.write_bytes(pdf)
    return IciciStatementParser().parse(path, password)


def savings(rs=None, **kw):
    return icici_requested_statement([section("Savings", SAV, "10000.00", rs if rs is not None else rows(), period=PERIOD)], **kw)


def test_parses_a_requested_statement_with_the_repeating_header_and_one_total(tmp_path):
    st = parse(savings(), tmp_path)
    (a,) = st.accounts
    assert (a.type, a.label, a.identifier) == ("bank", "Savings", SAV)
    assert (a.opening_balance, len(a.txns)) == (Decimal("10000.00"), 7) and a.integrity_errors() == []
    assert a.closing_balance == Decimal("10000.00") + sum(Decimal(r["amount"]) for r in rows())
    assert (st.period_from, st.period_to) == (date(2025, 4, 1), date(2026, 3, 31)) and st.warnings == []
    assert [t.kind for t in a.txns] == ["DEBIT", "CREDIT", "DEBIT", "DEBIT", "CREDIT", "DEBIT", "CREDIT"]


@pytest.mark.parametrize("damage", ["both_amounts", "no_amount"])
def test_a_row_without_exactly_one_amount_is_refused(tmp_path, damage):
    rs = rows(3)
    rs[1] = {**rs[1], damage: True}
    with pytest.raises(ValueError, match="expected one amount"):
        parse(savings(rs), tmp_path)


def test_a_section_that_runs_over_many_pages_chains_balances_and_matches_its_one_total(tmp_path):
    st = parse(savings(rows_per_page=2), tmp_path)            # four pages, the header repeats on each, no Total in between
    (a,) = st.accounts
    assert len(a.txns) == 7 and a.integrity_errors() == []
    assert [t.date.day for t in a.txns] == [5, 6, 7, 8, 9, 10, 11]            # in order across the page breaks


def test_wrapped_lines_stay_with_their_row_across_a_page_break(tmp_path):
    a = parse(savings(rows_per_page=1), tmp_path).accounts[0]                 # every row on its own page
    by_amount = {t.amount: t.description for t in a.txns}
    assert "Coffee" in by_amount[Decimal("-500")] and "shop@bank" in by_amount[Decimal("-500")]
    assert "ACME-SALARY" in by_amount[Decimal("25000")] and "REF 777" in by_amount[Decimal("25000")]
    assert "tail line one" in by_amount[Decimal("-99")] and "tail line two" in by_amount[Decimal("-99")]
    assert "Snacks" not in by_amount[Decimal("-1200.50")] and "Rent" in by_amount[Decimal("-1200.50")]


def test_the_lines_at_the_top_of_every_page_never_reach_a_description(tmp_path):
    a = parse(savings(rows_per_page=2, furniture="MR. SECRET NAME"), tmp_path).accounts[0]
    assert not any("SECRET" in t.description or "Page" in t.description for t in a.txns)


def test_references_are_found_and_long_numbers_masked(tmp_path):
    a = parse(savings(), tmp_path).accounts[0]
    by_amount = {t.amount: t for t in a.txns}
    assert by_amount[Decimal("-500")].reference == "100000000001" and "100000000001" not in by_amount[Decimal("-500")].description


def test_an_account_whose_type_the_statement_does_not_name_is_skipped_with_a_warning(tmp_path):
    pdf = icici_requested_statement([section("", OTHER, "100.00", [row("05-04-2025", "-10")], period=PERIOD),
                                     section("Savings", SAV, "10000.00", rows(), period=PERIOD)])
    st = parse(pdf, tmp_path)
    assert [a.identifier for a in st.accounts] == [SAV]
    assert any("type the statement does not name" in w and OTHER[-4:] in w for w in st.warnings)


def test_a_second_section_after_the_first_total_is_read_too(tmp_path):
    pdf = icici_requested_statement([section("Savings", SAV, "10000.00", rows(3), period=PERIOD),
                                     section("PPF", "555500006666", "1000.00", [row("05-04-2025", "500")], period=PERIOD)],
                                    rows_per_page=2)
    st = parse(pdf, tmp_path)
    assert {a.type for a in st.accounts} == {"bank", "ppf"} and [len(a.txns) for a in st.accounts] == [3, 1]


@pytest.mark.parametrize("kw, match", [({"total_dep_error": "1.00"}, "page totals"), ({"total_bal_error": "1.00"}, "page totals")])
def test_a_total_that_disagrees_with_the_rows_is_refused(kw, match, tmp_path):
    with pytest.raises(ValueError, match=match):
        parse(savings(**kw), tmp_path)


def test_a_wrong_printed_balance_is_refused(tmp_path):
    bad = rows(3)
    bad[1] = row("06-04-2025", "25000", head=["NEFT-ACME-SALARY"], bal="999.00")
    with pytest.raises(ValueError, match="not the printed balance"):
        parse(savings(bad), tmp_path)


def _accounts(db_path):
    conn = connect_ro(db_path)
    return conn.execute("SELECT id, mask FROM accounts WHERE type='bank'").fetchall(), conn


def test_a_masked_monthly_statement_and_a_full_number_requested_one_are_the_same_account(db_path, tmp_path):
    led = Ledger(db_path)
    monthly = parse(icici_statement([section("Savings", "XXXXXXXX9012", "10000.00", rows(4))]), tmp_path)
    requested = parse(savings(), tmp_path)                                      # prints …123456789012 in full
    assert led.import_statement(monthly, sha256="a" * 64).txn_new == 4
    res = led.import_statement(requested, sha256="b" * 64)
    assert (res.txn_new, res.txn_dup) == (3, 4)                                 # the four shared rows are not added again
    accounts, conn = _accounts(db_path)
    assert len(accounts) == 1
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 7
    assert led.import_statement(parse(savings(), tmp_path), sha256="c" * 64).txn_new == 0       # and a third pass adds nothing


def test_an_older_split_into_two_accounts_is_merged_without_double_counting(db_path, tmp_path):
    led = Ledger(db_path)
    led.import_statement(parse(icici_statement([section("Savings", "XXXXXXXX9012", "10000.00", rows(4))]), tmp_path), sha256="a" * 64)
    with led.conn:                                                              # what the ledger used to do: a second account
        led.conn.execute("INSERT INTO accounts (type, institution, name, mask, ref_hash, liability) "
                         "VALUES ('bank', 'ICICI Bank', 'ICICI Bank Savings …9012', '9012', 'oldspelling', 0)")
        dup = led.conn.execute("SELECT id FROM accounts WHERE ref_hash='oldspelling'").fetchone()["id"]
        led.conn.execute("INSERT INTO transactions (account_id, doc_id, date, kind, description, amount, balance, idem_key) "
                         "SELECT ?, doc_id, date, kind, description, amount, balance, idem_key || 'x' FROM transactions", (dup,))
        led.conn.execute("INSERT INTO account_balances (account_id, as_of, balance, doc_id) VALUES (?, '2026-12-31', '777', 1)", (dup,))
    assert led.merge_same_account_spellings() == 1
    accounts, conn = _accounts(db_path)
    assert len(accounts) == 1 and conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 4
    assert conn.execute("SELECT MAX(as_of) FROM account_balances WHERE account_id=?", (accounts[0]["id"],)).fetchone()[0] == "2026-12-31"


def test_encrypted_pdf_opens_with_a_password(tmp_path):
    assert len(parse(encrypt(savings(), "abcd0101"), tmp_path, password="abcd0101").accounts[0].txns) == 7


def test_the_emailed_combined_statement_still_parses_the_same(tmp_path):
    st = parse(icici_statement([section("Savings", "XXXXXXXX1234", "1000.00", [row("02-10-2026", "-100")])]), tmp_path)
    assert st.accounts[0].txns[0].amount == Decimal("-100") and st.accounts[0].integrity_errors() == []


# ── through the ledger and the Import page ────────────────────────────────────

def test_import_is_idempotent_and_overlaps_with_other_statements_add_nothing(db_path, tmp_path):
    led = Ledger(db_path)
    st = parse(savings(), tmp_path)
    assert led.import_statement(st, sha256="a" * 64).txn_new == 7
    assert led.import_statement(st, sha256="a" * 64).status == "duplicate"
    part = parse(icici_requested_statement([section("Savings", SAV, "10000.00", rows(4), period=PERIOD)]), tmp_path)
    res = led.import_statement(part, sha256="b" * 64)                          # a shorter statement of the same account
    assert (res.txn_new, res.txn_dup) == (0, 4)
    led.close()
    assert analytics.accounts_overview(connect_ro(db_path))["accounts"][0]["type"] == "bank"


from tests.test_web import authed, client  # noqa: E402,F401


def test_the_import_page_reads_a_requested_statement_and_a_wrong_file_says_why(client):
    authed(client)
    ok = client.post("/import", files={"file": ("s.pdf", savings(), "application/pdf")}, data={"parser": "bank"})
    assert "Imported as <b>ICICI account statement</b>" in ok.text and "7 new transactions" in ok.text
    from tests.pdffix import build_pdf
    bad = client.post("/import", files={"file": ("x.pdf", build_pdf([[(40, 60, "Hello")]]), "application/pdf")}, data={"parser": "bank"})
    assert "no account transactions found; is this an ICICI account statement?" in bad.text
