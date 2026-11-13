"""HDFC Bank credit card statement (regular and RuPay layouts).

Deterministic, no LLM. Page 1 has a summary row whose arithmetic is previous dues - payments + purchases + finance
charges = total due; the rupee sign is printed as the letter C, sometimes glued to the number and sometimes a
word of its own. Rows read `dd/mm/yyyy| hh:mm description amount`, and a credit has a `+` before the amount
(otherwise it is a debit). Each page that has a table repeats its header, which sets the columns. The statement
is refused unless the summary adds up (the total may be printed rounded to the rupee) and the rows explain the change in dues.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

from finance.models import AccountTxn, ParsedAccountStatement
from finance.parsers.base import register
from finance.parsers.cardlib import build_statement, day_month_year, make_txn, money, rupee_rounded, slash_date, whole_money
from finance.parsers.pdftext import Word, group_lines, line_text, read_pages

INSTITUTION = "HDFC Bank"
_CARD = re.compile(r"^\d{6}[Xx*]{4,8}\d{4}$")
_DMY = re.compile(r"(\d{1,2}) ([A-Za-z]{3}),? (\d{4})")
_TIME = re.compile(r"^\d{2}:\d{2}$")
_REF = re.compile(r"\(Ref#\s*[^)\s]*\)?")
CONTINUATION_GAP = 14


def _dates(text: str):
    return [day_month_year(*m.groups()) for m in _DMY.finditer(text)]


class _Parser:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.card: str | None = None
        self.vals: dict[str, Decimal] = {}
        self.statement_date = None
        self.period: tuple | None = None
        self.due_date = None
        self.rows: list[dict] = []

    # ── summary block (page 1) ────────────────────────────────────────────────

    def summary(self, lines: list[list[Word]]) -> None:
        def find(*needles: str) -> int | None:
            for i, ln in enumerate(lines):
                have = {w.text.upper().strip(":") for w in ln}
                if all(n in have for n in needles):
                    return i
            return None

        for ln in lines:
            t = line_text(ln)
            if not self.card:
                self.card = next((w.text for w in ln if _CARD.match(w.text)), None)
            if t.startswith("Statement Date") and (d := _dates(t)):
                self.statement_date = d[0]
            elif t.startswith("Billing Period") and len(d := _dates(t)) == 2:
                self.period = (d[0], d[1])

        i = find("PREVIOUS", "STATEMENT", "TOTAL", "AMOUNT", "DUE")
        if i is None:
            raise ValueError("HDFC card summary labels not found; layout not recognised")
        total_x = next(w.x0 for w in lines[i] if w.text.upper() == "TOTAL")
        got_four = False
        for ln in lines[i + 1:]:
            if ln[0].top - lines[i][0].top > 45:
                break
            found = [(w.x0, v) for w in ln if (v := money(w.text)) is not None]
            if len(found) == 4 and not got_four:                   # previous dues, payments, purchases, charges
                self.vals.update(zip(("previous", "payments", "purchases", "finance"), (v for _, v in found)))
                got_four = True
            elif found and found[-1][0] >= total_x - 6 and "total" not in self.vals:
                self.vals["total"] = found[-1][1]
        if not {"previous", "payments", "purchases", "finance", "total"} <= set(self.vals):
            raise ValueError("HDFC card summary values not found under their labels; layout not recognised")

        j = find("MINIMUM", "DUE", "DATE")
        if j is not None:
            min_x = next(w.x0 for w in lines[j] if w.text.upper() == "MINIMUM")
            for ln in lines[j + 1:j + 3]:
                found = [(w.x0, v) for w in ln if (v := money(w.text)) is not None and w.x0 >= min_x - 6]
                if found:
                    self.vals["min_due"] = found[0][1]
                    if d := _dates(line_text(ln)):
                        self.due_date = d[-1]
                    break
        k = find("TOTAL", "CREDIT", "LIMIT")
        if k is not None:
            for ln in lines[k + 1:k + 6]:
                found = [v for w in ln if (v := whole_money(w.text)) is not None and w.x0 < 150]
                if found:
                    self.vals["credit_limit"] = found[0]               # the first of the limits printed in a row
                    break

    # ── transaction table ─────────────────────────────────────────────────────

    def table(self, lines: list[list[Word]]) -> None:
        cols = None
        last: dict | None = None
        for ln in lines:
            names = {w.text.upper(): w for w in ln}
            if "DATE" in names and "TRANSACTION" in names and "AMOUNT" in names:
                reward = next((w.x0 for w in ln if w.text.lower() in ("base", "reward", "rewards")), None)
                cols = {"desc": names["TRANSACTION"].x0, "amount": names["AMOUNT"].x0, "reward": reward}
                last = None
                continue
            if cols is None:
                continue
            first = ln[0]
            if slash_date(first.text) and len(ln) > 1 and _TIME.match(ln[1].text):
                last = self._row(ln, cols)
                continue
            text = line_text(ln)
            gap = ln[0].top - last["top"] if last else None
            if last is not None and gap <= CONTINUATION_GAP and ln[0].x0 >= cols["desc"] - 2 \
                    and not text.rstrip().endswith("Transactions") and "ID :" not in text:
                extra = [w.text for w in ln if cols["desc"] - 2 <= w.x0 < cols["amount"] - 30]
                if extra and not any(money(w.text) is not None for w in ln if w.x0 >= cols["amount"] - 30):
                    last["desc"] += extra
                    last["top"] = ln[0].top
                    continue
            last = None

    def _row(self, ln: list[Word], cols: dict) -> dict:
        right = [w for w in ln if w.x0 >= cols["amount"] - 30]
        amount_word = next((w for w in right if money(w.text) is not None), None)
        if amount_word is None:
            raise ValueError("a transaction row has no amount in the amount column; layout not recognised")
        credit = any(w.text == "+" for w in right)
        limit = cols["reward"] - 3 if cols["reward"] else cols["amount"] - 30
        desc = [w.text for w in ln[2:] if w.x0 < min(limit, cols["amount"] - 30)]          # after the time; an EMI tag sits left of the header
        row = {"top": ln[0].top, "desc": desc, "day": slash_date(ln[0].text), "amount": money(amount_word.text),
               "credit": credit}
        self.rows.append(row)
        return row

    def result(self) -> ParsedAccountStatement:
        v = self.vals
        if not self.card:
            raise ValueError("no card number found on the statement; layout not recognised")
        if not self.statement_date or not self.period:
            raise ValueError("statement date or billing period not found; layout not recognised")
        if self.due_date is None:
            self.warnings.append("Payment due date not found on the statement.")
        exact = v["previous"] - v["payments"] + v["purchases"] + v["finance"]
        txns: list[AccountTxn] = [
            make_txn(r["day"], _REF.sub("", " ".join(r["desc"])), r["amount"], r["credit"]) for r in self.rows]
        return build_statement(
            parser=HdfcCardParser.name, institution=INSTITUTION, identifier=self.card, previous_due=v["previous"],
            total_due=rupee_rounded(v["total"], exact, self.warnings), summary_sum=exact,
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


class HdfcCardParser:
    name = "hdfc_card"

    def parse(self, path: Path, password: str | None) -> ParsedAccountStatement:
        return parse_pages(read_pages(path, password))


register(HdfcCardParser())
