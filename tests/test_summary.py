"""Summary statements: no fund house, no transactions. They must update holdings without duplicating anything."""

from decimal import Decimal

from finance import analytics
from finance.db import connect_ro
from finance.ledger import Ledger
from finance.parsers.cas import CasParser
from tests.conftest import FOLIO, make_cas, make_summary

A_ISIN = "INF000SYN001"


def _imp(db, cas, sha):
    led = Ledger(db)
    try:
        return led.import_statement(CasParser().from_casdata(cas), sha256=sha)
    finally:
        led.close()


def _accounts(db):
    return [dict(r) for r in connect_ro(db).execute("select id, institution, mask from accounts order by id")]


def _open(db, kind):
    return [dict(r) for r in analytics.open_review_items(connect_ro(db)) if r["kind"] == kind]


def test_summary_is_recognised(db_path):
    stmt = CasParser().from_casdata(make_summary())
    assert stmt.kind == "summary" and stmt.parser == "cas_summary" and stmt.period_from == stmt.period_to
    assert CasParser().from_casdata(make_cas()).kind == "detailed"


def test_summary_updates_the_existing_folio_instead_of_creating_one(db_path):
    _imp(db_path, make_cas(schemes=("A",)), "detailed")                       # 115 units, history complete
    r = _imp(db_path, make_summary(as_of="2025-03-31"), "summary")
    assert len(_accounts(db_path)) == 1 and _accounts(db_path)[0]["institution"] == "Synthetic AMC"
    assert r.txn_new == 0 and any("Summary statement" in w for w in r.warnings)
    conn = connect_ro(db_path)
    assert conn.execute("select count(*) from transactions").fetchone()[0] == 3        # untouched
    view = analytics.portfolio(conn)
    assert len(view.rows) == 1 and view.rows[0].as_of == "2025-03-31" and view.total_value == Decimal("28750")


def test_xirr_is_withheld_when_units_exceed_what_transactions_explain(db_path):
    _imp(db_path, make_cas(schemes=("A",)), "detailed")
    _imp(db_path, make_summary(folios=[(FOLIO, [(A_ISIN, "Synthetic Flexi Cap Fund", "140", "250", "20000")])]), "s")
    view = analytics.portfolio(connect_ro(db_path))                                       # 25 units bought since
    row = view.rows[0]
    assert row.history_gap == Decimal("25") and row.xirr_pct is None
    assert view.xirr_pct is None and view.xirr_funds == 0


def test_xirr_is_kept_when_the_summary_matches_the_recorded_history(db_path):
    _imp(db_path, make_cas(schemes=("A",)), "detailed")
    _imp(db_path, make_summary(), "summary")                                              # same 115 units, later date
    row = analytics.portfolio(connect_ro(db_path)).rows[0]
    assert row.history_gap is None and row.xirr_pct is not None


def test_portfolio_xirr_only_uses_funds_with_complete_history(db_path):
    _imp(db_path, make_cas(schemes=("A",)), "detailed")
    _imp(db_path, make_summary(folios=[
        (FOLIO, [(A_ISIN, "Synthetic Flexi Cap Fund", "115", "250", "15000")]),
        ("5555 / 1", [("INF000SYN009", "Other Fund", "10", "100", None)])]), "s")
    view = analytics.portfolio(connect_ro(db_path))
    assert len(view.rows) == 2 and view.xirr_funds == 1 and view.cost_missing == 1 and view.xirr_pct is not None


# ── matching a summary folio to a known one ──────────────────────────────────

def test_other_spelling_with_check_digit_suffix_matches_by_folio_ending(db_path):
    _imp(db_path, make_cas(schemes=("A",)), "detailed")
    _imp(db_path, make_summary(folios=[(FOLIO + " / 0", [(A_ISIN, "Synthetic Flexi Cap Fund", "115", "250", None)])]), "s")
    assert len(_accounts(db_path)) == 1
    _imp(db_path, make_summary(as_of="2025-04-30", folios=[(FOLIO + " / 0", [(A_ISIN, "Synthetic Flexi Cap Fund", "118", "260", None)])]), "s2")
    assert len(_accounts(db_path)) == 1                                                   # alias remembered


