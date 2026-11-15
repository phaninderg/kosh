"""Fixed deposits over time: a deposit a newer statement no longer lists has closed, and stops counting."""

from datetime import date
from decimal import Decimal

import pytest

from finance import analytics
from finance.db import connect_ro
from finance.ledger import Ledger
from finance.models import ParsedAccount, ParsedAccountStatement, ParsedDeposit
from finance.parsers.icici import IciciStatementParser
from tests.pdffix import icici_statement, row, section
from tests.test_web import authed, client  # noqa: F401

SAV = "123456789012"


def deposit(n, balance="50000", as_of=date(2026, 10, 31)):
    return ParsedDeposit(number=f"90000000000{n}", institution="ICICI Bank", linked_account=SAV, open_date=date(2026, 3, 5),
                         principal=Decimal(balance), rate=Decimal("6.75"), tenure="12 Mths 0 Days", maturity_date=date(2027, 3, 5),
                         maturity_amount=Decimal("53400"), current_balance=Decimal(balance), status="Registered", as_of=as_of)


def statement(as_of, nums, *, fd_total="auto", complete=True, summary=True):
    acct = ParsedAccount(type="bank", label="Savings", institution="ICICI Bank", identifier=SAV, period_from=date(2026, 10, 1),
                         period_to=as_of, opening_balance=Decimal("1000"), closing_balance=Decimal("1000"), txns=[])
    total = Decimal(50000 * len(nums)) if fd_total == "auto" else Decimal(fd_total)
    return ParsedAccountStatement(parser="icici", period_from=date(2026, 10, 1), period_to=as_of, accounts=[acct],
                                  deposits=[deposit(n, as_of=as_of) for n in nums], as_of=as_of,
                                  fd_summary={SAV: total} if summary else {}, fd_complete=complete)


@pytest.fixture
def led(db_path):
    return Ledger(db_path)


def held(db_path):
    """(open deposits' last digits, their total) as the Overview sees them, and every deposit's status."""
    conn = connect_ro(db_path)
    deps = analytics.accounts_overview(conn)["deposits"]
    return sorted(d["mask"][-1] for d in deps if d["status"] != "Closed"), analytics.overview(conn, date(2027, 1, 1))["assets"][1]["value"], \
        {d["mask"][-1]: d["status"] for d in deps}


def test_a_deposit_missing_from_the_next_statement_is_closed_and_leaves_net_worth(led, db_path):
    led.import_statement(statement(date(2026, 10, 31), [1, 2, 3]), sha256="a" * 64)
    assert held(db_path)[:2] == (["1", "2", "3"], Decimal("150000"))
    led.import_statement(statement(date(2026, 11, 30), [1, 2]), sha256="b" * 64)
    open_, total, status = held(db_path)
    assert open_ == ["1", "2"] and total == Decimal("100000") and status["3"] == "Closed"


def test_when_every_deposit_closes_the_total_goes_to_zero_but_the_history_stays(led, db_path):
    led.import_statement(statement(date(2026, 10, 31), [1, 2]), sha256="a" * 64)
    led.import_statement(statement(date(2026, 11, 30), [], fd_total="0"), sha256="b" * 64)
    open_, total, status = held(db_path)
    assert open_ == [] and total == 0 and status == {"1": "Closed", "2": "Closed"}
    assert connect_ro(db_path).execute("select count(*) from fixed_deposits").fetchone()[0] == 4          # 2 snapshots + 2 closing ones


def test_a_deposit_that_reappears_counts_again(led, db_path):
    led.import_statement(statement(date(2026, 10, 31), [1, 2]), sha256="a" * 64)
    led.import_statement(statement(date(2026, 11, 30), [1]), sha256="b" * 64)
    led.import_statement(statement(date(2026, 12, 31), [1, 2]), sha256="c" * 64)
    assert held(db_path)[:2] == (["1", "2"], Decimal("100000"))


def test_an_older_statement_imported_late_closes_nothing(led, db_path):
    led.import_statement(statement(date(2026, 10, 31), [1, 2, 3]), sha256="a" * 64)
    led.import_statement(statement(date(2026, 8, 31), [1]), sha256="b" * 64)
    assert held(db_path)[0] == ["1", "2", "3"]


def test_a_statement_that_does_not_report_linked_deposits_closes_nothing(led, db_path):
    led.import_statement(statement(date(2026, 10, 31), [1, 2]), sha256="a" * 64)
    led.import_statement(statement(date(2026, 11, 30), [], summary=False), sha256="b" * 64)      # the requested-period layout
    assert held(db_path)[0] == ["1", "2"]


def test_a_statement_with_an_unreadable_deposit_row_closes_nothing(led, db_path):
    led.import_statement(statement(date(2026, 10, 31), [1, 2, 3]), sha256="a" * 64)
    led.import_statement(statement(date(2026, 11, 30), [1, 2], complete=False), sha256="b" * 64)  # the third may just have been unreadable
    assert held(db_path)[0] == ["1", "2", "3"]


def test_importing_the_same_statement_again_changes_nothing(led, db_path):
    led.import_statement(statement(date(2026, 10, 31), [1, 2, 3]), sha256="a" * 64)
    st = statement(date(2026, 11, 30), [1, 2])
    led.import_statement(st, sha256="b" * 64)
    led.import_statement(st, sha256="b" * 64)
    led.import_statement(statement(date(2026, 11, 30), [1, 2]), sha256="c" * 64)                  # the same content from another file
    assert held(db_path)[:2] == (["1", "2"], Decimal("100000"))
    assert connect_ro(db_path).execute("select count(*) from fixed_deposits").fetchone()[0] == 6


def test_deposits_of_another_account_are_untouched(led, db_path):
    other = statement(date(2026, 10, 31), [1])
    other.accounts[0].identifier = "999999999999"
    other.deposits[0].linked_account = "999999999999"
    led.import_statement(other, sha256="a" * 64)
    led.import_statement(statement(date(2026, 11, 30), [], fd_total="0"), sha256="b" * 64)         # this account has none
    assert held(db_path)[0] == ["1"]


def test_the_parser_reads_the_summary_deposit_column_and_flags_an_unreadable_row(tmp_path):
    good = "900000000001 05-03-2026 50,000.00 6.75 12 Mths 0 Days 53,400.00 05-03-2027 50,000.00 Registered"
    odd = "900000000002 05-03-2026 40,000.00 an unexpected layout"
    pdf = icici_statement([section("Savings", SAV, "1000.00", [row("02-10-2026", "-100")])], fds=[dict(linked=SAV, text=good), dict(linked=SAV, text=odd)],
                          fd_column={SAV: "50,000.00"})
    path = tmp_path / "s.pdf"; path.write_bytes(pdf)
    st = IciciStatementParser().parse(path, None)
    assert st.fd_summary == {SAV: Decimal("50000.00")} and st.as_of == date(2026, 10, 31) and st.fd_complete is False
    clean = icici_statement([section("Savings", SAV, "1000.00", [row("02-10-2026", "-100")])], fds=[dict(linked=SAV, text=good)], fd_column={SAV: "50,000.00"})
    path.write_bytes(clean)
    assert IciciStatementParser().parse(path, None).fd_complete is True


def test_the_accounts_page_marks_closed_and_matured_deposits(client, led, db_path):
    led.import_statement(statement(date(2026, 10, 31), [1, 2]), sha256="a" * 64)
    led.import_statement(statement(date(2026, 11, 30), [1]), sha256="b" * 64)
    authed(client)
    page = client.get("/accounts").text
    assert "closed: not in the 2026-11-30 statement" in page
