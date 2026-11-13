"""The landing page: net worth and what it is made of, across bank, fixed deposits, funds, PPF, NPS and cards."""

from datetime import date
from decimal import Decimal

import pytest

from finance import analytics
from finance.db import connect_ro
from finance.ledger import Ledger
from finance.parsers.cas import CasParser
from finance.parsers.icici import IciciStatementParser
from finance.parsers.icici_card import IciciCardParser
from finance.parsers.nps import NpsParser
from tests.cardfix import crow, icici_card
from tests.conftest import make_cas
from tests.npsfix import nps_statement
from tests.test_icici import statement_pdf
from tests.test_nps import schemes
from tests.test_web import authed, client  # noqa: F401  (fixtures)

BANK, FD, MF, PPF, NPS, OWED = (Decimal(x) for x in ("524068.01", "50000", "24150", "105000", "8988.00", "3800.49"))
NET = BANK + FD + MF + PPF + NPS                                                  # 712,206.01: card bills are not subtracted


def load_everything(db_path, tmp_path):
    led = Ledger(db_path)
    files = {"bank.pdf": (IciciStatementParser(), statement_pdf()),
             "card.pdf": (IciciCardParser(), icici_card("2000.00", [crow("02/10/2026", "SHOP", "1200.50"), crow("20/10/2026", "BOOKS", "999.99"),
                                                                    crow("06/10/2026", "Payment received", "400", credit=True)])),
             "nps.pdf": (NpsParser(), nps_statement(schemes(opening=("0", "0", "0"))))}
    for i, (name, (parser, pdf)) in enumerate(files.items()):
        path = tmp_path / name
        path.write_bytes(pdf)
        led.import_statement(parser.parse(path, None), sha256=str(i) * 64)
    led.import_statement(CasParser().from_casdata(make_cas()), sha256="m" * 64)
    led.close()


BEFORE_DUE, DUE_DAY, AFTER_DUE = date(2026, 11, 13), date(2026, 11, 18), date(2026, 11, 19)


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    """The page asks for today's date; pin it so these tests do not age."""
    import finance.web.app as webapp
    monkeypatch.setattr(webapp, "today", lambda: BEFORE_DUE)


def test_net_worth_is_the_assets_with_card_dues_reported_beside_it_not_subtracted(db_path, tmp_path):
    load_everything(db_path, tmp_path)
    ov = analytics.overview(connect_ro(db_path), BEFORE_DUE)
    parts = {a["key"]: a for a in ov["assets"]}
    assert [a["key"] for a in ov["assets"]] == ["bank", "fd", "mf", "ppf", "nps"]            # a fixed order: colours follow the key
    assert (parts["bank"]["value"], parts["fd"]["value"], parts["mf"]["value"], parts["ppf"]["value"], parts["nps"]["value"]) == (BANK, FD, MF, PPF, NPS)
    assert ov["liability"]["value"] == OWED == ov["liabilities_total"]
    assert ov["assets_total"] == BANK + FD + MF + PPF + NPS and ov["net_worth"] == NET
    assert all(a["present"] and a["as_of"] for a in ov["assets"]) and ov["liability"]["as_of"] == "2026-10-31"
    assert sum(a["share_pct"] for a in ov["assets"]) in (Decimal("99.9"), Decimal("100.0"), Decimal("100.1"))
    assert parts["mf"]["gain"] is not None and parts["nps"]["xirr_pct"] is not None


def test_card_dues_say_what_to_pay_and_by_when(db_path, tmp_path):
    load_everything(db_path, tmp_path)
    liab = analytics.overview(connect_ro(db_path), BEFORE_DUE)["liability"]
    (due,) = liab["dues"]
    assert (due["payable"], due["outstanding"], due["due_date"]) == (OWED, OWED, "2026-11-18")
    assert liab["payable"] == OWED and liab["next_due"] == "2026-11-18"