def test_unrecognisable_spelling_matches_when_the_units_are_identical(db_path):
    _imp(db_path, make_cas(schemes=("A",)), "detailed")
    _imp(db_path, make_summary(folios=[("98765 / 4", [(A_ISIN, "Synthetic Flexi Cap Fund", "115", "250", None)])]), "s")
    assert len(_accounts(db_path)) == 1


def test_unknown_folio_is_kept_as_a_placeholder_and_adopted_by_a_later_detailed_statement(db_path):
    r = _imp(db_path, make_summary(folios=[("4444333322", [("INF000SYN001", "Synthetic Flexi Cap Fund", "50", "200", "9000")])]), "s")
    accts = _accounts(db_path)
    assert [a["institution"] for a in accts] == ["Unknown fund house"] and any("could not be matched" in w for w in r.warnings)
    _imp(db_path, make_cas(schemes=("A",), folio="4444333322"), "detailed")              # names the fund house, same folio
    accts = _accounts(db_path)
    assert len(accts) == 1 and accts[0]["institution"] == "Synthetic AMC"
    assert connect_ro(db_path).execute("select count(*) from holdings").fetchone()[0] == 2    # both snapshots kept


def test_detailed_statement_with_other_spelling_adopts_the_placeholder_by_ending_and_fund(db_path):
    _imp(db_path, make_summary(folios=[("12345678", [(A_ISIN, "Synthetic Flexi Cap Fund", "50", "200", None)])]), "s")
    _imp(db_path, make_cas(schemes=("A",), folio="12345678 / 0"), "detailed")             # core ends the same
    assert len(_accounts(db_path)) == 1 and _accounts(db_path)[0]["institution"] == "Synthetic AMC"


def test_unmatched_placeholder_is_queued_for_review_and_merges_cleanly(db_path):
    _imp(db_path, make_cas(schemes=("A",)), "detailed")                                    # one known holder of the fund
    _imp(db_path, make_summary(as_of="2025-04-30",
                               folios=[("777666555", [(A_ISIN, "Synthetic Flexi Cap Fund", "999", "250", None)])]), "s")
    assert [a["institution"] for a in _accounts(db_path)] == ["Synthetic AMC", "Unknown fund house"]
    item = _open(db_path, "duplicate_folio")
    assert len(item) == 1 and "summary statement" in item[0]["reason"]
    _, pa, pb = item[0]["ref"].split(":")
    led = Ledger(db_path); led.merge_accounts(int(pa), int(pb)); led.close()
    assert [a["institution"] for a in _accounts(db_path)] == ["Synthetic AMC"]
    again = _imp(db_path, make_summary(as_of="2025-05-31",
                                       folios=[("777666555", [(A_ISIN, "Synthetic Flexi Cap Fund", "999", "250", None)])]), "s3")
    assert len(_accounts(db_path)) == 1 and again.status == "imported"                  # the new spelling is now an alias


def test_same_folio_number_at_two_fund_houses_is_resolved_by_the_fund_held(db_path):
    _imp(db_path, make_cas(schemes=("A",), folio="1111"), "x")                           # Synthetic AMC, folio 1111, fund SYN001
    other = make_cas(schemes=("A",), folio="1111", variant=3)
    other.folios[0].amc = "Other AMC"
    other.folios[0].schemes[0].isin = "INF000SYN777"
    _imp(db_path, other, "y")                                                              # Other AMC, same folio number
    assert len(_accounts(db_path)) == 2
    _imp(db_path, make_summary(folios=[("1111", [("INF000SYN777", "Other Fund", "115", "250", None)])]), "s")
    assert len(_accounts(db_path)) == 2                                                    # went to Other AMC's account


def test_folio_absent_from_a_summary_is_flagged_not_closed(db_path):
    _imp(db_path, make_cas(schemes=("A",)), "detailed")
    _imp(db_path, make_summary(as_of="2025-06-30", folios=[("9999", [("INF000SYN555", "Another", "5", "10", None)])]), "s")
    assert len(_open(db_path, "folio_missing")) == 1
    assert any(r.isin == A_ISIN for r in analytics.portfolio(connect_ro(db_path)).rows)    # still held until you decide
