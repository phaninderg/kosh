"""Tata (Neu) credit card statement.

Deterministic, no LLM. Page 1 has labelled summary values and an account-summary row: previous balance - payments and
credits + purchases and other charges + finance charges = total outstanding, which Tata prints rounded to a whole
rupee. The headline *Total Amount Due* is what to pay by the due date and can be lower than the outstanding balance
(instalments not yet billed), so the balance is checked against the summary row and the headline is kept as the
amount due. Rows read `dd Mon yy description amount CODE` under a `Date ... Amount` header, which sets the amount
column; a tax line under a transaction has no date but its own amount and code. From the statement's legend: C credit
and T temporary credit; D debit, EN encash, FP Flexipay, EMD easy money draft and BT balance transfer are money owed.
Lines coded M (monthly instalment) or carrying no code are information, not movements: the totals only balance
without them (checked on real statements), so they are left out and reported in a warning. A statement is refused
unless the rows that remain explain the change in dues exactly.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from finance.models import ParsedAccountStatement
from finance.parsers.base import register
from finance.parsers.cardlib import MONTHS, build_statement, day_month_year, make_txn, money, rupee_rounded, whole_money
from finance.parsers.pdftext import Word, group_lines, line_text, read_pages

INSTITUTION = "Tata Neu"
_PERIOD = re.compile(r"Statement Period:\s*(\d{1,2}) ([A-Za-z]{3}) (\d{2}) to (\d{1,2}) ([A-Za-z]{3}) (\d{2})")
_DMY4 = re.compile(r"(\d{1,2}) ([A-Za-z]{3}),? (\d{4})")
_GROUP = re.compile(r"^[\dXx*]{4}$")
RIGHT_PANEL_X = 300         # the headline values sit in a right-hand panel
DEBIT_CODES = {"D", "EN", "FP", "EMD", "BT"}
CREDIT_CODES = {"C", "T"}
MEMO_CODES = {"M"}          # monthly instalment lines are not part of the period's movements
CONTINUATION_GAP = 16       # a wrapped line or tax line sits at most this far below the row above it
DESC_X = 71                 # the description column starts here (the date uses the columns to its left)


def _words(ln: list[Word]) -> list[str]:
    return [w.text.strip("*()`:").lower() for w in ln]


def _short_year(d: str, mon: str, yy: str) -> date:
    return day_month_year(d, mon, str(2000 + int(yy)))


class _Parser:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.card: str | None = None
        self.vals: dict[str, Decimal] = {}
        self.period: tuple | None = None
        self.statement_date = self.due_date = None
        self.rows: list[dict] = []
        self.memo: list[dict] = []

    def summary(self, lines: list[list[Word]]) -> None:
        def find(*needles: str, min_x: float = 0, max_x: float = 9999) -> int | None:
            for i, ln in enumerate(lines):
                have = _words([w for w in ln if min_x <= w.x0 < max_x])
                if all(any(h.startswith(n) for h in have) for n in needles):
                    return i
            return None

        def below(i: int, within: float, pick, min_x: float = 0, max_x: float = 9999):
            for ln in lines[i + 1:]:
                if ln[0].top - lines[i][0].top > within:
                    break
                got = pick([w for w in ln if min_x <= w.x0 < max_x])
                if got is not None:
                    return got
            return None

        first_money = lambda ws: next((v for w in ws if (v := money(w.text)) is not None), None)
        first_date = lambda ws: next((day_month_year(*m.groups()) for m in [_DMY4.search(line_text(ws))] if m), None)

        for ln in lines:
            if (m := _PERIOD.search(line_text(ln))):
                self.period = (_short_year(*m.groups()[:3]), _short_year(*m.groups()[3:]))
        i = find("credit", "card", "number", min_x=RIGHT_PANEL_X)
        if i is not None:
            self.card = below(i, 40, lambda ws: "".join(w.text for w in ws) if len(ws) == 4 and all(_GROUP.match(w.text) for w in ws) else None)
        for key, needles in (("amount_due", ("total", "amount", "due")), ("min_due", ("minimum", "amount", "due"))):
            j = find(*needles, min_x=RIGHT_PANEL_X)
            if j is not None:
                v = below(j, 40, first_money, min_x=RIGHT_PANEL_X)
                if v is not None:
                    self.vals[key] = v
        j = find("statement", "date", min_x=RIGHT_PANEL_X)
        if j is not None:
            self.statement_date = below(j, 30, first_date, min_x=RIGHT_PANEL_X)
        j = find("payment", "due", "date", min_x=RIGHT_PANEL_X)
        if j is not None:
            self.due_date = below(j, 30, first_date, min_x=RIGHT_PANEL_X)
        j = find("credit", "limit", max_x=RIGHT_PANEL_X)
        if j is not None:
            v = below(j, 30, lambda ws: next((x for w in ws if (x := whole_money(w.text)) is not None), None), max_x=150)
            if v is not None:
                self.vals["credit_limit"] = v

        k = find("previous", "balance", "total", "outstanding")
        if k is None:
            raise ValueError("Tata card summary labels not found; layout not recognised")
        row = below(k, 45, lambda ws: [money(w.text) for w in ws] if len([w for w in ws if money(w.text) is not None]) == 5 else None)
        if row is None:
            raise ValueError("Tata card summary does not have the expected five amounts; layout not recognised")
        self.vals.update(zip(("previous", "payments", "purchases", "finance", "total_row"), row))

    def table(self, lines: list[list[Word]]) -> None:
        amount_x = None
        last: dict | None = None
        for ln in lines:
            if len(ln) == 2 and ln[0].text == "Date" and ln[1].text == "Amount":
                amount_x, last = ln[1].x0, None
                continue
            if amount_x is None:
                continue
            dated = len(ln) >= 5 and re.fullmatch(r"\d{2}", ln[0].text) and ln[1].text.lower() in MONTHS \
                and re.fullmatch(r"\d{2}", ln[2].text)
            if not dated:
                # A tax or charge line printed under a transaction has no date but its own amount and code; a line
                # with no amount is the wrapped end of the description above it.
                if last is None or ln[0].top - last["top"] > CONTINUATION_GAP or ln[0].x0 < DESC_X - 5:
                    last = None
                    continue
                in_amount_col = [w for w in ln if w.x0 >= amount_x - 25]
                if not in_amount_col:
                    last["desc"] += [w.text for w in ln]
                    last["top"] = ln[0].top
                    continue
                if not self._has_amount_tail(ln, amount_x):
                    last = None
                    continue
                last = self._row(ln, amount_x, last["day"], start=0)
                continue
            last = self._row(ln, amount_x, _short_year(*(w.text for w in ln[:3])), start=3)

    @staticmethod
    def _has_amount_tail(ln: list[Word], amount_x: float) -> bool:
        tail = ln[-1] if money(ln[-1].text) is not None else (ln[-2] if len(ln) > 1 else None)
        return tail is not None and money(tail.text) is not None and tail.x0 >= amount_x - 25

    def _row(self, ln: list[Word], amount_x: float, day: date, start: int) -> dict:
        marker = None if money(ln[-1].text) is not None else ln[-1].text
        amt = ln[-1] if marker is None else ln[-2]
        if money(amt.text) is None or amt.x0 < amount_x - 25:
            raise ValueError("a transaction row has no amount in the amount column; layout not recognised")
        if marker is not None and marker not in DEBIT_CODES | CREDIT_CODES | MEMO_CODES:
            raise ValueError(f"a transaction row has the unknown code {marker!r}; layout not recognised")
        memo = marker is None or marker in MEMO_CODES
        row = {"day": day, "desc": [w.text for w in ln[start:ln.index(amt)]], "amount": money(amt.text),
               "credit": marker in CREDIT_CODES, "top": ln[0].top, "memo": memo}
        (self.memo if memo else self.rows).append(row)
        return row

    def result(self) -> ParsedAccountStatement:
        v = self.vals
        if not self.card:
            raise ValueError("no card number found on the statement; layout not recognised")
        if "amount_due" not in v or not self.period or not self.statement_date:
            raise ValueError("total due, statement date or period not found; layout not recognised")
        if self.due_date is None:
            self.warnings.append("Payment due date not found on the statement.")
        exact = v["previous"] - v["payments"] + v["purchases"] + v["finance"]
        total = rupee_rounded(v["total_row"], exact, self.warnings)      # the balance owed (Total Outstanding)

        txns = [make_txn(r["day"], " ".join(r["desc"]), r["amount"], r["credit"]) for r in self.rows]
        if self.memo:
            self.warnings.append(f"{len(self.memo)} line(s) coded M (monthly instalment) or without a D/C code are not "
                                 "movements in this statement's totals and were left out.")
        return build_statement(
            parser=TataCardParser.name, institution=INSTITUTION, identifier=self.card, previous_due=v["previous"],
            total_due=total, summary_sum=exact, txns=txns, period_from=self.period[0], period_to=self.period[1],
            statement_date=self.statement_date, due_date=self.due_date, min_due=v.get("min_due"),
            credit_limit=v.get("credit_limit"), warnings=self.warnings, amount_due=v["amount_due"])


def parse_pages(pages: list[list[Word]]) -> ParsedAccountStatement:
    p = _Parser()
    for i, words in enumerate(pages):
        lines = group_lines(words)
        if i == 0:
            p.summary(lines)
        p.table(lines)
    return p.result()


class TataCardParser:
    name = "tata_card"

    def parse(self, path: Path, password: str | None) -> ParsedAccountStatement:
        return parse_pages(read_pages(path, password))


register(TataCardParser())
