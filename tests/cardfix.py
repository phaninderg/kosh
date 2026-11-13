"""Synthetic credit card statement PDFs for parser tests, laid out with the same column geometry as the ICICI, HDFC
and Axis statements the parsers were written against (summary block, header-driven table columns, direction
markers). Every name, number and amount here is invented.

A row is `crow(date, description, amount, credit=False)`; `amount` is the positive figure as printed."""

from __future__ import annotations

from decimal import Decimal

from tests.pdffix import _width, build_pdf, inr


def crow(date: str, description: str, amount: str | int, credit: bool = False, *, tail: str = "", tag: str = "",
         after: tuple[str, str] | None = None) -> dict:
    """`tag` is a code the statement prints (Tata: M monthly instalment, T, EN...; "-" = no code); `after` is the
    (description, amount) of a dateless debit line printed under the row (Tata tax lines)."""
    return dict(date=date, desc=description, amount=Decimal(str(amount)), credit=credit, tail=tail, tag=tag, after=after)


class _Page:
    def __init__(self) -> None:
        self.pages: list[list[tuple[float, float, str]]] = [[]]

    def put(self, x: float, y: float, text: str) -> None:
        self.pages[-1].append((x, y, text))

    def right(self, edge: float, y: float, text: str) -> None:
        self.put(edge - _width(text), y, text)

    def words(self, y: float, *items: tuple[float, str]) -> None:
        """Separate words on one line; a word is pushed right if it would touch the previous one (pdfplumber would
        merge them)."""
        end = -100.0
        for x, t in items:
            x = max(x, end + 3.5)
            self.put(x, y, t)
            end = x + sum(0.72 if c.isupper() else 0.56 if c.isalnum() else 0.3 for c in t) * 8

    def new_page(self) -> None:
        self.pages.append([])

    def pdf(self) -> bytes:
        return build_pdf(self.pages)


def totals(prev: str, rows: list[dict]) -> tuple[Decimal, Decimal, Decimal]:
    purchases = sum((r["amount"] for r in rows if not r["credit"]), Decimal(0))
    payments = sum((r["amount"] for r in rows if r["credit"]), Decimal(0))
    return purchases, payments, Decimal(prev) + purchases - payments


# ── ICICI ─────────────────────────────────────────────────────────────────────

def icici_card(prev: str, rows: list[dict], *, card="4315XXXXXXXX1234", total: str | None = None, per_first_page=99,
               skip: tuple[int, ...] = (), flip: tuple[int, ...] = (), period=("October 01, 2026", "October 31, 2026"),
               due="November 18, 2026", extra_date: str | None = None) -> bytes:
    """Summary: total = previous + purchases - payments. `skip` leaves rows out of the table, `flip` prints the wrong
    CR marker, `total` prints a wrong total: each makes a statement that must be refused."""
    purchases, payments, tot = totals(prev, rows)
    p = _Page()
    p.put(71, 195, period[1]); p.put(65, 226, due)
    if extra_date:
        p.put(65, 240, extra_date)
    p.words(269, (81, "Total"), (111, "Amount"), (150, "due"), (221, "Previous"), (262, "Balance"), (306, "Purchases"),
            (350, "/"), (358, "Charges"), (404, "Cash"), (430, "Advances"), (488, "Payments"), (530, "/"), (538, "Credits"))
    p.put(83, 279, inr(total or tot))
    p.put(225, 284, inr(prev)); p.put(315, 284, inr(purchases)); p.put(418, 284, inr(0))
    p.put(492, 284, inr(payments))
    p.words(309, (73, "Minimum"), (110, "Amount"), (142, "due"))
    p.put(92, 318, inr("500"))
    p.words(327, (208, "Credit"), (229, "Limit"), (291, "Available"), (322, "Credit"))
    p.put(219, 346, inr("100000"))
    p.put(40, 784, f"Statement period : {period[0]} to {period[1]}")

    def header(y: float, c: dict) -> None:
        p.words(y, (c["date"], "Date"), (c["ser"], "SerNo."), (c["desc"], "Transaction"), (c["desc"] + 34, "Details"),
                (c["reward"], "Reward"), (c["amount"], "Amount"), (c["amount"] + 23, "(in)"))

    first = dict(date=208, ser=252, desc=305, reward=443, amount=522)
    later = dict(date=35, ser=113, desc=181, reward=385, amount=525)
    cols, y = first, 370.0
    header(y, cols)
    p.put(cols["date"], y + 21, card)
    y += 36
    p.put(60, y, "99%")                                           # the page-1 rewards panel sits left of the table
    for i, r in enumerate(rows):
        if i == per_first_page:
            p.new_page(); cols, y = later, 60.0
            header(y, cols); y += 13
        if i in skip:
            continue
        p.put(cols["date"], y, r["date"]); p.put(cols["ser"], y, f"2{i:010d}")
        p.put(cols["desc"], y, r["desc"]); p.put(cols["reward"] + 6, y, "9")
        credit = r["credit"] != (i in flip)
        if credit:
            p.right(cols["amount"] + 26, y, inr(r["amount"])); p.put(cols["amount"] + 30, y, "CR")
        else:
            p.right(cols["amount"] + 50, y, inr(r["amount"]))
        y += 13
        if r["tail"]:
            p.put(cols["desc"], y - 6, r["tail"]); y += 7
    p.put(32, y + 13, "#"); p.put(42, y + 13, "International"); p.put(100, y + 13, "Spends")
    return p.pdf()