def test_a_card_in_credit_is_not_a_liability(db_path, tmp_path):
    from finance.parsers.axis_card import AxisCardParser
    from tests.cardfix import axis_card
    led = Ledger(db_path)
    path = tmp_path / "c.pdf"
    path.write_bytes(axis_card("100.00", [crow("02/10/2026", "SHOP", "100"), crow("03/10/2026", "Payment received", "400", credit=True)]))
    led.import_statement(AxisCardParser().parse(path, None), sha256="c" * 64)
    ov = analytics.overview(connect_ro(db_path), BEFORE_DUE)
    assert ov["liabilities_total"] == 0 and ov["net_worth"] == 0 and ov["liability"]["present"]


def test_only_what_exists_is_present(db_path, tmp_path):
    led = Ledger(db_path)
    led.import_statement(CasParser().from_casdata(make_cas()), sha256="m" * 64)
    ov = analytics.overview(connect_ro(db_path), BEFORE_DUE)
    assert [a["key"] for a in ov["assets"] if a["present"]] == ["mf"] and not ov["liability"]["present"]
    assert ov["net_worth"] == MF and ov["any"]


def test_an_empty_ledger_says_so(db_path):
    ov = analytics.overview(connect_ro(db_path), BEFORE_DUE)
    assert ov["any"] is False and ov["net_worth"] == 0


def test_landing_page_shows_net_worth_charts_and_a_tile_per_tab(client, db_path, tmp_path):
    load_everything(db_path, tmp_path)
    authed(client)
    page = client.get("/").text
    assert "<h1>Overview</h1>" in page and "₹7,12,206" in page                                         # the hero figure, to the rupee
    assert "What your assets are made of" in page and "Where your net worth sits" in page
    for label in ("Bank balance", "Fixed deposits", "Mutual funds", "PPF", "NPS", "Credit card dues"):
        assert label in page
    assert page.count('class="seg c-') == 5 and "View as a table" in page                                      # chart bars and the table view
    assert 'href="/portfolio"' in page and 'href="/accounts"' in page
    assert "by 2026-11-18" in page and "To pay" not in page
    css = client.get("/static/style.css").text
    assert "--c-nps" in css and "prefers-color-scheme:dark" in css


def test_the_fund_portfolio_moved_to_its_own_tab(client, db_path, tmp_path):
    load_everything(db_path, tmp_path)
    authed(client)
    assert "Mutual fund portfolio" in client.get("/portfolio").text and "Mutual fund portfolio" not in client.get("/").text
    nav = client.get("/").text
    assert ">Overview<" in nav and ">Mutual funds<" in nav


def test_landing_page_with_nothing_imported_points_to_import(client):
    authed(client)
    page = client.get("/").text
    assert "Nothing imported yet" in page and 'href="/import"' in page and "Net worth" not in page


def test_open_review_items_are_flagged_on_the_landing_page(client, db_path, tmp_path):
    load_everything(db_path, tmp_path)
    led = Ledger(db_path)
    led.add_review("needs_attention", "x", "something")
    led.close()
    authed(client)
    assert "need review" in client.get("/").text


# ── dues depend on today's date: only bills still ahead are shown or counted ──────────────────────────────────

def test_a_bill_is_due_through_its_due_date_and_not_after(db_path, tmp_path):
    load_everything(db_path, tmp_path)
    conn = connect_ro(db_path)
    for day in (BEFORE_DUE, DUE_DAY):                                  # due on the day itself still counts
        ov = analytics.overview(conn, day)
        assert ov["liabilities_total"] == OWED and ov["net_worth"] == NET and len(ov["liability"]["dues"]) == 1
        assert ov["liability"]["stale"] == []
    after = analytics.overview(conn, AFTER_DUE)
    assert after["liabilities_total"] == 0 and after["net_worth"] == NET == after["assets_total"]
    assert after["liability"]["dues"] == [] and after["liability"]["payable"] == 0 and after["liability"]["next_due"] is None
    (old,) = after["liability"]["stale"]
    assert (old["statement_date"], old["due_date"]) == ("2026-10-31", "2026-11-18") and "1234" in old["card"]
    assert after["liability"]["present"]


