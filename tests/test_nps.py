"""NPS (CRA) statement parser: both layouts, integrity gates, ledger import, NPS view. Synthetic PDFs only."""

from datetime import date
from decimal import Decimal

import pytest

from finance import analytics
from finance.db import connect_ro
from finance.ledger import Ledger
from finance.parsers.nps import NpsParser
from tests.cardfix import crow  # noqa: F401  (keeps the shared fixtures importable)
from tests.npsfix import NAMES, nps_statement, nrow, nscheme
from tests.pdffix import encrypt


def schemes(opening=("100.0000", "200.0000", "10.0000")):
    return [
        nscheme(NAMES[0], opening[0], "50.0000", [nrow("05-Jan-26", "5000.00", "50.0000"), nrow("31-Jan-26", "-12.00", "50.0000")]),
        nscheme(NAMES[1], opening[1], "30.0000", [nrow("05-Jan-26", "3000.00", "30.0000")]),
        nscheme(NAMES[2], opening[2], "20.0000", [nrow("05-Jan-26", "1000.00", "20.0000")]),
    ]


def parse(pdf, tmp_path, password=None):
    path = tmp_path / "n.pdf"
    path.write_bytes(pdf)
    return NpsParser().parse(path, password)


LAYOUTS = pytest.mark.parametrize("layout", ["seq", "cols"])


# ── parser ────────────────────────────────────────────────────────────────────

@LAYOUTS
def test_parses_schemes_units_nav_value_and_rows(layout, tmp_path):
    st = parse(nps_statement(schemes(), layout=layout), tmp_path)
    (folio,) = st.folios
    assert (st.account_type, st.parser, st.kind) == ("nps", "nps", "detailed")
    assert (st.period_from, st.period_to) == (date(2026, 1, 1), date(2026, 1, 31))
    assert folio.amc == "NPS Tier I" and folio.folio == "TI-123456789012"
    assert [s.scheme_name for s in folio.schemes] == NAMES
    first = folio.schemes[0]
    assert (first.close_units, first.nav, first.value) == (Decimal("199.7600"), Decimal("50.0000"), Decimal("9988.00"))
    assert first.close_calculated == first.close_units and first.isin == "NPS:ACME-FUND-E-TIER-I"
    assert [(t.kind, t.amount, t.units) for t in first.txns] == [
        ("PURCHASE_NPS", Decimal("5000.00"), Decimal("100.0000")), ("CHARGE_NPS", Decimal("-12.00"), Decimal("-0.2400"))]
    assert [t.balance for t in first.txns] == [Decimal("200.0000"), Decimal("199.7600")]       # the running unit balance
    assert [len(s.txns) for s in folio.schemes] == [2, 1, 1] and st.warnings == []


@LAYOUTS
def test_tier_two_is_its_own_account(layout, tmp_path):
    st = parse(nps_statement(schemes(), layout=layout, tier="II"), tmp_path)
    assert st.folios[0].amc == "NPS Tier II" and st.folios[0].folio == "TII-123456789012"


@LAYOUTS
def test_encrypted_pdf_opens_with_password(layout, tmp_path):
    st = parse(encrypt(nps_statement(schemes(), layout=layout), "abcd0101"), tmp_path, password="abcd0101")
    assert len(st.folios[0].schemes) == 3


@LAYOUTS
def test_wrong_closing_balance_is_refused(layout, tmp_path):
    with pytest.raises(ValueError, match="statement says"):
        parse(nps_statement(schemes(), layout=layout, closing_error="150.0000"), tmp_path)


@LAYOUTS
def test_value_that_is_not_units_times_nav_is_refused(layout, tmp_path):
    with pytest.raises(ValueError, match="units x NAV"):
        parse(nps_statement(schemes(), layout=layout, value_error="1.00"), tmp_path)


@LAYOUTS
def test_holdings_table_units_must_match_the_closing_balance(layout, tmp_path):
    with pytest.raises(ValueError, match="differ from the holdings table"):
        parse(nps_statement(schemes(), layout=layout, table_units_error="1.0000"), tmp_path)


@LAYOUTS
def test_contributions_table_disagreement_is_only_a_warning(layout, tmp_path):
    st = parse(nps_statement(schemes(), layout=layout, contributions="1.00"), tmp_path)
    assert len(st.folios[0].schemes) == 3 and any("contributions table totals" in w for w in st.warnings)


