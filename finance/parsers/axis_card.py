"""Axis Bank credit card statement.

Deterministic, no LLM. The summary reads left to right as previous balance - payments - credits + purchases +
cash advances + other debits and charges = total payment due; every amount carries a `Dr` or `Cr` word (money owed
or in the customer's favour). Transaction rows start with `dd/mm/yyyy`, and the amount's `Dr` / `Cr` word gives
the direction (Dr = spent, Cr = credited). The statement is refused unless the summary adds up and the rows
explain the change in dues.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

from finance.models import ParsedAccountStatement
from finance.parsers.base import register
from finance.parsers.cardlib import build_statement, make_txn, money, slash_date
from finance.parsers.pdftext import Word, group_lines, line_text, read_pages

INSTITUTION = "Axis Bank"
_CARD = re.compile(r"^\d{6}[Xx*]{4,8}\d{4}$")
DR_CR = ("dr", "cr")


def _signed(ln: list[Word]) -> list[tuple[float, Decimal]]:
    """Amounts on a line with their Dr/Cr word applied: owed is positive, in the customer's favour negative."""
    out: list[tuple[float, Decimal]] = []
    for i, w in enumerate(ln):
        if (v := money(w.text)) is None:
            continue
        nxt = ln[i + 1].text.lower() if i + 1 < len(ln) else ""
        out.append((w.x0, -v if nxt == "cr" else v))
    return out


class _Parser:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.card: str | None = None
        self.vals: dict[str, Decimal] = {}
        self.period: tuple | None = None
        self.due_date = None
        self.statement_date = None
        self.rows: list[dict] = []

    def summary(self, lines: list[list[Word]]) -> None:
        def find(*needles: str) -> int | None:
            for i, ln in enumerate(lines):
                t = line_text(ln).lower()
                if all(n in t for n in needles):
                    return i
            return None

        i = find("total payment due", "minimum payment due", "payment due date")
        if i is None:
            raise ValueError("Axis card summary labels not found; layout not recognised")
        ln = next((x for x in lines[i + 1:i + 3]), [])
        dates = [d for w in ln if (d := slash_date(w.text))]
        if len(dates) != 4:
            raise ValueError("Axis card statement dates not found under their labels; layout not recognised")
        self.period, self.due_date, self.statement_date = (dates[0], dates[1]), dates[2], dates[3]
        amounts = _signed(ln)
        if len(amounts) != 2:
            raise ValueError("Axis card total and minimum due not found; layout not recognised")
        self.vals["total_head"], self.vals["min_due"] = amounts[0][1], amounts[1][1]

        j = find("card number", "credit limit")
        if j is not None:
            for ln in lines[j + 1:j + 4]:                    # a note from the right-hand column can sit in between
                if self.card is None and (card := next((w.text for w in ln if _CARD.match(w.text)), None)):
                    self.card = card
                    nums = [v for w in ln if (v := money(w.text)) is not None]
                    if nums:
                        self.vals["credit_limit"] = nums[0]

        k = find("previous balance", "payments", "total payment due")
        if k is None:
            raise ValueError("Axis card breakdown labels not found; layout not recognised")
        row = next((r for ln in lines[k + 1:k + 4] if len(r := _signed(ln)) == 7), [])
        if len(row) != 7:
            raise ValueError("Axis card breakdown does not have the expected seven amounts; layout not recognised")
        keys = ("previous", "payments", "credits", "purchases", "cash", "other", "total")
        self.vals.update({key: v for key, (_, v) in zip(keys, row)})

    def table(self, lines: list[list[Word]]) -> None:
        cols = None
        for ln in lines:
            names = {w.text.upper(): w for w in ln}
            if "DATE" in names and "TRANSACTION" in names and "AMOUNT" in names:
                cols = {"date": names["DATE"].x0, "merchant": names["MERCHANT"].x0 if "MERCHANT" in names else 380,
                        "amount": names["AMOUNT"].x0}
                continue
            if cols is None:
                continue
            if "End of Statement" in line_text(ln):
                cols = None
                continue
            day = slash_date(ln[0].text) if ln and abs(ln[0].x0 - cols["date"]) <= 20 else None
            if not day:
                continue
            right = [w for w in ln if w.x0 >= cols["amount"] - 40]
            amt = next((w for w in right if money(w.text) is not None), None)
            marker = next((w.text.lower() for w in right if w.text.lower() in DR_CR), None)
            if amt is None or marker is None:
                raise ValueError("a transaction row has no amount with Dr/Cr in the amount column; layout not recognised")
            desc = [w.text for w in ln[1:] if w.x0 < cols["merchant"] - 28]
            self.rows.append({"day": day, "desc": desc, "amount": money(amt.text), "credit": marker == "cr"})

    def result(self) -> ParsedAccountStatement:
        v = self.vals
        if not self.card:
            raise ValueError("no card number found on the statement; layout not recognised")
        if v["total"] != v["total_head"]:
            raise ValueError("the total payment due is printed twice and the two disagree; layout not recognised")
        txns = [make_txn(r["day"], " ".join(r["desc"]), r["amount"], r["credit"]) for r in self.rows]
        return build_statement(
            parser=AxisCardParser.name, institution=INSTITUTION, identifier=self.card, previous_due=v["previous"],
            total_due=v["total"],
            summary_sum=v["previous"] - v["payments"] - v["credits"] + v["purchases"] + v["cash"] + v["other"],
            txns=txns, period_from=self.period[0], period_to=self.period[1], statement_date=self.statement_date,
            due_date=self.due_date, min_due=v.get("min_due"), credit_limit=v.get("credit_limit"),
            warnings=self.warnings)


def parse_pages(pages: list[list[Word]]) -> ParsedAccountStatement:
    p = _Parser()
    for i, words in enumerate(pages):
        lines = group_lines(words)
        if i == 0:
            p.summary(lines)
        p.table(lines)
    return p.result()


class AxisCardParser:
    name = "axis_card"

    def parse(self, path: Path, password: str | None) -> ParsedAccountStatement:
        return parse_pages(read_pages(path, password))


register(AxisCardParser())
