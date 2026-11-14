import sqlite3

import pytest
from datetime import date
from decimal import Decimal

from finance import analytics
from finance.db import connect_ro
from finance.ledger import Ledger
from finance.parsers.cas import CasParser
from tests.conftest import FOLIO, make_cas


def _import(db, cas, sha):
    led = Ledger(db)
    try:
        return led.import_statement(CasParser().from_casdata(cas), sha256=sha)
    finally:
        led.close()


def test_import_is_idempotent(db_path):
    r1 = _import(db_path, make_cas(), "sha-1")
    assert (r1.status, r1.txn_new, r1.txn_dup) == ("imported", 5, 0)
    r2 = _import(db_path, make_cas(), "sha-1")          # same file
    assert r2.status == "duplicate"
    r3 = _import(db_path, make_cas(), "sha-2")          # same content, different file hash
    assert (r3.txn_new, r3.txn_dup) == (0, 5)
    conn = connect_ro(db_path)
    assert conn.execute("select count(*) from transactions").fetchone()[0] == 5
    assert conn.execute("select count(*) from accounts").fetchone()[0] == 1


def test_overlapping_statement_adds_only_new_txns(db_path):
    _import(db_path, make_cas(extra_txn=False, period=("2024-01-01", "2024-12-31")), "a")
    r = _import(db_path, make_cas(), "b")
    assert r.txn_new == 1 and r.txn_dup == 4


def test_full_folio_number_never_stored(db_path):
    _import(db_path, make_cas(), "x")
    dump = "\n".join(connect_ro(db_path).iterdump())
    assert FOLIO not in dump
    assert "…7890" in dump                                # last 4 only
    assert "Test Person" not in dump and "t@example.invalid" not in dump


def test_balance_mismatch_goes_to_review_queue(db_path):
    r = _import(db_path, make_cas(close_calc_off=True), "m")
    assert r.warnings
    assert len(analytics.open_review_items(connect_ro(db_path))) == 1


def test_scheme_missing_from_newer_statement_is_closed(db_path):
    _import(db_path, make_cas(), "old")
    _import(db_path, make_cas(period=("2024-01-01", "2025-03-31"), schemes=("B",)), "new")
    view = analytics.portfolio(connect_ro(db_path))
    assert view.rows == []                                # scheme A no longer held


def test_portfolio_metrics_use_decimal_and_statement_date(db_path):
    _import(db_path, make_cas(), "p")
    view = analytics.portfolio(connect_ro(db_path))
    assert len(view.rows) == 1                            # redeemed scheme B excluded
    row = view.rows[0]
    assert isinstance(view.total_value, Decimal)
    assert row.value == Decimal("24150") and row.cost == Decimal("15000")
    assert view.pnl == Decimal("9150") and view.abs_return_pct == Decimal("61.00")
    assert view.as_of == "2025-01-31" and view.xirr_pct and view.xirr_pct > 0


def test_failed_import_can_be_retried(db_path):
    led = Ledger(db_path)
    led.record_failure(sha256="r", parser="cas", error="bad password")
    res = led.import_statement(CasParser().from_casdata(make_cas()), sha256="r")
    led.close()
    assert res.status == "imported"


# ── folio missing from a newer statement ─────────────────────────────────────

from tests.conftest import FOLIO2


def _two_folio_setup(db):
    _import(db, make_cas(), "old")                                                   # folio 1 held
    return _import(db, make_cas(period=("2024-01-01", "2025-06-30"), folio=FOLIO2, variant=1,   # newer, other folio only
                                schemes=("A",)), "new")


def _open(db):
    return [dict(r) for r in analytics.open_review_items(connect_ro(db))]


def test_missing_folio_is_flagged_not_closed(db_path):
    r = _two_folio_setup(db_path)
    items = _open(db_path)
    assert len(items) == 1 and items[0]["kind"] == "folio_missing" and r.warnings
    assert "…7890" in items[0]["reason"] and FOLIO not in items[0]["reason"]
    assert len(analytics.portfolio(connect_ro(db_path)).rows) == 2        # nothing guessed


def test_missing_folio_close_decision(db_path):
    _two_folio_setup(db_path)
    led = Ledger(db_path); led.resolve_folio_missing(_open(db_path)[0]["id"], close=True); led.close()
    rows = analytics.portfolio(connect_ro(db_path)).rows
    assert len(rows) == 1 and rows[0].mask == "0001" and _open(db_path) == []


def test_missing_folio_keep_decision_and_no_repeat(db_path):
    _two_folio_setup(db_path)
    led = Ledger(db_path); led.resolve_folio_missing(_open(db_path)[0]["id"], close=False)
    assert len(analytics.portfolio(connect_ro(db_path)).rows) == 2
    with pytest.raises(ValueError):                                         # already resolved
        led.resolve_folio_missing(1, close=True)
    led.close()