# ── HDFC ──────────────────────────────────────────────────────────────────────

def hdfc_card(prev: str, rows: list[dict], *, card="123456XXXXXX7890", total: str | None = None, per_first_page=99,
              skip: tuple[int, ...] = (), flip: tuple[int, ...] = (), finance="0.00") -> bytes:
    """Summary: total = previous - payments + purchases + finance charges; the rupee sign is the letter C."""
    purchases, payments, tot = totals(prev, rows)
    tot += Decimal(finance)
    p = _Page()
    p.words(91, (194, "Credit"), (212, "Card"), (227, "No."), (318, card))
    p.words(120, (194, "Statement"), (225, "Date"), (341, "31"), (349, "Oct,"), (363, "2026"))
    p.words(135, (194, "Billing"), (212, "Period"), (299, "01"), (307, "Oct,"), (321, "2026"), (337, "-"), (341, "31"),
            (349, "Oct,"), (363, "2026"))
    p.words(220, (40, "PREVIOUS"), (71, "STATEMENT"), (108, "DUES"), (354, "FINANCE"), (381, "CHARGES"), (446, "TOTAL"),
            (466, "AMOUNT"), (496, "DUE"))
    p.put(446, 236, "C" + inr(str(total or tot).replace(",", "")))
    for x, v in ((65, prev), (169, payments), (270, purchases), (372, finance)):
        p.put(x, 242, "C" + inr(v))
    p.words(267, (65, "TOTAL"), (85, "CREDIT"), (107, "LIMIT"))
    p.put(78, 294, "C1,00,000")
    p.words(273, (446, "MINIMUM"), (479, "DUE"), (512, "DUE"), (526, "DATE"))
    p.put(446, 290, "C" + inr("500")); p.words(290, (512, "18"), (522, "Nov,"), (539, "2026"))

    def header(y: float, c: dict) -> None:
        p.words(y, (c["date"], "DATE"), (c["date"] + 17, "&"), (c["date"] + 23, "TIME"), (c["desc"], "TRANSACTION"),
                (c["desc"] + 44, "DESCRIPTION"), (c["amount"], "AMOUNT"))

    first = dict(date=170, time=232, desc=276, amount=528)
    later = dict(date=26, time=88, desc=132, amount=528)
    cols, y = first, 690.0
    p.words(675, (170, "Domestic"), (200, "Transactions"))
    header(y, cols); y += 15
    for i, r in enumerate(rows):
        if i == per_first_page:
            p.new_page(); cols, y = later, 264.0
            header(y, cols); y += 15
        if i in skip:
            continue
        p.put(cols["date"], y, r["date"] + "|"); p.put(cols["time"], y, "09:30")
        if r["tag"]:
            p.put(cols["desc"] - 19, y, r["tag"])
        p.put(cols["desc"], y, r["desc"]); p.put(cols["amount"] - 140, y, f"(Ref# 1234567890{i:04d})")
        if r["tail"]:
            p.put(cols["desc"], y + 9, r["tail"])
        if r["credit"] != (i in flip):
            p.put(cols["amount"] - 16, y, "+"); p.put(cols["amount"] - 4, y, "C"); p.put(cols["amount"] + 8, y, inr(r["amount"]))
        else:
            p.put(cols["amount"], y, "C"); p.put(cols["amount"] + 12, y, inr(r["amount"]))
        y += 14 + (9 if r["tail"] else 0)
    return p.pdf()


# ── Axis ──────────────────────────────────────────────────────────────────────

