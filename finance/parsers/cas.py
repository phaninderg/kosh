"""CAS parser (CAMS / KFintech / MF Central) built on casparser.

Unlike the old analysis pipeline (which kept active holdings only),
this keeps fully redeemed schemes so the ledger holds complete history.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from finance.models import ParsedFolio, ParsedScheme, ParsedStatement, ParsedTxn
from finance.parsers.base import register

# Transaction kinds that move money into / out of the investor's pocket (the same rule finance.xirr callers use).
OUTFLOW_KINDS = ("PURCHASE", "SWITCH_IN", "DIVIDEND_REINVEST")
INFLOW_KINDS = ("REDEMPTION", "SWITCH_OUT")


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    return Decimal(str(v))


def _date(v: Any) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(v), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"unparseable date: {v!r}")


def _kind(t: Any) -> str:
    return str(getattr(t, "value", t)).upper()


class CasParser:
    name = "cas"

    def parse(self, path: Path, password: str | None) -> ParsedStatement:
        import casparser

        last_exc: Exception | None = None
        data = None
        for pwd in (password or "", ""):
            try:
                data = casparser.read_cas_pdf(str(path), password=pwd, output="dict")
                break
            except Exception as exc:       # wrong password etc. — try the next candidate
                last_exc = exc
        if data is None:
            raise RuntimeError(f"casparser failed: {last_exc}")
        return self.from_casdata(data)

    def from_casdata(self, data: Any) -> ParsedStatement:
        if not hasattr(data, "folios"):
            raise ValueError("Unsupported statement type (NSDL/CDSL demat CAS is not handled yet)")

        period = data.statement_period
        summary = str(getattr(data, "cas_type", "")).upper().endswith("SUMMARY")
        stmt = ParsedStatement(
            kind="summary" if summary else "detailed",
            parser="cas_summary" if summary else self.name,
            period_from=_date(getattr(period, "from_", None) or period.model_dump(by_alias=True)["from"]),
            period_to=_date(period.to),
        )
        for folio in data.folios:
            schemes: list[ParsedScheme] = []
            for s in folio.schemes:
                txns = [
                    ParsedTxn(
                        date=_date(t.date), kind=_kind(t.type), description=t.description or "",
                        amount=_dec(t.amount), units=_dec(t.units), nav=_dec(t.nav),
                        balance=_dec(t.balance),
                    )
                    for t in s.transactions
                ]
                val = s.valuation
                cost = _dec(val.cost)
                if not cost:                       # fall back to the closing valuation
                    cost = sum(
                        (abs(t.amount) for t in txns
                         if t.amount and t.amount > 0 and any(k in t.kind for k in OUTFLOW_KINDS)),
                        Decimal(0),
                    ) or None
                schemes.append(ParsedScheme(
                    scheme_name=(s.scheme or "").strip(),
                    isin=s.isin or f"NOISIN:{s.rta_code}",
                    amfi_code=s.amfi or "",
                    close_units=_dec(s.close) or Decimal(0),
                    close_calculated=_dec(s.close_calculated),
                    valuation_date=_date(val.date),
                    nav=_dec(val.nav) or Decimal(0),
                    cost=cost,
                    value=_dec(val.value) or Decimal(0),
                    txns=txns,
                ))
            stmt.folios.append(ParsedFolio(folio=folio.folio, amc=folio.amc, schemes=schemes))
        return stmt


register(CasParser())