def test_dues_are_worked_out_for_the_date_the_page_is_loaded(client, db_path, tmp_path, monkeypatch):
    import finance.web.app as webapp
    load_everything(db_path, tmp_path)
    authed(client)
    page = client.get("/").text
    assert "…1234 = <b>₹3,800</b> by 2026-11-18" in page and "Credit card bills coming up: <b>₹3,800</b>" in page   # which card, the amount, when
    monkeypatch.setattr(webapp, "today", lambda: AFTER_DUE)             # the next day: the same data, no longer due
    page = client.get("/").text
    assert "by 2026-11-18" not in page and "was due" not in page and "Credit card bills coming up" not in page
    assert "₹7,12,206" in page and "· latest statement" not in page     # net worth is the assets again; no extra text


def test_the_card_tile_is_only_the_amount_and_one_line_per_card(client, db_path, tmp_path):
    led = Ledger(db_path)
    for sha, (card, due, amount) in zip("ab", (("4315XXXXXXXX1111", "November 20, 2026", "1000"), ("4315XXXXXXXX2222", "November 25, 2026", "250"))):
        path = tmp_path / f"{sha}.pdf"
        path.write_bytes(icici_card("0.00", [crow("02/10/2026", "SHOP", amount)], card=card, due=due))
        led.import_statement(IciciCardParser().parse(path, None), sha256=sha * 64)
    led.close()
    authed(client)
    page = client.get("/").text
    assert "₹1,250" in page and "ICICI Bank …1111 = <b>₹1,000</b> by 2026-11-20" in page
    assert "ICICI Bank …2222 = <b>₹250</b> by 2026-11-25" in page
    for extra in ("To pay", "outstanding", "Nothing due", "· latest statement", "was due"):
        assert extra not in page


def test_even_one_card_is_named_with_its_amount_and_date(client, db_path, tmp_path):
    load_everything(db_path, tmp_path)
    authed(client)
    assert "ICICI Bank …1234 = <b>₹3,800</b> by 2026-11-18" in client.get("/").text


# ── dues depend on today's date: only bills still ahead are shown or counted ──────────────────────────────────

def test_a_bill_is_due_through_its_due_date_and_not_after(db_path, tmp_path):
    load_everything(db_path, tmp_path)
    conn = connect_ro(db_path)
    for day in (BEFORE_DUE, DUE_DAY):                                  # due on the day itself still counts
        ov = analytics.overview(conn, day)
        assert ov["liabilities_total"] == OWED and ov["net_worth"] == NET and len(ov["liability"]["dues"]) == 1
        assert ov["liability"]["stale"] == []
    after = analytics.overview(conn, AFTER_DUE)
    assert after["liabilities_total"] == 0 and after["net_worth"] == NET == after["assets_total"]
    assert after["liability"]["dues"] == [] and after["liability"]["payable"] == 0 and after["liability"]["next_due"] is None
    (old,) = after["liability"]["stale"]
    assert (old["statement_date"], old["due_date"]) == ("2026-10-31", "2026-11-18") and "1234" in old["card"]
    assert after["liability"]["present"]


def test_dues_are_worked_out_for_the_date_the_page_is_loaded(client, db_path, tmp_path, monkeypatch):
    import finance.web.app as webapp
    load_everything(db_path, tmp_path)
    authed(client)
    page = client.get("/").text
    assert "…1234 = <b>₹3,800</b> by 2026-11-18" in page and "Credit card bills coming up: <b>₹3,800</b>" in page   # which card, the amount, when
    monkeypatch.setattr(webapp, "today", lambda: AFTER_DUE)             # the next day: the same data, no longer due
    page = client.get("/").text
    assert "by 2026-11-18" not in page and "was due" not in page and "Credit card bills coming up" not in page
    assert "₹7,12,206" in page and "· latest statement" not in page     # net worth is the assets again; no extra text