def test_missing_folio_question_not_duplicated_and_auto_resolved_when_folio_returns(db_path):
    _two_folio_setup(db_path)
    _import(db_path, make_cas(period=("2024-01-01", "2025-06-30"), folio=FOLIO2, variant=1, schemes=("A",)), "new-again")
    assert len(_open(db_path)) == 1                                         # same date: not re-queued
    _import(db_path, make_cas(period=("2024-01-01", "2025-09-30")), "folio1-back")
    refs = [i["ref"] for i in _open(db_path)]                               # folio 1 is back: its question is moot;
    assert len(refs) == 1 and refs[0].startswith("folio_missing:2:")        # folio 2 is now the one that's absent


def test_older_statement_never_flags(db_path):
    _import(db_path, make_cas(period=("2024-01-01", "2025-06-30")), "newer")
    _import(db_path, make_cas(period=("2023-01-01", "2024-06-30"), folio=FOLIO2, variant=1, schemes=("A",)), "older")
    assert _open(db_path) == []


def test_phase1_database_upgrades_in_place(tmp_path):
    from finance import db
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.executescript(db.SCHEMA)                          # exactly what phase 1 created
    raw.execute("PRAGMA user_version = 1")
    raw.execute("INSERT INTO source_documents (source, sha256, status) VALUES ('upload', 'abc', 'imported')")
    raw.commit(); raw.close()

    conn = db.connect_rw(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    row = conn.execute("SELECT sha256, sender FROM source_documents").fetchone()
    assert (row["sha256"], row["sender"]) == ("abc", "")                  # data kept, new column defaulted
    assert conn.execute("SELECT count(*) FROM imap_state").fetchone()[0] == 0
    db.connect_rw(path).close()                                          # idempotent on a second open


def test_read_only_open_upgrades_an_old_database(tmp_path):
    """Regression: the Mailboxes page opened a phase-1 DB read-only and hit 'no such table: imap_state'."""
    from finance import db
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.executescript(db.SCHEMA); raw.execute("PRAGMA user_version = 1"); raw.commit(); raw.close()
    conn = db.connect_ro(path)
    assert conn.execute("SELECT count(*) FROM imap_state").fetchone()[0] == 0
    with pytest.raises(sqlite3.OperationalError):                 # still genuinely read-only
        conn.execute("INSERT INTO review_queue (kind, reason) VALUES ('x','y')")


def test_redemptions_list_units_proceeds_and_whether_the_fund_is_still_held(db_path, stmt):
    from finance import analytics
    from finance.db import connect_ro
    led = Ledger(db_path)
    led.import_statement(stmt, sha256="a" * 64)
    led.close()
    (r,) = analytics.redemptions(connect_ro(db_path))
    assert r["scheme_name"].startswith("Synthetic Liquid") and r["units"] == Decimal("100") and r["amount"] == Decimal("12000")
    assert (r["last"], r["count"], r["held"]) == ("2024-09-01", 1, Decimal(0))


def test_a_partly_redeemed_fund_shows_what_is_still_held(db_path):
    from casparser.types import TransactionData
    from finance import analytics
    cas = make_cas()
    a = cas.folios[0].schemes[0]
    a.transactions.append(TransactionData(date=date(2025, 1, 20), description="Redemption", amount=Decimal("-2000"),
                                          units=Decimal("-10"), nav=Decimal("200"), balance=Decimal("105"), type="REDEMPTION"))
    a.close = a.close_calculated = Decimal("105")
    a.valuation.value = Decimal("105") * 210
    led = Ledger(db_path)
    led.import_statement(CasParser().from_casdata(cas), sha256="p" * 64)
    led.close()
    by_name = {r["scheme_name"]: r for r in analytics.redemptions(connect_ro(db_path))}
    part = by_name["Synthetic Flexi Cap Fund - Direct Growth"]
    assert (part["units"], part["amount"], part["held"]) == (Decimal("10"), Decimal("2000"), Decimal("105"))
    assert by_name["Synthetic Liquid Fund - Direct Growth"]["held"] == 0


def test_a_fund_that_dropped_out_of_a_newer_statement_is_listed_without_proceeds(db_path):
    from finance import analytics
    led = Ledger(db_path)
    led.import_statement(CasParser().from_casdata(make_cas(schemes=("A", "B"))), sha256="1" * 64)
    led.import_statement(CasParser().from_casdata(make_cas(schemes=("B",), period=("2025-02-01", "2025-03-31"))), sha256="2" * 64)
    led.close()
    by_name = {r["scheme_name"]: r for r in analytics.redemptions(connect_ro(db_path))}
    gone = by_name["Synthetic Flexi Cap Fund - Direct Growth"]
    assert (gone["units"], gone["amount"], gone["recorded"], gone["held"], gone["last"]) == (
        Decimal("115"), None, False, Decimal(0), "2025-03-31")
    assert by_name["Synthetic Liquid Fund - Direct Growth"]["recorded"] is True
    assert connect_ro(db_path).execute("SELECT COUNT(*) FROM transactions WHERE isin='INF000SYN001' AND kind LIKE '%REDEM%'").fetchone()[0] == 0
