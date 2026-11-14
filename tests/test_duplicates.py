"""Same folio, written differently by different statements (the 'wrong totals after multiple imports' bug)."""

import sqlite3
from decimal import Decimal

import pytest

from finance import analytics
from finance.db import connect_ro
from finance.ledger import Ledger
from finance.parsers.cas import CasParser
from tests.conftest import FOLIO, make_cas

ALT = "1234567890 / 0"                 # same folio as FOLIO, spelled the way another statement writes it


def _imp(db, cas, sha):
    led = Ledger(db)
    try:
        return led.import_statement(CasParser().from_casdata(cas), sha256=sha)
    finally:
        led.close()


def _scenario(db, shift_days=0):
    """Statement 1 spells the folio one way and shows 115 units held; statement 2 spells it another way,
    repeats the history, and shows the fund sold (0 units)."""
    _imp(db, make_cas(), "s1")
    sold = make_cas(period=("2024-01-01", "2025-06-30"), folio=ALT, schemes=("A",))
    sold.folios[0].schemes[0].close = Decimal(0)
    sold.folios[0].schemes[0].close_calculated = Decimal(0)
    sold.folios[0].schemes[0].valuation.value = Decimal(0)
    sold.folios[0].schemes[0].valuation.cost = Decimal(0)
    if shift_days:                                   # the other statement dates each transaction differently
        from datetime import timedelta
        for t in sold.folios[0].schemes[0].transactions:
            t.date = t.date - timedelta(days=shift_days)
    _imp(db, sold, "s2")
    return sold


def _open(db, kind=None):
    return [dict(r) for r in analytics.open_review_items(connect_ro(db)) if kind in (None, r["kind"])]


def test_the_bug_reproduces_and_is_flagged(db_path):
    _scenario(db_path)
    conn = connect_ro(db_path)
    assert conn.execute("select count(*) from accounts").fetchone()[0] == 2        # one folio, two accounts
    assert len(analytics.portfolio(conn).rows) == 1                                # stale: the sold fund still shows
    dup = _open(db_path, "duplicate_folio")
    assert len(dup) == 1 and "identical transactions" in dup[0]["reason"]
    assert FOLIO not in dup[0]["reason"]


def test_merge_fixes_holdings_transactions_and_xirr_inputs(db_path):
    _scenario(db_path)
    item = _open(db_path, "duplicate_folio")[0]
    _, pa, pb = item["ref"].split(":")
    led = Ledger(db_path)
    res = led.merge_accounts(int(pa), int(pb))
    led.close()
    conn = connect_ro(db_path)
    assert conn.execute("select count(*) from accounts").fetchone()[0] == 1
    assert res["dropped"] == 3 and res["moved"] == 0                                # history was fully duplicated
    assert conn.execute("select count(*) from transactions where scheme_name like '%Flexi%'").fetchone()[0] == 3
    assert analytics.portfolio(conn).rows == []                                    # sold fund no longer shown
    assert _open(db_path, "duplicate_folio") == []
    assert _open(db_path, "folio_missing") == []


def test_future_imports_in_either_spelling_do_not_duplicate_again(db_path):
    sold = _scenario(db_path)
    item = _open(db_path, "duplicate_folio")[0]
    _, pa, pb = item["ref"].split(":")
    led = Ledger(db_path); led.merge_accounts(int(pa), int(pb)); led.close()

    again = _imp(db_path, sold, "s2-again")                                         # alt spelling, new file hash
    assert again.txn_new == 0 and again.txn_dup == 3
    conn = connect_ro(db_path)
    assert conn.execute("select count(*) from accounts").fetchone()[0] == 1
    _imp(db_path, make_cas(), "s1-again")                                            # original spelling
    assert conn.execute("select count(*) from transactions").fetchone()[0] == 5      # 3 (A) + 2 (B), nothing doubled
    assert _open(db_path, "duplicate_folio") == []


