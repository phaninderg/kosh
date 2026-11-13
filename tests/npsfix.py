"""Synthetic NPS (CRA) transaction statements for parser tests, in the two layouts the parser reads: schemes one after
another ("seq") and schemes side by side with three-line rows ("cols"). Names, numbers and amounts are invented."""

from __future__ import annotations

from decimal import Decimal

from tests.cardfix import _Page

NAMES = ["ACME FUND E - TIER I", "ACME FUND C - TIER I", "ACME FUND G - TIER I"]
CONTRIBUTION = "By Contribution for {m}"
BILLING = "By Billing for Q1, 2026-2027"


def units_of(amount: str | Decimal, nav: str | Decimal) -> Decimal:
    return (Decimal(str(amount)) / Decimal(str(nav))).quantize(Decimal("0.0001"))


def nrow(day: str, amount: str, nav: str, *, desc: str | None = None, units: str | None = None, month: str = "January,2026") -> dict:
    """A scheme row. A contribution by default; a charge when `amount` is negative (units come out negative)."""
    a = Decimal(amount)
    return dict(day=day, desc=desc or (BILLING if a < 0 else CONTRIBUTION.format(m=month)), amount=a, nav=Decimal(nav),
                units=Decimal(units) if units is not None else units_of(a, nav))


def nscheme(name: str, opening: str, nav: str, rows: list[dict]) -> dict:
    opening = Decimal(opening)
    closing = opening + sum((r["units"] for r in rows), Decimal(0))
    return dict(name=name, opening=opening, nav=Decimal(nav), rows=rows, closing=closing,
                value=(closing * Decimal(nav)).quantize(Decimal("0.01")))


def _f(v: Decimal, places: int = 2) -> str:
    s = f"{abs(v):,.{places}f}"
    return f"({s})" if v < 0 else s


def nps_statement(schemes: list[dict], *, layout: str = "seq", period=("Jan 01, 2026", "Jan 31, 2026"), tier: str = "I",
                  pran: str = "123456789012", closing_error: str | None = None, value_error: str | None = None,
                  table_units_error: str | None = None, contributions: str | None = None, portal: bool = False) -> bytes:
    """`closing_error` prints a wrong closing balance for the first scheme, `value_error` a wrong value,
    `table_units_error` wrong units in the holdings table, `contributions` a wrong total in the contributions table.
    `portal` writes dates with four-digit years and the period with full month names and a lowercase "to" (the layout
    of statements downloaded from the NPS portal; `layout` "seq" only)."""
    p = _Page()
    p.words(20, (415, period[0].split()[0]), (445, period[0].split()[1]), (475, period[0].split()[2]), (505, "to" if portal else "To"),
            (525, period[1].split()[0]), (550, period[1].split()[1]), (575, period[1].split()[2]))
    p.words(30, (181, "NPS"), (207, "Transaction"), (272, "Statement"), (327, "for"), (345, "Tier"), (370, tier), (385, "Account"))
    p.words(40, (35, "PRAN"), (142, pran))
    if layout == "seq":
        _seq(p, schemes, closing_error, value_error, table_units_error, contributions, portal)
    else:
        _cols(p, schemes, closing_error, value_error, table_units_error, contributions)
    return p.pdf()


def _contrib_table(p: _Page, y: float, schemes: list[dict], contributions: str | None) -> None:
    total = sum((r["amount"] for s in schemes for r in s["rows"] if r["amount"] > 0), Decimal(0))
    p.words(y, (45, "Date"), (131, "Description"), (409, "Employee"), (463, "Employer's"), (529, "Total"))
    p.put(37, y + 21, "31-Jan-26"); p.put(78, y + 21, "For January, 2026")
    p.put(433, y + 21, "0.00"); p.put(474, y + 21, _f(total)); p.put(533, y + 21, contributions or _f(total))