def test_portal_downloads_with_four_digit_years_and_full_month_names_parse(tmp_path):
    st = parse(nps_statement(schemes(), portal=True, period=("April 01, 2025", "March 31, 2026")), tmp_path)
    assert (st.period_from, st.period_to) == (date(2025, 4, 1), date(2026, 3, 31))
    first = st.folios[0].schemes[0]
    assert first.txns[0].date == date(2026, 1, 5) and first.close_units == Decimal("199.7600") and st.warnings == []


def test_not_an_nps_statement_is_refused(tmp_path):
    from tests.pdffix import build_pdf
    with pytest.raises(ValueError, match="layout not recognised"):
        parse(build_pdf([[(40, 60, "Hello world")]]), tmp_path)


# ── ledger ────────────────────────────────────────────────────────────────────

@pytest.fixture
def stmt(tmp_path):
    return parse(nps_statement(schemes()), tmp_path)


def test_import_creates_an_nps_account_and_never_touches_the_fund_portfolio(db_path, stmt):
    led = Ledger(db_path)
    res = led.import_statement(stmt, sha256="a" * 64)
    assert (res.status, res.txn_new, res.holdings) == ("imported", 4, 3)
    assert led.import_statement(stmt, sha256="a" * 64).status == "duplicate"
    led.close()
    conn = connect_ro(db_path)
    acct = conn.execute("SELECT * FROM accounts").fetchone()
    assert (acct["type"], acct["institution"], acct["mask"], acct["liability"]) == ("nps", "NPS Tier I", "9012", 0)
    assert "123456789012" not in str(dict(acct).values())                       # the PRAN is hashed, not stored
    assert analytics.portfolio(conn).rows == [] and analytics.redemptions(conn) == []
    assert len(analytics.current_holdings(conn, "nps")) == 3


def test_overlapping_monthly_and_annual_layouts_do_not_double_count(db_path, tmp_path):
    led = Ledger(db_path)
    led.import_statement(parse(nps_statement(schemes(), layout="seq"), tmp_path), sha256="a" * 64)
    again = led.import_statement(parse(nps_statement(schemes(), layout="cols"), tmp_path), sha256="b" * 64)
    assert (again.txn_new, again.txn_dup) == (0, 4)


def test_nps_statements_do_not_create_folio_missing_items_for_funds_and_back(db_path, stmt):
    from casparser.types import CASData  # noqa: F401
    from finance.parsers.cas import CasParser
    from tests.conftest import make_cas
    led = Ledger(db_path)
    led.import_statement(CasParser().from_casdata(make_cas()), sha256="m" * 64)       # a fund statement first ...
    res = led.import_statement(stmt, sha256="n" * 64)                                 # ... then an NPS one, later in time
    assert not any("folio" in w for w in res.warnings)
    res = led.import_statement(CasParser().from_casdata(make_cas(period=("2024-01-01", "2026-03-31"), variant=1)), sha256="o" * 64)
    assert led.conn.execute("SELECT COUNT(*) FROM review_queue WHERE kind='folio_missing'").fetchone()[0] == 0






# ── pages and mailbox sync ────────────────────────────────────────────────────

from tests.test_web import authed, client  # noqa: E402,F401


def test_accounts_page_has_an_nps_section_and_transactions_an_nps_tab(client, db_path, stmt):
    led = Ledger(db_path)
    led.import_statement(stmt, sha256="a" * 64)
    led.close()
    authed(client)
    page = client.get("/accounts").text
    assert "<h2>NPS</h2>" in page and NAMES[0] in page and "NPS Tier I …9012" in page
    assert "Gain and XIRR are not shown because the history is incomplete" in page          # the fixture opens with units
    assert ">NPS<" in client.get("/transactions").text
    assert "PURCHASE_NPS" in client.get("/transactions?account_type=nps").text
    assert "Mutual fund portfolio" in client.get("/portfolio").text and NAMES[0] not in client.get("/portfolio").text


def test_import_page_reads_an_nps_statement(client):
    authed(client)
    r = client.post("/import", files={"file": ("n.pdf", nps_statement(schemes(), layout="cols"), "application/pdf")},
                    data={"parser": "nps"})
    assert "Imported as <b>NPS statement</b>" in r.text and "4 new transactions" in r.text


