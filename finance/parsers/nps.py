"""NPS (National Pension System) transaction statement from the CRA: the monthly one and the annual one.

Deterministic, no LLM. A statement holds one account (Tier I or Tier II) with a few schemes. Each scheme has an
opening and a closing unit balance, contribution and charge rows, and a value = units x NAV. One layout lists the
schemes one after another (a row reads `date description amount NAV units`); the other puts the schemes side by
side in columns, each row spread over three lines (amount, units, NAV). Both the monthly and the annual statement
come in one of these two forms. Both are read into the same shape and
must pass the same checks, otherwise the statement is refused:

  * opening units + the units of every row = closing units, for each scheme;
  * the closing units equal the units in the statement's own holdings table;
  * units x NAV equals the value in that table (within a rupee).

The contributions table (a cross-check on the amounts) only produces a warning when it disagrees. NPS has no ISIN, so
a scheme is identified by its name. The PRAN (the account number) is hashed by the ledger and shown by its last 4
digits only.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from finance.models import ParsedFolio, ParsedScheme, ParsedStatement, ParsedTxn
from finance.parsers.base import register
from finance.parsers.pdftext import Word, group_lines, line_text, read_pages
from finance.redact import redact
from finance.schemes import scheme_id

_NUM = re.compile(r"^\(?-?\d[\d,]*(?:\.\d+)?\)?$")
_DAY = re.compile(r"^\d{2}-[A-Za-z]{3}-(?:\d{2}|\d{4})$")           # 05-Jan-26 (email) or 05-Jan-2026 (portal)
_PERIOD = re.compile(r"([A-Za-z]{3,9}) (\d{1,2}), (\d{4}) [Tt]o ([A-Za-z]{3,9}) (\d{1,2}), (\d{4})")   # "Jan 01, 2026 To Jan 31, 2026" or "April 01, 2026 to November 13, 2026"
_PRAN = re.compile(r"PRAN\s+(\d{12})")
_TIER = re.compile(r"Tier\s+(I{1,2})\s+Account", re.I)
_FORMULA = re.compile(r"\b[A-Z]\s*=\s*[A-Z]\s*\*\s*[A-Z]\b")          # "C = A * B": value = units x NAV
VALUE_TOLERANCE = Decimal("1.00")           # units x NAV against the printed value, in rupees
COL_PAD = 10
NOT_NAMES = {"NAV", "RS", "(RS)", "UNITS", "AMOUNT", "TOTAL"}      # capitals that label a column, not a scheme


def _num(text: str) -> Decimal:
    """'1,234.50' -> 1234.50; '(9.9999)' -> -9.9999 (the statement writes a deduction in brackets)."""
    neg = text.startswith("(") or text.startswith("-")
    v = Decimal(text.strip("()-").replace(",", ""))
    return -v if neg else v


def _is_num(w: Word) -> bool:
    return bool(_NUM.match(w.text)) and any(c.isdigit() for c in w.text) and "." in w.text


def _day(text: str) -> date:
    return datetime.strptime(text, "%d-%b-%Y" if len(text) == 11 else "%d-%b-%y").date()


def _month_day(mon: str, d: str, y: str) -> date:
    return datetime.strptime(f"{mon[:3]} {d} {y}", "%b %d %Y").date()


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().upper()


def _kind(description: str, units: Decimal) -> str:
    d = description.lower()
    if "shifting" in d or "switch" in d or "migrat" in d or "transfer" in d:
        return "SHIFT_NPS"               # units carried from one scheme to another inside the account: not new money
    if "contribut" in d:
        return "PURCHASE_NPS"
    if "billing" in d or "charge" in d:
        return "CHARGE_NPS"
    if "redempt" in d or "withdraw" in d:
        return "REDEMPTION"
    return "OTHER_NPS"


class _Scheme:
    def __init__(self, name: str):
        self.name = name
        self.opening: Decimal | None = None
        self.closing: Decimal | None = None
        self.txns: list[dict] = []
        self.value = self.units = self.nav = None


class _Parser:
    def __init__(self) -> None:
        self.pran = ""
        self.tier = "I"
        self.period: tuple[date, date] | None = None
        self.schemes: dict[str, _Scheme] = {}
        self.order: list[str] = []
        self.contrib_rows: list[Decimal] = []
        self.warnings: list[str] = []

    def scheme(self, name: str) -> _Scheme:
        k = _key(name)
        if k not in self.schemes:
            self.schemes[k] = _Scheme(re.sub(r"\s+", " ", name).strip())
            self.order.append(k)
        return self.schemes[k]

    # ── header ────────────────────────────────────────────────────────────────

    def header(self, lines: list[list[Word]]) -> None:
        for ln in lines:
            t = line_text(ln)
            if self.period is None and (m := _PERIOD.search(t)):
                a, b = m.groups()[:3], m.groups()[3:]
                self.period = (_month_day(*a), _month_day(*b))
            if not self.pran and (m := _PRAN.search(t)):
                self.pran = m.group(1)
            if (m := _TIER.search(t)):
                self.tier = m.group(1).upper()
        if not self.pran or self.period is None:
            raise ValueError("PRAN or statement period not found; layout not recognised")

    # ── contributions table (cross-check only) ────────────────────────────────

    def contributions(self, lines: list[list[Word]]) -> None:
        """Rows of the contributions table: a description and the total in the last column. The date leads the row,
        except where the portal wraps it onto the lines above and below, so a row is a dated line or one that starts
        with 'For' or 'By'."""
        for ln in lines:
            if not ln:
                continue
            nums = [w for w in ln if _is_num(w)]
            if len(nums) >= 2 and nums[-1].x0 > 470 and (_DAY.match(ln[0].text) or ln[0].text in ("For", "By")):
                self.contrib_rows.append(_num(nums[-1].text))

    # ── monthly layout: schemes one after another ─────────────────────────────

    def monthly(self, lines: list[list[Word]]) -> None:
        i = next((n for n, ln in enumerate(lines) if _FORMULA.search(line_text(ln))), None)
        if i is None:
            raise ValueError("NPS holdings table not found; layout not recognised")
        for ln in lines[i + 1:]:
            nums = [w for w in ln if _is_num(w)]
            if len(nums) == 3 and ln[-1] is nums[-1] and ln[-3] is nums[0]:
                s = self.scheme(" ".join(w.text for w in ln[:-3]))
                s.value, s.units, s.nav = (_num(w.text) for w in nums)
            elif self.schemes:
                break
        if not self.schemes:
            raise ValueError("no schemes found in the NPS holdings table; layout not recognised")
        current: _Scheme | None = None
        for ln in lines:
            t = line_text(ln)
            if _key(t) in self.schemes:
                current = self.schemes[_key(t)]
                continue
            if current is None or not ln or not _DAY.match(ln[0].text) or ln[0].x0 > 100:
                continue
            nums = [w for w in ln if _is_num(w)]
            low = t.lower()
            if "opening" in low and "balance" in low and nums:
                current.opening = _num(nums[-1].text)
            elif "closing" in low and "balance" in low and nums:
                current.closing = _num(nums[-1].text)
                current = None
            elif len(nums) >= 3:
                amount, nav, units = (_num(w.text) for w in nums[-3:])
                desc = " ".join(w.text for w in ln[1:ln.index(nums[-3])])
                current.txns.append({"day": _day(ln[0].text), "desc": desc, "amount": amount, "nav": nav, "units": units})

    # ── annual layout: schemes side by side, three lines per row ──────────────

    def annual(self, pages_lines: list[list[list[Word]]]) -> None:
        first = pages_lines[0]
        i = next((n for n, ln in enumerate(first) if _FORMULA.search(line_text(ln))), None)
        if i is None:
            raise ValueError("NPS holdings table not found; layout not recognised")
        # the value row's numbers can sit a fraction of a point above the formula line, so look on both sides of it
        rows = [[w for w in ln if _is_num(w)] for ln in first[max(0, i - 2):i + 8]]
        rows = [r for r in rows if len(r) >= 2][:3]
        if len(rows) != 3 or len({len(r) for r in rows}) != 1:
            raise ValueError("NPS holdings table does not have value, units and NAV rows; layout not recognised")
        n = len(rows[0])
        values = [[_num(w.text) for w in sorted(r, key=lambda w: w.x0)] for r in rows]
        cols: list[float] | None = None
        names: list[str] | None = None
        pending: dict | None = None            # a dated row still waiting for its units and NAV lines
        scheme_objs: list[_Scheme] = []
        for lines in pages_lines:
            for k, ln in enumerate(lines):
                t = line_text(ln)
                amounts = [w for w in ln if w.text == "Amount"]
                if len(amounts) == n and n > 1:                                 # a table header sets the columns
                    cols = [w.x0 - COL_PAD for w in amounts]
                    if names is None:
                        names = self._annual_names(lines, k, cols)
                        scheme_objs = [self.scheme(nm) for nm in names]
                        for s, v, u, nv in zip(scheme_objs, values[0], values[1], values[2]):
                            s.value, s.units, s.nav = v, u, nv
                    continue
                if cols is None or not scheme_objs:
                    continue
                low = t.lower()
                dated = ln and _DAY.match(ln[0].text) and ln[0].x0 < 60
                if dated and "balance" in low and ("opening" in low or "closing" in low):
                    nums = sorted((w for w in ln if _is_num(w)), key=lambda w: w.x0)
                    if len(nums) != n:
                        raise ValueError("an NPS balance line does not have one figure per scheme; layout not recognised")
                    for s, w in zip(scheme_objs, nums):
                        if "opening" in low:
                            s.opening = _num(w.text)
                        else:
                            s.closing = _num(w.text)
                    pending = None
                    continue
                if dated:
                    pending = {"day": _day(ln[0].text), "top": ln[0].top, "lines": [ln],
                               "desc": " ".join(w.text for w in ln[1:] if w.x0 < cols[0] and not _is_num(w))}
                    continue
                if pending is not None and ln[0].top - pending["top"] < 40:
                    pending["lines"].append(ln)
                    pending["top"] = ln[0].top
                    if len(pending["lines"]) == 3:
                        self._annual_row(pending, scheme_objs, cols)
                        pending = None

    def _annual_names(self, lines, k: int, cols: list[float]) -> list[str]:
        """Scheme names are printed in capitals, two lines per column, a little above the table header (a column
        label such as NAV can sit between)."""
        bounds = cols + [10_000.0]
        parts: list[list[str]] = [[] for _ in cols]
        top = lines[k][0].top
        for ln in lines[:k]:
            if top - ln[0].top > 45:
                continue
            for w in ln:
                if w.x0 >= cols[0] and w.text.upper() == w.text and any(ch.isalpha() for ch in w.text) \
                        and w.text not in NOT_NAMES or (w.x0 >= cols[0] and w.text in ("-", "I", "II")):
                    for c in range(len(cols)):
                        if bounds[c] <= w.x0 < bounds[c + 1]:
                            parts[c].append(w.text)
        names = [" ".join(p).strip() for p in parts]
        if any(not nm for nm in names):
            raise ValueError("NPS scheme names not found over the table; layout not recognised")
        return names

    def _annual_row(self, row: dict, schemes: list[_Scheme], cols: list[float]) -> None:
        bounds = cols + [10_000.0]
        figures: list[list[Word]] = []
        for ln in row["lines"]:
            by_col: list[Word | None] = [None] * len(schemes)
            for w in ln:
                if _is_num(w) and w.x0 >= cols[0]:
                    for c in range(len(schemes)):
                        if bounds[c] <= w.x0 < bounds[c + 1]:
                            by_col[c] = w
            figures.append(by_col)
        extra = " ".join(w.text for ln in row["lines"][1:] for w in ln if w.x0 < cols[0] and not _is_num(w))
        desc = (row["desc"] + " " + extra).strip()
        amounts, units, navs = figures
        for c, s in enumerate(schemes):
            if amounts[c] is None and units[c] is None and navs[c] is None:
                continue
            if None in (amounts[c], units[c], navs[c]):
                raise ValueError("an NPS row is missing an amount, units or NAV for a scheme; layout not recognised")
            s.txns.append({"day": row["day"], "desc": desc, "amount": _num(amounts[c].text),
                           "units": _num(units[c].text), "nav": _num(navs[c].text)})

    # ── result ────────────────────────────────────────────────────────────────

    def result(self) -> ParsedStatement:
        if self.period is None:
            raise ValueError("statement period not found; layout not recognised")
        as_of = self.period[1]
        schemes: list[ParsedScheme] = []
        for k in self.order:
            s = self.schemes[k]
            if s.opening is None or s.closing is None:
                raise ValueError(f"{s.name}: opening or closing balance not found; layout not recognised")
            running, txns = s.opening, []
            for t in s.txns:
                running += t["units"]
                txns.append(ParsedTxn(date=t["day"], kind=_kind(t["desc"], t["units"]), description=redact(t["desc"], limit=200),
                                      amount=t["amount"], units=t["units"], nav=t["nav"], balance=running))
            if running != s.closing:
                raise ValueError(f"{s.name}: opening {s.opening} + the units of every row = {running}, "
                                 f"statement says {s.closing}")
            if s.units != s.closing:
                raise ValueError(f"{s.name}: closing units {s.closing} differ from the holdings table ({s.units})")
            if abs(s.units * s.nav - s.value) > VALUE_TOLERANCE:
                raise ValueError(f"{s.name}: units x NAV = {(s.units * s.nav).quantize(Decimal('0.01'))}, "
                                 f"the statement's value is {s.value}")
            schemes.append(ParsedScheme(
                scheme_name=s.name, isin=scheme_id(s.name),
                close_units=s.closing, close_calculated=running, valuation_date=as_of, nav=s.nav, cost=None,
                value=s.value, txns=txns))
        listed = sum(self.contrib_rows, Decimal(0))
        done = sum((t.amount for sc in schemes for t in sc.txns if t.kind == "PURCHASE_NPS"), Decimal(0))
        if self.contrib_rows and listed != done:
            self.warnings.append(f"The statement's contributions table totals {listed} but the scheme rows add up to {done}.")
        return ParsedStatement(kind="detailed", parser=NpsParser.name, account_type="nps", period_from=self.period[0],
                               period_to=as_of, warnings=self.warnings,
                               folios=[ParsedFolio(folio=f"T{self.tier}-{self.pran}", amc=f"NPS Tier {self.tier}", schemes=schemes)])


def parse_pages(pages: list[list[Word]]) -> ParsedStatement:
    all_lines = [group_lines(w) for w in pages]
    p = _Parser()
    p.header(all_lines[0])
    flat = [ln for lines in all_lines for ln in lines]
    heading = next((n for n, ln in enumerate(flat) if line_text(ln).strip().lower() == "transaction details"), len(flat))
    p.contributions(flat[:heading])                    # the contributions table ends where the scheme rows begin
    # Monthly: each scheme's rows follow its own name line. Annual: one set of columns over all the schemes.
    opening_lines = [ln for ln in flat if ln and _DAY.match(ln[0].text) and "opening" in line_text(ln).lower()]
    side_by_side = bool(opening_lines) and len([w for w in opening_lines[0] if _is_num(w)]) > 1
    if side_by_side:
        p.annual(all_lines)
    else:
        p.monthly(flat)
    return p.result()


class NpsParser:
    name = "nps"

    def parse(self, path: Path, password: str | None) -> ParsedStatement:
        return parse_pages(read_pages(path, password))


register(NpsParser())