def test_new_transaction_seen_first_in_one_spelling_is_not_doubled_by_the_other(db_path):
    _scenario(db_path)
    item = _open(db_path, "duplicate_folio")[0]
    _, pa, pb = item["ref"].split(":")
    led = Ledger(db_path); led.merge_accounts(int(pa), int(pb)); led.close()
    # A later statement (original spelling) adds a brand-new SIP...
    later = make_cas(period=("2024-01-01", "2025-08-31"))
    from datetime import date
    from casparser.types import TransactionData
    later.folios[0].schemes[0].transactions.append(TransactionData(
        date=date(2025, 7, 10), description="SIP", amount=Decimal("5000"), units=Decimal("20"), nav=Decimal("250"),
        balance=Decimal("135"), type="PURCHASE_SIP"))
    first = _imp(db_path, later, "later-1")
    assert first.txn_new == 1
    # ...and the other spelling reports the same new SIP afterwards: must not be counted again.
    alt = make_cas(period=("2024-01-01", "2025-08-31"), folio=ALT, schemes=("A",))
    alt.folios[0].schemes[0].transactions.append(later.folios[0].schemes[0].transactions[-1])
    second = _imp(db_path, alt, "later-2")
    assert second.txn_new == 0
    assert connect_ro(db_path).execute("select count(*) from transactions where date='2025-07-10'").fetchone()[0] == 1


def test_genuinely_different_folios_are_not_flagged(db_path):
    _imp(db_path, make_cas(), "a")
    _imp(db_path, make_cas(folio="5555555555", variant=1), "b")
    assert _open(db_path, "duplicate_folio") == []


def test_keep_separate_is_final(db_path):
    _scenario(db_path)
    item = _open(db_path, "duplicate_folio")[0]
    led = Ledger(db_path); led.keep_separate(item["id"])
    assert led.flag_duplicate_accounts() == []                                       # not re-asked
    led.close()
    assert connect_ro(db_path).execute("select count(*) from accounts").fetchone()[0] == 2


def test_merge_validation_and_backup(db_path, tmp_path):
    _scenario(db_path)
    led = Ledger(db_path)
    with pytest.raises(ValueError):
        led.merge_accounts(1, 1)
    backup = led.backup(str(tmp_path / "bk"))
    led.close()
    assert oct(__import__("os").stat(backup).st_mode & 0o777) == "0o600"
    from finance import vault
    assert vault.require_vault().open_snapshot(backup).execute("select count(*) from accounts").fetchone()[0] == 2


def test_statements_that_date_transactions_differently_are_still_the_same_folio(db_path):
    """Real-world case: one statement dates a SIP by order date, the other by allotment date (weeks apart)."""
    _scenario(db_path, shift_days=33)
    item = _open(db_path, "duplicate_folio")
    assert len(item) == 1
    _, pa, pb = item[0]["ref"].split(":")
    led = Ledger(db_path); res = led.merge_accounts(int(pa), int(pb)); led.close()
    assert res == {"moved": 0, "dropped": 3}                      # nothing left double counted
    conn = connect_ro(db_path)
    assert conn.execute("select count(*) from transactions where scheme_name like '%Flexi%'").fetchone()[0] == 3
    again = _imp(db_path, make_cas(period=("2024-01-01", "2025-06-30"), folio=ALT, schemes=("A",)), "other-file")
    assert again.txn_new == 0                                     # a later statement in the other spelling adds nothing


def test_genuinely_new_transaction_after_a_merge_is_still_imported(db_path):
    _scenario(db_path, shift_days=5)
    item = _open(db_path, "duplicate_folio")[0]
    _, pa, pb = item["ref"].split(":")
    led = Ledger(db_path); led.merge_accounts(int(pa), int(pb)); led.close()
    from datetime import date
    from casparser.types import TransactionData
    later = make_cas(folio=ALT, schemes=("A",), period=("2024-01-01", "2025-09-30"))
    later.folios[0].schemes[0].transactions.append(TransactionData(
        date=date(2025, 8, 10), description="SIP", amount=Decimal("5000"), units=Decimal("20"), nav=Decimal("250"),
        balance=Decimal("135"), type="PURCHASE_SIP"))
    assert _imp(db_path, later, "later").txn_new == 1


# ── the same transaction dated differently by different statements, inside ONE folio ──

from datetime import date, timedelta


def _shifted(days, period=("2024-01-01", "2025-06-30"), **kw):
    c = make_cas(period=period, **kw)
    for sch in c.folios[0].schemes:
        for t in sch.transactions:
            t.date = t.date + timedelta(days=days)
    return c


def test_import_does_not_double_a_transaction_that_a_later_statement_dates_differently(db_path):
    _imp(db_path, make_cas(), "order-dates")
    later = _imp(db_path, _shifted(5, period=("2024-01-01", "2025-06-30")), "allotment-dates")
    assert later.txn_new == 0 and later.txn_dup == 5
    assert connect_ro(db_path).execute("select count(*) from transactions").fetchone()[0] == 5