def axis_card(prev: str, rows: list[dict], *, card="123456******7890", total: str | None = None,
              skip: tuple[int, ...] = (), flip: tuple[int, ...] = (), other="0.00") -> bytes:
    """Summary: previous - payments - credits + purchases + cash + other = total; every amount carries Dr or Cr."""
    purchases, payments, tot = totals(prev, rows)
    tot += Decimal(other)
    p = _Page()
    p.words(218, (52, "Total"), (70, "Payment"), (101, "Due"), (152, "Minimum"), (185, "Payment"), (216, "Due"),
            (272, "Statement"), (308, "Period"), (381, "Payment"), (412, "Due"), (427, "Date"), (472, "Statement"),
            (509, "Generation"), (548, "Date"))
    shown = Decimal(total) if total else tot
    mark = "Cr" if shown < 0 else "Dr"
    p.words(232, (62, inr(shown)), (96, mark), (173, inr("500")), (201, "Dr"), (260, "01/10/2026"), (299, "-"),
            (303, "31/10/2026"), (393, "20/11/2026"), (510, "31/10/2026"))
    p.words(247, (50, "Credit"), (72, "Card"), (89, "Number"), (172, "Credit"), (194, "Limit"))
    p.words(260, (51, card), (173, "1,00,000.00"))
    p.words(276, (68, "Previous"), (99, "Balance"), (126, "-"), (130, "Payments"), (164, "-"), (168, "Credits"), (193, "+"),
            (199, "Purchase"), (230, "+"), (236, "Cash"), (253, "Advance"), (283, "+"), (289, "Other"),
            (310, "Debits&Charges"), (361, "=Total"), (383, "Payment"), (414, "Due"))
    p.words(289, (48, inr(prev)), (80, "Dr"), (116, inr(payments)), (172, "0.00"), (211, inr(purchases)), (273, "0.00"),
            (342, inr(other)), (406, inr(shown)), (440, mark))
    p.words(312, (50, "DATE"), (184, "TRANSACTION"), (233, "DETAILS"), (386, "MERCHANT"), (427, "CATEGORY"), (514, "AMOUNT"),
            (548, "(Rs.)"))
    p.words(322, (83, "Card"), (100, "No:"), (122, card), (260, "Name"))
    y = 333.0
    for i, r in enumerate(rows):
        if i in skip:
            continue
        p.put(35, y, r["date"]); p.put(90, y, r["desc"]); p.put(335, y, "IN"); p.put(360, y, "SHOP")
        p.right(560, y, inr(r["amount"]))
        p.put(574, y, "Cr" if r["credit"] != (i in flip) else "Dr")
        y += 10
    p.words(y + 13, (254, "****"), (273, "End"), (288, "of"), (297, "Statement"), (337, "****"))
    return p.pdf()


# ── Tata ──────────────────────────────────────────────────────────────────────

_MON = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def tata_card(prev: str, rows: list[dict], *, card=("XXXX", "XXXX", "XXXX", "XX99"), total: str | None = None,
              skip: tuple[int, ...] = (), flip: tuple[int, ...] = (), finance="0.00", payable: str | None = None) -> bytes:
    """Summary: previous - payments + purchases + finance = total outstanding; rows end in a code (D, C, T, M ...).
    Rows tagged M or "-" are memo lines: printed, but not part of the totals. `payable` is the headline amount due."""
    counted = [r for r in rows if r["tag"] not in ("M", "-")]
    counted += [dict(amount=Decimal(r["after"][1]), credit=False) for r in counted if r.get("after")]
    purchases, payments, tot = totals(prev, counted)
    tot += Decimal(finance)
    shown = total or inr(tot)
    p = _Page()
    p.words(55, (324, "Credit"), (348, "Card"), (367, "Number"))
    p.words(77, (318, card[0]), (342, card[1]), (366, card[2]), (390, card[3]))
    p.words(98, (323, "*Total"), (350, "Amount"), (385, "Due"))
    p.put(335, 116, payable or shown)
    p.words(140, (314, "**Minimum"), (365, "Amount"), (400, "Due"))
    p.put(343, 157, inr("500"))
    p.words(185, (43, "Credit"), (67, "Limit("), (332, "Statement"), (375, "Date"))
    p.put(63, 201, "1,00,000.00"); p.words(201, (334, "31"), (350, "Oct"), (370, "2026"))
    p.words(227, (329, "Payment"), (370, "Due"), (395, "Date"))
    p.words(242, (331, "18"), (350, "Nov"), (370, "2026"))
    p.words(306, (30, "Previous"), (70, "Balance"), (357, "Total"), (385, "Outstanding"))
    for x, v in ((39, prev), (119, payments), (203, purchases), (289, finance), (369, shown)):
        p.put(x, 336, v if isinstance(v, str) and v is shown else inr(v))
    p.words(435, (35, "Date"), (371, "Amount"))
    p.put(157, 443, "for Statement Period: 01 Oct 26 to 31 Oct 26")
    y = 462.0
    for i, r in enumerate(rows):
        if i in skip:
            continue
        d, m, yr = r["date"].split("/")
        p.words(y, (26, d), (37, _MON[int(m)]), (52, yr[2:]))
        p.put(71, y, r["desc"]); p.right(408, y, inr(r["amount"]))
        if r["tag"] != "-":                                       # tag = the statement's own code (M, T, EN...); "-" = none
            p.put(415, y, r["tag"] or ("C" if r["credit"] != (i in flip) else "D"))
        y += 12
        if r["after"]:
            p.put(71, y, r["after"][0]); p.right(408, y, inr(r["after"][1])); p.put(415, y, "D")
            y += 12
    return p.pdf()
