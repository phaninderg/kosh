"""Typed records exchanged between agents. All money/units are Decimal."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class ParsedTxn(BaseModel):
    date: date
    kind: str                      # normalised type, e.g. PURCHASE_SIP, REDEMPTION
    description: str = ""
    amount: Decimal | None = None
    units: Decimal | None = None
    nav: Decimal | None = None
    balance: Decimal | None = None


class ParsedScheme(BaseModel):
    scheme_name: str
    isin: str
    amfi_code: str = ""
    close_units: Decimal = Decimal(0)
    close_calculated: Decimal | None = None
    valuation_date: date
    nav: Decimal = Decimal(0)
    cost: Decimal | None = None
    value: Decimal = Decimal(0)
    txns: list[ParsedTxn] = Field(default_factory=list)


class ParsedFolio(BaseModel):
    folio: str                     # full number; never persisted, only hashed + last 4
    amc: str
    schemes: list[ParsedScheme]


class ParsedStatement(BaseModel):
    kind: str = "detailed"         # detailed | summary (summary: holdings only, no transactions, fund house unknown)
    account_type: str = "mutual_fund"    # mutual_fund | nps: what kind of account the folios are
    warnings: list[str] = Field(default_factory=list)
    parser: str
    period_from: date | None = None
    period_to: date | None = None
    folios: list[ParsedFolio] = Field(default_factory=list)


class AccountTxn(BaseModel):
    """One bank / card / PPF row. `amount` is the effect on the user's money: negative = money out."""
    date: date
    description: str = ""
    amount: Decimal
    balance: Decimal | None = None          # running balance printed on the statement (banks, PPF)
    kind: str = ""                          # CREDIT | DEBIT (cards add PAYMENT, REFUND, ...)
    mode: str = ""
    reference: str = ""                     # transaction reference (UPI/IMPS RRN, transfer id): not an account number


class ParsedAccount(BaseModel):
    """One account's section of a statement. `identifier` is as printed (already masked by some banks);
    it is hashed and reduced to its last 4 characters, never stored."""
    type: str                               # bank | ppf | card
    label: str = ""                         # Savings, PPF, ...
    institution: str
    identifier: str
    period_from: date | None = None
    period_to: date | None = None
    opening_balance: Decimal | None = None
    closing_balance: Decimal
    txns: list[AccountTxn] = Field(default_factory=list)
    # Cards only: the statement's headline. A card's balances are negative while money is owed, so
    # opening = -(previous dues), closing = -(total due), and the same opening + movements = closing check applies.
    statement_date: date | None = None
    due_date: date | None = None
    min_due: Decimal | None = None
    amount_due: Decimal | None = None       # what the statement asks to be paid by the due date; None = the whole balance
    credit_limit: Decimal | None = None

    def integrity_errors(self) -> list[str]:
        """Money check: opening + every movement = closing, and each row's printed balance follows from the
        one before it. A statement that fails is never imported."""
        errs: list[str] = []
        if self.opening_balance is None:
            return ["opening balance not found, so the statement cannot be verified"]
        running = self.opening_balance
        for i, t in enumerate(self.txns, 1):
            running += t.amount
            if t.balance is not None and t.balance != running:
                errs.append(f"row {i} ({t.date}): printed balance {t.balance} but opening + movements = {running}")
                break
        if not errs and running != self.closing_balance:
            errs.append(f"opening {self.opening_balance} + movements = {running}, statement says {self.closing_balance}")
        return errs


class ParsedDeposit(BaseModel):
    """A fixed deposit listed on a bank statement (snapshot as of the statement date)."""
    number: str                             # as printed; hashed + last 4 only
    institution: str
    linked_account: str = ""
    open_date: date | None = None
    principal: Decimal | None = None
    rate: Decimal | None = None
    tenure: str = ""
    maturity_date: date | None = None
    maturity_amount: Decimal | None = None
    current_balance: Decimal | None = None
    status: str = ""
    as_of: date


class ParsedAccountStatement(BaseModel):
    kind: str = "account"
    parser: str
    period_from: date | None = None
    period_to: date | None = None
    accounts: list[ParsedAccount] = Field(default_factory=list)
    deposits: list[ParsedDeposit] = Field(default_factory=list)
    # What the statement says it holds in linked fixed deposits, per account number (None of this is stored). When an account is
    # here and every deposit row was read (`fd_complete`), a deposit this statement no longer lists has been closed.
    fd_summary: dict[str, Decimal] = Field(default_factory=dict)
    fd_complete: bool = True
    as_of: date | None = None
    warnings: list[str] = Field(default_factory=list)


class ImportResult(BaseModel):
    status: str                    # imported | duplicate
    doc_id: int | None = None
    txn_new: int = 0
    txn_dup: int = 0
    holdings: int = 0
    warnings: list[str] = Field(default_factory=list)


class SourceDocument(BaseModel):
    """One attachment pulled from a source. `data` is excluded from repr/dumps (it is the raw PDF)."""
    account: str
    message_id: str
    sender: str
    subject: str = ""
    filename: str = ""
    sha256: str
    folder: str = ""
    uid: int = 0
    received_at: str | None = None        # email Date header, UTC ISO
    auth: str = ""                        # summary of Authentication-Results
    data: bytes = Field(repr=False, exclude=True)


class SourceMessage(BaseModel):
    uid: int
    message_id: str
    sender: str
    subject: str = ""
    rule_match: str = ""                  # which allowlist rule matched
    skip_reason: str = ""                 # set => do not process; "" => docs are good to go
    reject: bool = False                  # skip_reason is worth surfacing to the user
    docs: list[SourceDocument] = Field(default_factory=list)
