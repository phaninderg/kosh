"""Pieces shared by the credit card statement parsers (ICICI, HDFC, Axis).

Every card statement carries a summary that must add up (previous dues, payments, purchases, charges = total
due) and a list of rows that must explain the change in dues. A card is a liability, so its balances are
negative while money is owed: opening = -(previous dues), closing = -(total due), purchases are negative and
payments positive, and the same `opening + movements = closing` check that guards bank statements applies.
A statement that fails either check is refused, never imported.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from finance.models import AccountTxn, ParsedAccount, ParsedAccountStatement
from finance.parsers.pdftext import Word
from finance.redact import redact

# ₹ is printed as a backtick (ICICI), a real rupee sign, or the letter C (HDFC); a sign may also lead the number.
_MONEY = re.compile(r"^(?P<neg>-)?(?:[`₹]|C(?=\d))?(?P<num>\d[\d,]*\.\d{2})$")
_DATE_SLASH = re.compile(r"^(\d{2})/(\d{2})/(\d{4})\|?$")
_PAYMENT = re.compile(r"\bpayment\b", re.I)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def money(text: str) -> Decimal | None:
    """'`9,99,999.99', 'C1,234.50', '1,234.50' -> Decimal; anything else -> None."""
    m = _MONEY.match(text.strip())
    if not m:
        return None
    v = Decimal(m.group("num").replace(",", ""))
    return -v if m.group("neg") else v


_WHOLE = re.compile(r"^(?:[`₹]|C(?=\d))?(\d[\d,]*)(?:\.\d{2})?$")


def whole_money(text: str) -> Decimal | None:
    """Like money(), but the paise are optional: HDFC prints the credit limit as 'C1,00,000'."""
    m = _WHOLE.match(text.strip())
    return Decimal(m.group(1).replace(",", "")) if m else None


def slash_date(text: str) -> date | None:
    m = _DATE_SLASH.match(text)
    return date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None


def day_month_year(day: str, month: str, year: str) -> date:
    """'15', 'Mar', '2026' -> date (HDFC prints '15 Mar, 2026')."""
    return date(int(year), MONTHS[month[:3].lower()], int(day))


def long_date(text: str) -> date:
    """'March 15, 2026' (ICICI)."""
    return datetime.strptime(text, "%B %d, %Y").date()


def classify(amount: Decimal, description: str) -> str:
    """DEBIT (spend, fee, interest), PAYMENT (a bill payment received: what the transfer matcher looks for), or
    CREDIT (refund, reversal, cashback)."""
    if amount < 0:
        return "DEBIT"
    return "PAYMENT" if _PAYMENT.search(description) else "CREDIT"


def make_txn(day: date, description: str, spent: Decimal, credit: bool) -> AccountTxn:
    """`spent` is the amount as printed (positive); direction comes from the statement's own marker."""
    desc = redact(re.sub(r"\s+", " ", description).strip(), limit=300)
    amount = spent if credit else -spent
    return AccountTxn(date=day, description=desc, amount=amount, kind=classify(amount, desc))


def rupee_rounded(printed: Decimal, exact: Decimal, warnings: list[str]) -> Decimal:
    """Some issuers (Tata, HDFC) print the total due rounded to the rupee. Accept a printed total equal to the exact
    components rounded to the nearest rupee and return the exact figure, which the rows must then explain to the
    paisa; any other difference is returned unchanged so the summary check refuses it."""
    if printed != exact and printed == exact.quantize(Decimal(1), rounding=ROUND_HALF_UP):
        warnings.append("The printed total is rounded to a whole rupee; the exact figure was worked out from the summary.")
        return exact
    return printed


def in_region(w: Word, lo: float, hi: float) -> bool:
    return lo <= w.x0 < hi


def build_statement(*, parser: str, institution: str, identifier: str, previous_due: Decimal, total_due: Decimal,
                    summary_sum: Decimal, txns: list[AccountTxn], period_from: date | None, period_to: date | None,
                    statement_date: date | None, due_date: date | None, min_due: Decimal | None,
                    credit_limit: Decimal | None, warnings: list[str],
                    amount_due: Decimal | None = None) -> ParsedAccountStatement:
    """`summary_sum` is what the statement's own summary arithmetic gives for the total due; it must equal the
    printed total (this catches a mis-read summary), and the rows must explain the change in dues."""
    mask = identifier[-4:]
    if summary_sum != total_due:
        raise ValueError(f"card …{mask}: the summary does not add up (components give {summary_sum}, "
                         f"statement says {total_due}); layout not recognised")
    if not txns and previous_due != total_due:
        raise ValueError(f"card …{mask}: no transactions were read but the dues changed; layout not recognised")
    acct = ParsedAccount(
        type="card", label="Credit Card", institution=institution, identifier=identifier, period_from=period_from,
        period_to=period_to or statement_date, opening_balance=-previous_due, closing_balance=-total_due, txns=txns,
        statement_date=statement_date or period_to, due_date=due_date, min_due=min_due, amount_due=amount_due, credit_limit=credit_limit)
    errs = acct.integrity_errors()
    if errs:
        raise ValueError(f"card …{mask}: {errs[0]}")
    if acct.statement_date is None:
        raise ValueError(f"card …{mask}: statement date not found; layout not recognised")
    return ParsedAccountStatement(parser=parser, period_from=acct.period_from, period_to=acct.period_to,
                                  accounts=[acct], warnings=warnings)
