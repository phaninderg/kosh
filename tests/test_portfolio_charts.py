"""Mutual fund page: category as a share of value, AMC as bars of amounts."""

import json
import re
from datetime import date
from decimal import Decimal

from finance import analytics
from finance.ledger import Ledger
from finance.models import ParsedFolio, ParsedScheme, ParsedStatement
from tests.test_web import authed, client  # noqa: F401  (fixtures)

FUNDS = [  # (amc, folio, scheme, value) -> categories large_cap, debt, flexi_cap, small_cap, index, elss, mid_cap, hybrid
    ("Alpha AMC", "1001", "Alpha Large Cap Fund - Direct Growth", "40000"),
    ("Alpha AMC", "1001", "Alpha Liquid Fund - Direct Growth", "10000"),
    ("Beta AMC", "2002", "Beta Flexi Cap Fund - Direct Growth", "30000"),
    ("Gamma AMC", "3003", "Gamma Small Cap Fund - Direct Growth", "10000"),
    ("Gamma AMC", "3003", "Gamma Index Fund Nifty 50 - Direct", "5000"),
    ("Delta AMC", "4004", "Delta ELSS Tax Saver - Direct", "3000"),
    ("Delta AMC", "4004", "Delta Mid Cap Fund - Direct Growth", "1500"),
    ("Delta AMC", "4004", "Delta Balanced Advantage Fund", "500"),
]


def load_funds(db_path):
    folios: dict[tuple, list] = {}
    for i, (amc, folio, name, value) in enumerate(FUNDS):
        folios.setdefault((amc, folio), []).append(ParsedScheme(
            scheme_name=name, isin=f"INF00000{i:04d}", close_units=Decimal("100"), valuation_date=date(2026, 10, 31),
            nav=Decimal(value) / 100, cost=Decimal(value) * Decimal("0.8"), value=Decimal(value)))
    stmt = ParsedStatement(parser="cas", period_to=date(2026, 10, 31),
                           folios=[ParsedFolio(folio=f, amc=a, schemes=s) for (a, f), s in folios.items()])
    led = Ledger(db_path)
    led.import_statement(stmt, sha256="f" * 64)
    led.close()


def test_shares_are_percentages_largest_first_and_add_to_a_hundred():
    rows = analytics.share_breakdown({"a": Decimal(50), "b": Decimal(30), "c": Decimal(20), "z": Decimal(0)})
    assert [(n, p) for n, _, p in rows] == [("a", Decimal("50.0")), ("b", Decimal("30.0")), ("c", Decimal("20.0"))]
    assert analytics.share_breakdown({}) == []


def test_a_long_tail_is_folded_into_other():
    values = {f"c{i}": Decimal(100 - i) for i in range(12)}
    rows = analytics.share_breakdown(values, keep=7)
    assert [n for n, _, _ in rows][:7] == [f"c{i}" for i in range(7)] and rows[-1][0] == "Other" and len(rows) == 8
    assert rows[-1][1] == sum(Decimal(100 - i) for i in range(7, 12)) and abs(sum(p for _, _, p in rows) - 100) < Decimal("0.5")
    assert len(analytics.share_breakdown(values, keep=1000)) == 12                    # AMCs are never folded


def test_category_chart_carries_percentages_and_the_amc_chart_is_bars_of_amounts(client, db_path):
    load_funds(db_path)
    authed(client)
    page = client.get("/portfolio").text
    assert 'id="cat"' in page and 'id="amc"' not in page                                 # a doughnut for shares; no AMC doughnut
    assert "share of value" in page
    data = json.loads(re.search(r"const rows=(\[\[.*?\]\]),", page, re.S).group(1))
    assert data[0] == ["large_cap", 40.0] and [r[1] for r in data] == sorted((r[1] for r in data), reverse=True)
    assert abs(sum(r[1] for r in data) - 100) < 0.5                                       # percentages, not rupees
    assert "· '+r[1]+'%" in page                                                         # legend text = name · share %
    bars = page.split('class="amc-bars"')[1].split("</div></div>\n</div>")[0]
    assert bars.index("Alpha AMC") < bars.index("Beta AMC") < bars.index("Gamma AMC") < bars.index("Delta AMC")   # largest first
    for amount in ("₹50,000", "₹30,000", "₹15,000", "₹5,000"):
        assert amount in bars
    assert 'style="width:100.0%"' in bars                                               # the largest fills the track


def test_the_category_table_view_lists_shares_not_amounts(client, db_path):
    load_funds(db_path)
    authed(client)
    page = client.get("/portfolio").text
    table = page.split("View as a table")[1].split("</table>")[0]
    assert "40.0%" in table and "₹" not in table


def test_colours_for_the_charts_are_defined_for_light_and_dark(client):
    authed(client)
    css = client.get("/static/style.css").text
    assert "--s1:#2a78d6" in css and "--s1:#3987e5" in css and "--other:" in css
    assert "--s8:#e34948" in css and "--s8:#e66767" in css                       # a chart can show eight categories before folding


def test_eight_categories_are_all_shown_and_nine_fold_into_other():
    eight = {f"c{i}": Decimal(100 - i) for i in range(8)}
    assert [n for n, _, _ in analytics.share_breakdown(eight)] == list(eight)
    nine = {f"c{i}": Decimal(100 - i) for i in range(9)}
    assert [n for n, _, _ in analytics.share_breakdown(nine)][-1] == "Other" and len(analytics.share_breakdown(nine)) == 8