def test_the_card_tile_is_only_the_amount_and_one_line_per_card(client, db_path, tmp_path):
    led = Ledger(db_path)
    for sha, (card, due, amount) in zip("ab", (("4315XXXXXXXX1111", "November 20, 2026", "1000"), ("4315XXXXXXXX2222", "November 25, 2026", "250"))):
        path = tmp_path / f"{sha}.pdf"
        path.write_bytes(icici_card("0.00", [crow("02/10/2026", "SHOP", amount)], card=card, due=due))
        led.import_statement(IciciCardParser().parse(path, None), sha256=sha * 64)
    led.close()
    authed(client)
    page = client.get("/").text
    assert "₹1,250" in page and "ICICI Bank …1111 = <b>₹1,000</b> by 2026-11-20" in page
    assert "ICICI Bank …2222 = <b>₹250</b> by 2026-11-25" in page
    for extra in ("To pay", "outstanding", "Nothing due", "· latest statement", "was due"):
        assert extra not in page




def test_only_the_cards_still_ahead_are_counted(db_path, tmp_path):
    led = Ledger(db_path)
    for sha, (card, due) in zip("ab", (("4315XXXXXXXX1111", "November 10, 2026"), ("4315XXXXXXXX2222", "November 25, 2026"))):
        path = tmp_path / f"{sha}.pdf"
        path.write_bytes(icici_card("0.00", [crow("02/10/2026", "SHOP", "1000")], card=card, due=due))
        led.import_statement(IciciCardParser().parse(path, None), sha256=sha * 64)
    led.close()
    ov = analytics.overview(connect_ro(db_path), BEFORE_DUE)
    assert ov["liabilities_total"] == Decimal("1000") and [d["due_date"] for d in ov["liability"]["dues"]] == ["2026-11-25"]
    assert [s["due_date"] for s in ov["liability"]["stale"]] == ["2026-11-10"] and ov["liability"]["next_due"] == "2026-11-25"


def test_a_card_whose_statement_has_no_due_date_still_counts(db_path, tmp_path):
    load_everything(db_path, tmp_path)
    led = Ledger(db_path)
    led.conn.execute("UPDATE statement_summaries SET due_date = NULL")
    led.conn.commit()
    ov = analytics.overview(led.conn, AFTER_DUE)
    assert ov["liabilities_total"] == OWED and ov["liability"]["dues"][0]["due_date"] is None and ov["liability"]["stale"] == []


def test_by_default_the_real_todays_date_decides(db_path, tmp_path):
    """No date passed: a bill due in 2001 is long gone, one due in 2099 is still ahead."""
    led = Ledger(db_path)
    for sha, (card, per, due) in zip("ab", (("4315XXXXXXXX1111", ("January 01, 2001", "January 31, 2001"), "February 18, 2001"),
                                            ("4315XXXXXXXX2222", ("January 01, 2099", "January 31, 2099"), "February 18, 2099"))):
        path = tmp_path / f"{sha}.pdf"
        path.write_bytes(icici_card("0.00", [crow("02/01/" + per[0][-4:], "SHOP", "700")], card=card, period=per, due=due))
        led.import_statement(IciciCardParser().parse(path, None), sha256=sha * 64)
    led.close()
    ov = analytics.overview(connect_ro(db_path))
    assert ov["liabilities_total"] == Decimal("700") and [d["due_date"] for d in ov["liability"]["dues"]] == ["2099-02-18"]
    assert [s["due_date"] for s in ov["liability"]["stale"]] == ["2001-02-18"]


def test_the_tile_shows_what_to_pay_not_the_whole_balance(client, db_path, tmp_path):
    load_everything(db_path, tmp_path)
    led = Ledger(db_path)
    led.conn.execute("UPDATE statement_summaries SET amount_due = '300'")        # e.g. instalments not yet billed
    led.conn.commit()
    led.close()
    authed(client)
    page = client.get("/").text
    assert '<div class="v">₹300</div>' in page and "ICICI Bank …1234 = <b>₹300</b> by 2026-11-18" in page
    assert "−₹" not in page                                                         # nothing is subtracted from net worth any more