def test_nps_statement_email_is_imported(db_path, tmp_path, monkeypatch):
    from tests.test_cards import _sync
    a = _sync(db_path, tmp_path, encrypt(nps_statement(schemes()), "test1705"), "nps", monkeypatch)
    assert (a.imported, a.attention, a.error, a.txn_new) == (1, 0, "", 4)
    assert connect_ro(db_path).execute("SELECT type FROM accounts").fetchone()[0] == "nps"









# ── NPS as one account: money in, value now, whatever the schemes ─────────────────────────────────────────────

SHIFT = "By Contribution Shifting"
JAN, FEB, MAR = ("Jan 01, 2026", "Jan 31, 2026"), ("Feb 01, 2026", "Feb 28, 2026"), ("Mar 01, 2026", "Mar 31, 2026")


def import_all(db_path, tmp_path, *stmts):
    led = Ledger(db_path)
    for sha, (sch, per) in zip("abcdef", stmts):
        led.import_statement(parse(nps_statement(sch, period=per), tmp_path), sha256=sha * 64)
    led.close()
    return analytics.nps_overview(connect_ro(db_path))


def test_value_contributed_gain_and_xirr_for_the_whole_account(db_path, tmp_path):
    ov = import_all(db_path, tmp_path, (schemes(opening=("0", "0", "0")), JAN))
    assert len(ov["rows"]) == 3 and ov["as_of"] == "2026-01-31" and ov["complete"] and ov["reasons"] == []
    assert ov["total_contributed"] == Decimal("9000.00") and ov["total_value"] == sum(r["value"] for r in ov["rows"])
    assert ov["gain"] == ov["total_value"] - Decimal("9000.00") and ov["xirr_pct"] is not None
    assert sum(r["share_pct"] for r in ov["rows"]) in (Decimal("100.0"), Decimal("99.9"), Decimal("100.1"))


def test_switching_schemes_is_not_new_money_and_the_old_scheme_still_counts(db_path, tmp_path):
    """Money goes in through scheme A; later A is replaced by B, which gets A's value as a 'shifting' row."""
    a = [nscheme(NAMES[0], "0", "50.0000", [nrow("05-Jan-26", "5000.00", "50.0000")])]                 # 100 units
    b = [nscheme(NAMES[1], "0", "40.0000", [nrow("10-Feb-26", "5500.00", "55.0000", desc=SHIFT),        # A's 100 units at 55
                                            nrow("11-Feb-26", "1000.00", "40.0000", month="February,2026")])]
    ov = import_all(db_path, tmp_path, (a, JAN), (b, FEB))
    assert [r["scheme_name"] for r in ov["rows"]] == [NAMES[1]]                      # only what is held now is listed
    assert ov["total_contributed"] == Decimal("6000.00")                              # 5,000 + 1,000: the shift is not money in
    assert ov["complete"] and ov["gain"] == ov["total_value"] - Decimal("6000.00") and ov["xirr_pct"] is not None
    kinds = {k[0] for k in connect_ro(db_path).execute("SELECT DISTINCT kind FROM transactions WHERE amount = '5500'")}
    assert kinds == {"SHIFT_NPS"}


def test_moving_back_to_an_earlier_scheme_needs_no_special_handling(db_path, tmp_path):
    a = [nscheme(NAMES[0], "0", "50.0000", [nrow("05-Jan-26", "5000.00", "50.0000")])]
    b = [nscheme(NAMES[1], "0", "55.0000", [nrow("10-Feb-26", "5500.00", "55.0000", desc=SHIFT)])]
    back = [nscheme(NAMES[0], "0", "60.0000", [nrow("10-Mar-26", "6000.00", "60.0000", desc=SHIFT)])]
    ov = import_all(db_path, tmp_path, (a, JAN), (b, FEB), (back, MAR))
    assert [r["scheme_name"] for r in ov["rows"]] == [NAMES[0]] and ov["total_contributed"] == Decimal("5000.00")
    assert ov["complete"] and ov["xirr_pct"] is not None