def _seq(p: _Page, schemes, closing_error, value_error, table_units_error, contributions, portal: bool = False) -> None:
    year = (lambda d: d[:7] + "20" + d[7:]) if portal else (lambda d: d)             # 05-Jan-26 -> 05-Jan-2026
    p.words(99, (117, "Scheme"), (282, "Value"), (416, "Total"), (508, "NAV"))
    p.words(106, (258, "Holdings"), (329, "(in"), (338, "Rs)"), (349, "(C"), (358, "="), (364, "A"), (370, "*"), (374, "B)"))
    y = 116.0
    for i, s in enumerate(schemes):
        p.put(76, y, s["name"])
        p.put(302, y, _f(Decimal(value_error) if value_error and i == 0 else s["value"]))
        p.put(415, y, _f(Decimal(table_units_error) if table_units_error and i == 0 else s["closing"], 4)); p.put(510, y, _f(s["nav"], 4))
        y += 12
    _contrib_table(p, 259.0, schemes, contributions)
    p.words(326, (250, "Transaction"), (304, "Details"))
    y = 370.0
    for i, s in enumerate(schemes):
        p.put(232, y, s["name"])
        p.words(y + 20, (68, "Date"), (195, "Description"), (332, "Amount"), (425, "NAV"), (514, "Units"))
        y += 40
        p.put(58, y, year("01-Jan-26")); p.put(187, y, "Opening balance" if portal else "Opening Balance"); p.put(507, y, _f(s["opening"], 4)); y += 15
        for r in s["rows"]:
            p.put(61, y, year(r["day"])); p.put(166, y, r["desc"]); p.put(329, y, _f(r["amount"]))
            p.put(420, y, _f(r["nav"], 4)); p.put(508, y, _f(r["units"], 4)); y += 15
        shown = Decimal(closing_error) if closing_error and i == 0 else s["closing"]
        p.put(58, y, year("31-Jan-26")); p.put(187, y, "Closing Balance"); p.put(507, y, _f(shown, 4)); y += 30


def _cols(p: _Page, schemes, closing_error, value_error, table_units_error, contributions) -> None:
    n = len(schemes)
    p.words(771, (35, "Scheme"), (58, "wise"), (72, "Value"), (89, "of"), (96, "your"))
    p.put(200, 782, "C=A*B")
    cx = [229, 358, 491][:n]
    p.words(783, (35, "Holdings"), (75, "(Contribution)"), (150, "(in"), (170, "Rs)"))
    for x, s, i in zip(cx, schemes, range(n)):
        p.put(x, 779.5, _f(Decimal(value_error) if value_error and i == 0 else s["value"]))        # a hair above the formula line
        p.put(x, 801, _f(Decimal(table_units_error) if table_units_error and i == 0 else s["closing"], 4))
        p.put(x + 5, 821, _f(s["nav"], 4))
    p.words(801, (35, "Total"), (50, "Units")); p.words(821, (35, "NAV"), (49, "on"), (57, "31-Jan-26"))
    p.new_page()
    _contrib_table(p, 141.0, schemes, contributions)
    p.words(347, (250, "Transaction"), (304, "Details"))
    base = [259, 364, 469]
    for c, s in enumerate(schemes):                              # scheme names: two lines per column, capitals
        words = s["name"].split()
        half = (len(words) + 1) // 2
        p.words(379, *[(base[c] + 2 + k * 28, w) for k, w in enumerate(words[:half])])
        p.words(385, *[(base[c] + 7 + k * 20, w) for k, w in enumerate(words[half:])])
    p.words(393, (38, "Date"), (104, "Description"))
    p.words(410, *[(b, "Amount") for b in base])
    p.words(417, *[(b + 62, "Units") for b in base])
    p.words(420, *[(b + 4, "NAV") for b in base])
    y = 431.0
    p.put(31, y, "01-Jan-26"); p.put(90, y, "Opening balance")
    for c, s in enumerate(schemes):
        p.put(base[c] + 59, y, _f(s["opening"], 4))
    y += 13
    days = sorted({r["day"] for s in schemes for r in s["rows"]})
    for day in days:
        row_of = [next((r for r in s["rows"] if r["day"] == day), None) for s in schemes]
        first = next(r for r in row_of if r)
        p.put(30, y, day); p.put(90, y, first["desc"])
        for c, r in enumerate(row_of):
            if r:
                p.put(base[c] + 13, y, _f(r["amount"]))
        for c, r in enumerate(row_of):
            if r:
                p.put(base[c] + 66, y + 6, _f(r["units"], 4))
        for c, r in enumerate(row_of):
            if r:
                p.put(base[c] + 16, y + 13, _f(r["nav"], 4))
        y += 26
    p.put(30, y, "31-Jan-26"); p.put(90, y, "Closing Balance")
    for c, s in enumerate(schemes):
        shown = Decimal(closing_error) if closing_error and c == 0 else s["closing"]
        p.put(base[c] + 59, y, _f(shown, 4))