def test_genuine_repeat_purchases_are_not_mistaken_for_duplicates(db_path):
    """buy 25 / sell 25 / buy 25 at the same NAV: identical purchase rows, but a single statement lists both."""
    from casparser.types import TransactionData
    cas = make_cas(schemes=("A",), extra_txn=False)
    t = lambda d, amt, u, bal, typ: TransactionData(date=date.fromisoformat(d), description="x", amount=Decimal(amt),
                                                    units=Decimal(u), nav=Decimal("200"), balance=Decimal(bal), type=typ)
    cas.folios[0].schemes[0].transactions += [t("2024-05-01", "5000", "25", "115", "PURCHASE"),
                                              t("2024-06-01", "-5000", "-25", "90", "REDEMPTION"),
                                              t("2024-07-01", "5000", "25", "115", "PURCHASE")]
    r = _imp(db_path, cas, "x")
    assert r.txn_new == 5 and r.txn_dup == 0
    assert _imp(db_path, cas, "x2").txn_new == 0                                    # and re-import is still idempotent


def test_stamp_duty_rows_from_a_later_window_are_never_dropped(db_path):
    from casparser.types import TransactionData
    sd = lambda d: TransactionData(date=date.fromisoformat(d), description="Stamp Duty", amount=Decimal("0.25"),
                                   type="STAMP_DUTY_TAX")
    c1 = make_cas(schemes=("A",)); c1.folios[0].schemes[0].transactions += [sd("2024-01-10"), sd("2024-02-10")]
    c2 = make_cas(schemes=("A",), period=("2024-01-01", "2025-09-30"))
    c2.folios[0].schemes[0].transactions += [sd("2025-03-10"), sd("2025-04-10")]            # genuinely new, same amount
    _imp(db_path, c1, "w1")
    assert _imp(db_path, c2, "w2").txn_new >= 2
    assert connect_ro(db_path).execute("select count(*) from transactions where kind='STAMP_DUTY_TAX'").fetchone()[0] == 4


def seed_same_account_duplicates(db):
    """Duplicates already in the DB (as written by the pre-fix importer): same account, different documents."""
    _imp(db, make_cas(schemes=("A",)), "first")
    led = Ledger(db)
    doc = led.conn.execute("INSERT INTO source_documents (source, sha256, status) VALUES ('imap','second','imported')").lastrowid
    for n, t in enumerate(led.conn.execute("SELECT * FROM transactions").fetchall()):
        led.conn.execute(
            "INSERT INTO transactions (account_id, doc_id, isin, scheme_name, date, kind, description, amount, units, nav, balance, idem_key) "
            "VALUES (?,?,?,?,date(?, '+5 day'),?,?,?,?,?,?,?)",
            (t["account_id"], doc, t["isin"], t["scheme_name"], t["date"], t["kind"], t["description"], t["amount"],
             t["units"], t["nav"], t["balance"], f"old-key-{n}"))
    led.conn.commit(); led.flag_duplicate_transactions(); led.close()


def test_cleanup_removes_cross_document_duplicates_only(db_path):
    seed_same_account_duplicates(db_path)
    items = _open(db_path, "duplicate_txns")
    assert len(items) == 1 and "3 transaction(s)" in items[0]["reason"]
    led = Ledger(db_path)
    assert led.remove_duplicate_transactions() == 3
    assert led.remove_duplicate_transactions() == 0
    led.close()
    conn = connect_ro(db_path)
    assert conn.execute("select count(*) from transactions").fetchone()[0] == 3
    assert [r[0] for r in conn.execute("select min(date) from transactions")] == ["2024-01-10"]    # first import wins
    assert _open(db_path, "duplicate_txns") == []


def test_cleanup_leaves_repeats_inside_one_document_alone(db_path):
    from casparser.types import TransactionData
    cas = make_cas(schemes=("A",), extra_txn=False)
    row = lambda d: TransactionData(date=date.fromisoformat(d), description="x", amount=Decimal("5000"),
                                    units=Decimal("25"), nav=Decimal("200"), balance=Decimal("115"), type="PURCHASE")
    cas.folios[0].schemes[0].transactions += [row("2024-05-01"), row("2024-07-01")]
    _imp(db_path, cas, "one-doc")
    led = Ledger(db_path)
    assert led.flag_duplicate_transactions() == [] and led.remove_duplicate_transactions() == 0
    led.close()