def test_a_history_that_opens_with_units_gives_no_gain_or_xirr(db_path, tmp_path):
    ov = import_all(db_path, tmp_path, (schemes(), JAN))                                 # the first statement opens with units
    assert not ov["complete"] and ov["gain"] is None and ov["xirr_pct"] is None
    assert "already opens with units" in ov["reasons"][0] and ov["total_contributed"] == Decimal("9000.00")


def test_a_stretch_with_no_statement_gives_no_gain_or_xirr(db_path, tmp_path):
    jan = [nscheme(NAMES[0], "0", "50.0000", [nrow("05-Jan-26", "5000.00", "50.0000")])]
    mar = [nscheme(NAMES[0], "100.0000", "60.0000", [nrow("05-Mar-26", "6000.00", "60.0000", month="March,2026")])]
    ov = import_all(db_path, tmp_path, (jan, JAN), (mar, MAR))                            # February is missing
    assert not ov["complete"] and ov["xirr_pct"] is None and "no statement covers 2026-02-01 to 2026-02-28" in ov["reasons"][0]


def test_overlapping_statements_do_not_break_the_continuity_check(db_path, tmp_path):
    jan = [nscheme(NAMES[0], "0", "50.0000", [nrow("05-Jan-26", "5000.00", "50.0000")])]
    both = [nscheme(NAMES[0], "0", "55.0000", [nrow("05-Jan-26", "5000.00", "50.0000"),
                                               nrow("05-Feb-26", "5500.00", "55.0000", month="February,2026")])]
    ov = import_all(db_path, tmp_path, (jan, JAN), (both, ("Jan 01, 2026", "Feb 28, 2026")))
    assert ov["complete"] and ov["total_contributed"] == Decimal("10500.00")


@pytest.mark.parametrize("description, kind", [
    ("By Contribution On Account of Subscriber Shifting", "SHIFT_NPS"),        # the statement's own wording
    ("By Contribution for January,2026", "PURCHASE_NPS"), ("By Contribution (August-2024)", "PURCHASE_NPS"),
    ("By Voluntary Contributions", "PURCHASE_NPS"), ("Billing for Q1, 2026-2027", "CHARGE_NPS"),
])
def test_row_descriptions_are_classified(description, kind):
    from finance.parsers.nps import _kind
    assert _kind(description, Decimal(1)) == kind


# ── the same scheme under different spellings (emailed vs portal, renamed over the years) ──────────────────────

def test_scheme_identity_survives_renames_and_the_pop_suffix():
    from finance.schemes import scheme_id
    same = ["ICICI PENSION FUND SCHEME E - TIER I POP", "ICICI PRUDENTIAL PENSION FUND SCHEME E - TIER I",
            "ICICI PRUDENTIAL PENSION FUND SCHEME E - TIER I POP", "icici  pension fund scheme e - tier i"]
    assert {scheme_id(n) for n in same} == {"NPS:ICICI-SCHEME-E-TIER-I"}
    assert scheme_id("ICICI PENSION FUND SCHEME C - TIER I") != scheme_id("ICICI PENSION FUND SCHEME E - TIER I")
    assert scheme_id("ICICI PENSION FUND SCHEME E - TIER II") != scheme_id("ICICI PENSION FUND SCHEME E - TIER I")
    assert scheme_id("SBI PENSION FUNDS SCHEME E - TIER I") != scheme_id("ICICI PENSION FUND SCHEME E - TIER I")    # another house
    assert scheme_id("SBI PENSION FUNDS PVT LTD SCHEME - CORPORATE-CG") == "NPS:SBI-PENSION-FUNDS-PVT-LTD-SCHEME-CORPORATE-CG"
    assert scheme_id("ACME FUND E - TIER I") == "NPS:ACME-FUND-E-TIER-I"                                   # no 'SCHEME': by name
    assert scheme_id("SBI PRUDENTIAL PENSION FUNDS PVT LTD SCHEME - CORPORATE-CG POP") == scheme_id("SBI PENSION FUNDS PVT LTD SCHEME - CORPORATE-CG")


OLD, NEW = "ICICI PENSION SCHEME E - TIER I POP", "ICICI PRUDENTIAL SCHEME E - TIER I"


def two_spellings():
    jan = [nscheme(OLD, "0", "50.0000", [nrow("05-Jan-26", "5000.00", "50.0000")])]
    both = [nscheme(NEW, "0", "55.0000", [nrow("05-Jan-26", "5000.00", "50.0000"),
                                          nrow("05-Feb-26", "5500.00", "55.0000", month="February,2026")])]
    return jan, both


