"""ICICI Bank credit card statement.

Deterministic, no LLM. Page 1 holds a summary block (total due = previous balance + purchases + cash advances
- payments) whose values sit under their labels; the transaction table has its columns at x positions that
differ between page 1 (a side panel pushes it right) and later pages, so each page's header row sets them.
A credit carries a `CR` word after the amount; everything else is a debit. The statement is refused unless the
summary adds up and the rows explain the change in dues.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

from finance.models import AccountTxn, ParsedAccountStatement
from finance.parsers.base import register
from finance.parsers.cardlib import build_statement, long_date, make_txn, money, slash_date
from finance.parsers.pdftext import Word, group_lines, line_text, read_pages

INSTITUTION = "ICICI Bank"
_CARD = re.compile(r"^\d{4}[Xx*]{4,10}\d{4}$")
_PERIOD = re.compile(r"Statement period\s*:\s*(?P<a>[A-Za-z]+ \d{1,2}, \d{4}) to (?P<b>[A-Za-z]+ \d{1,2}, \d{4})")
_LONG = re.compile(r"[A-Za-z]+ \d{1,2}, \d{4}")
CONTINUATION_GAP = 16       # a description line wraps at most this far below its row
SIDE_PANEL_X = 200          # page 1 only: left of this is a rewards panel, not the table


class _Parser:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.cards: list[str] = []
        self.txns: list[AccountTxn] = []
        self.summary: dict[str, Decimal] = {}
        self.period: tuple | None = None
        self.panel_dates: list = []
        self.rows: list[dict] = []

    def feed_page(self, words: list[Word], first: bool) -> None:
        lines = group_lines(words)
        if first:
            self._summary(lines)
            self._panel(lines)
        for ln in lines:
            if (m := _PERIOD.search(line_text(ln))) and self.period is None:
                self.period = (long_date(m.group("a")), long_date(m.group("b")))
        self._table(lines)

    # ── summary block (page 1) ────────────────────────────────────────────────

    def _summary(self, lines: list[list[Word]]) -> None:
        def label_line(*needles: str) -> int | None:
            for i, ln in enumerate(lines):
                have = {w.text.lower() for w in ln}
                if all(n in have for n in needles):
                    return i
            return None

        def amounts_below(i: int, within: float = 40, every_line: bool = False) -> list[tuple[float, Decimal]]:
            """Amounts under label line `i`: the first line that has any, or (every_line) all lines in range."""
            out: list[tuple[float, Decimal]] = []
            for ln in lines[i + 1:]:
                if ln[0].top - lines[i][0].top > within:
                    break
                found = [(w.x0, v) for w in ln if (v := money(w.text)) is not None]
                if found and not every_line:
                    return found
                out += found
            return out

        i = label_line("previous", "balance", "purchases")
        if i is None:
            raise ValueError("ICICI card summary block not found; layout not recognised")
        starts = {w.text.lower(): w.x0 for w in lines[i]
                  if w.text.lower() in ("total", "previous", "purchases", "cash", "payments")}
        names = {"total": "total", "previous": "previous", "purchases": "purchases", "cash": "cash", "payments": "payments"}
        for x, v in amounts_below(i, 25, every_line=True):
            owner = max((s for s in starts.items() if s[1] <= x + 8), key=lambda s: s[1], default=None)
            if owner:
                self.summary[names[owner[0]]] = v
        if set(self.summary) != set(names):
            raise ValueError("ICICI card summary values not found under their labels; layout not recognised")
        j = label_line("minimum", "amount", "due")
        if j is not None and (found := amounts_below(j, 25)):
            self.summary["min_due"] = found[0][1]
        j = label_line("credit", "limit", "available")
        if j is not None and (found := amounts_below(j, 40)):
            self.summary["credit_limit"] = found[0][1]

    def _panel(self, lines: list[list[Word]]) -> None:
        """The statement's own dates sit in a left panel as 'Month dd, yyyy'; the due date is the one after the period."""
        for ln in lines:
            if ln[0].top > 260:
                break
            left = " ".join(w.text for w in ln if w.x0 < SIDE_PANEL_X)
            for m in _LONG.finditer(left):
                try:
                    self.panel_dates.append(long_date(m.group(0)))
                except ValueError:
                    pass

    # ── transaction table ─────────────────────────────────────────────────────

    def _table(self, lines: list[list[Word]]) -> None:
        cols = None
        last: dict | None = None
        for ln in lines:
            ws = {w.text: w for w in ln}
            if "Date" in ws and "Transaction" in ws and "Amount" in ws:           # header row sets the columns
                cols = {"date": ws["Date"].x0, "desc": ws["Transaction"].x0,
                        "reward": ws["Reward"].x0 if "Reward" in ws else ws["Amount"].x0 - 60,
                        "amount": ws["Amount"].x0}
                last = None
                continue
            if cols is None:
                continue
            card = [w.text for w in ln if _CARD.match(w.text)]
            if card:
                if card[0] not in self.cards:
                    self.cards.append(card[0])
                last = None
                continue
            text = line_text(ln)
            if text.startswith("#") or "International Spends" in text:
                last = None
                continue
            first = next((w for w in ln if abs(w.x0 - cols["date"]) <= 3 and slash_date(w.text)), None)
            if first:
                last = self._row(ln, first, cols)
                continue
            if last is not None and ln[0].top - last["top"] <= CONTINUATION_GAP and ln[0].x0 >= cols["desc"] - 2:
                extra = [w.text for w in ln if cols["desc"] - 2 <= w.x0 < cols["reward"] - 3]
                if extra:
                    last["desc"].append(" ".join(extra))
                    last["top"] = ln[0].top
                    continue
            last = None

    def _row(self, ln: list[Word], first: Word, cols: dict) -> dict:
        amount_words = [w for w in ln if w.x0 >= cols["amount"] - 30 and money(w.text) is not None]
        if not amount_words:
            raise ValueError("a transaction row has no amount in the amount column; layout not recognised")
        amt = amount_words[-1]
        credit = any(w.text.upper() == "CR" and w.x0 > amt.x0 for w in ln)
        desc = [w.text for w in ln if cols["desc"] - 2 <= w.x0 < cols["reward"] - 3]
        row = {"top": ln[0].top, "desc": desc, "day": slash_date(first.text), "amount": money(amt.text), "credit": credit}
        self.rows.append(row)
        return row

    def result(self) -> ParsedAccountStatement:
        s = self.summary
        if not self.cards:
            raise ValueError("no card number found on the statement; layout not recognised")
        if len(self.cards) > 1:
            self.warnings.append(f"The statement lists {len(self.cards)} cards; all rows are kept under the first.")
        if self.period is None:
            raise ValueError("statement period not found; layout not recognised")
        later = sorted({d for d in self.panel_dates if d > self.period[1]})
        due = later[0] if len(later) == 1 else None                 # several candidates: do not guess
        if due is None:
            self.warnings.append("Payment due date not found on the statement." if not later else
                                 "Payment due date is ambiguous on the statement; left empty.")
        txns = [make_txn(r["day"], " ".join(r["desc"]), r["amount"], r["credit"]) for r in self.rows]
        return build_statement(
            parser=IciciCardParser.name, institution=INSTITUTION, identifier=self.cards[0],
            previous_due=s["previous"], total_due=s["total"],
            summary_sum=s["previous"] + s["purchases"] + s["cash"] - s["payments"], txns=txns,
            period_from=self.period[0], period_to=self.period[1], statement_date=self.period[1], due_date=due,
            min_due=s.get("min_due"), credit_limit=s.get("credit_limit"), warnings=self.warnings)


def parse_pages(pages: list[list[Word]]) -> ParsedAccountStatement:
    p = _Parser()
    for i, words in enumerate(pages):
        p.feed_page(words, first=(i == 0))
    return p.result()


class IciciCardParser:
    name = "icici_card"

    def parse(self, path: Path, password: str | None) -> ParsedAccountStatement:
        return parse_pages(read_pages(path, password))


register(IciciCardParser())