def test_a_renamed_scheme_is_one_scheme_so_overlapping_rows_are_recognised_at_import(db_path, tmp_path):
    jan, both = two_spellings()
    led = Ledger(db_path)
    led.import_statement(parse(nps_statement(jan, period=JAN), tmp_path), sha256="a" * 64)
    res = led.import_statement(parse(nps_statement(both, period=("Jan 01, 2026", "Feb 28, 2026")), tmp_path), sha256="b" * 64)
    assert (res.txn_new, res.txn_dup) == (1, 1)                         # January's row is the same row under the new name
    led.close()
    ov = analytics.nps_overview(connect_ro(db_path))
    assert len(ov["rows"]) == 1 and ov["total_contributed"] == Decimal("10500.00") and ov["complete"]


def test_rows_stored_under_older_names_are_re_keyed_and_their_duplicates_cleaned(db_path, tmp_path):
    """Data imported before schemes had a stable identity: the same January row stored twice, under two exact names,
    from two different statements."""
    jan, both = two_spellings()
    led = Ledger(db_path)
    led.import_statement(parse(nps_statement(both, period=("Jan 01, 2026", "Feb 28, 2026")), tmp_path), sha256="b" * 64)
    led.import_statement(parse(nps_statement(jan, period=JAN), tmp_path), sha256="a" * 64)        # its January row is a known duplicate
    doc = led.conn.execute("SELECT MAX(id) FROM source_documents").fetchone()[0]
    legacy = "NPS:" + OLD.replace(" ", "-")
    led.conn.execute("INSERT INTO transactions (account_id, doc_id, isin, scheme_name, date, kind, description, amount, units, nav, "
                     "balance, idem_key) SELECT account_id, ?, ?, ?, date, kind, description, amount, units, nav, balance, 'legacy' "
                     "FROM transactions WHERE date = '2026-01-05'", (doc, legacy, OLD))
    led.conn.execute("UPDATE holdings SET isin = ?, scheme_name = ? WHERE doc_id = ?", (legacy, OLD, doc))
    led.conn.commit()
    assert analytics.nps_overview(led.conn)["total_contributed"] == Decimal("15500.00")            # the double count
    assert any("stored twice" in w for w in led.flag_duplicate_transactions())                     # re-keys, then spots it
    assert led.conn.execute("SELECT COUNT(*) FROM review_queue WHERE kind='duplicate_txns' AND status='open'").fetchone()[0] == 1
    assert led.unify_nps_scheme_ids() == 0                                                         # already re-keyed: idempotent
    assert led.remove_duplicate_transactions() == 1
    ov = analytics.nps_overview(led.conn)
    assert len(ov["rows"]) == 1 and ov["total_contributed"] == Decimal("10500.00") and ov["complete"] and ov["xirr_pct"] is not None
    assert led.conn.execute("SELECT COUNT(DISTINCT isin) FROM holdings").fetchone()[0] == 1


def test_unify_rekeys_rows_when_called_on_its_own(db_path, tmp_path):
    jan, _ = two_spellings()
    led = Ledger(db_path)
    led.import_statement(parse(nps_statement(jan, period=JAN), tmp_path), sha256="a" * 64)
    for table in ("transactions", "holdings"):
        led.conn.execute(f"UPDATE {table} SET isin = 'NPS:OLD-NAME'")
    led.conn.commit()
    assert led.unify_nps_scheme_ids() == 1 and led.unify_nps_scheme_ids() == 0
    assert {r[0] for r in led.conn.execute("SELECT isin FROM transactions UNION SELECT isin FROM holdings")} == {"NPS:ICICI-SCHEME-E-TIER-I"}


def test_unifying_never_touches_mutual_funds(db_path):
    from finance.parsers.cas import CasParser
    from tests.conftest import make_cas
    led = Ledger(db_path)
    led.import_statement(CasParser().from_casdata(make_cas()), sha256="m" * 64)
    before = led.conn.execute("SELECT isin FROM holdings ORDER BY isin").fetchall()
    assert led.unify_nps_scheme_ids() == 0
    assert led.conn.execute("SELECT isin FROM holdings ORDER BY isin").fetchall() == before
