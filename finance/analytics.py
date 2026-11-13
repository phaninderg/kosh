"""Analyst Agent (ledger side): deterministic portfolio metrics from the database.

Sums use Decimal. XIRR uses finance.xirr (a float root-finder on a *rate*); cash flows are
converted at the boundary and the result is shown as a rounded percentage.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from finance.parsers.cas import INFLOW_KINDS, OUTFLOW_KINDS
from finance.xirr import xirr


@dataclass
class HoldingRow:
    account_id: int
    amc: str
    mask: str
    scheme_name: str
    isin: str
    category: str
    units: Decimal
    nav: Decimal
    cost: Decimal | None
    value: Decimal
    as_of: str
    pnl: Decimal | None = None
    abs_return_pct: Decimal | None = None
    xirr_pct: float | None = None
    history_gap: Decimal | None = None      # units held that the recorded transactions do not explain


@dataclass
class PortfolioView:
    rows: list[HoldingRow] = field(default_factory=list)
    total_cost: Decimal = Decimal(0)
    total_value: Decimal = Decimal(0)
    pnl: Decimal = Decimal(0)
    abs_return_pct: Decimal | None = None
    xirr_pct: float | None = None
    as_of: str | None = None
    by_category: dict[str, Decimal] = field(default_factory=dict)
    by_amc: dict[str, Decimal] = field(default_factory=dict)
    xirr_funds: int = 0                     # funds with complete history, i.e. included in the portfolio XIRR
    cost_missing: int = 0                   # funds whose statement gave no cost, so "invested" excludes them


def share_breakdown(values: dict[str, Decimal], keep: int = 7) -> list[tuple[str, Decimal, Decimal]]:
    """(name, amount, share in %) largest first. Past `keep` entries the tail is folded into "Other" (a chart cannot tell
    more than about eight colours apart). Shares add up to 100 within rounding."""
    items = sorted((kv for kv in values.items() if kv[1] > 0), key=lambda kv: -kv[1])
    if len(items) > keep + 1:
        items = items[:keep] + [("Other", sum((v for _, v in items[keep:]), Decimal(0)))]
    total = sum((v for _, v in items), Decimal(0))
    return [(k, v, (v / total * 100).quantize(Decimal("0.1"))) for k, v in items] if total else []


def _category(scheme_name: str) -> str:
    from finance.categories import categorize_fund
    return categorize_fund(scheme_name) or "Other"


def _cashflows(conn: sqlite3.Connection, account_id: int, isin: str) -> list[tuple[date, float]]:
    flows: list[tuple[date, float]] = []
    for t in conn.execute(
        "SELECT date, kind, amount FROM transactions WHERE account_id=? AND isin=? AND amount IS NOT NULL",
        (account_id, isin),
    ):
        amt = Decimal(t["amount"])
        if amt == 0:
            continue
        if any(k in t["kind"] for k in OUTFLOW_KINDS):
            flows.append((date.fromisoformat(t["date"]), -float(abs(amt))))
        elif any(k in t["kind"] for k in INFLOW_KINDS):
            flows.append((date.fromisoformat(t["date"]), float(abs(amt))))
    return flows


def _units_explained(conn: sqlite3.Connection, account_id: int, isin: str) -> Decimal:
    """Net units according to the recorded transactions (purchases +, redemptions -)."""
    total = Decimal(0)
    for r in conn.execute("SELECT units FROM transactions WHERE account_id=? AND isin=? AND units IS NOT NULL",
                          (account_id, isin)):
        total += Decimal(r["units"])
    return total


def current_holdings(conn: sqlite3.Connection, account_type: str = "mutual_fund") -> list[sqlite3.Row]:
    """Latest holding of every scheme in accounts of one type (mutual funds by default; NPS has its own view)."""
    return conn.execute(
        "SELECT h.*, a.institution, a.mask FROM holdings h JOIN accounts a ON a.id = h.account_id "
        "WHERE a.type = ? AND h.as_of = (SELECT MAX(as_of) FROM holdings x WHERE x.account_id=h.account_id AND x.isin=h.isin) "
        "ORDER BY a.institution, h.scheme_name", (account_type,)
    ).fetchall()


def portfolio(conn: sqlite3.Connection) -> PortfolioView:
    view = PortfolioView()
    all_flows: list[tuple[date, float]] = []

    for r in current_holdings(conn):
        units = Decimal(r["units"])
        if units <= 0:
            continue
        flows = _cashflows(conn, r["account_id"], r["isin"])
        gap = units - _units_explained(conn, r["account_id"], r["isin"])
        complete = abs(gap) <= max(Decimal("0.05"), units * Decimal("0.0005"))
        value = Decimal(r["value"])
        cost = Decimal(r["cost"]) if r["cost"] is not None else None
        as_of = date.fromisoformat(r["as_of"])
        row = HoldingRow(
            account_id=r["account_id"], amc=r["institution"], mask=r["mask"],
            scheme_name=r["scheme_name"], isin=r["isin"], category=_category(r["scheme_name"]),
            units=units, nav=Decimal(r["nav"]), cost=cost, value=value, as_of=r["as_of"],
        )
        if cost:
            row.pnl = value - cost
            row.abs_return_pct = ((value - cost) / cost * 100).quantize(Decimal("0.01"))
            view.total_cost += cost
        if not complete:
            row.history_gap = gap
        row.xirr_pct = xirr(flows + [(as_of, float(value))]) if flows and complete else None
        if flows and complete:                  # an incomplete history would overstate the portfolio XIRR
            all_flows += flows + [(as_of, float(value))]
            view.xirr_funds += 1
        if cost is None:
            view.cost_missing += 1
        view.total_value += value
        view.by_category[row.category] = view.by_category.get(row.category, Decimal(0)) + value
        view.by_amc[row.amc] = view.by_amc.get(row.amc, Decimal(0)) + value
        view.as_of = max(view.as_of or r["as_of"], r["as_of"])
        view.rows.append(row)

    view.pnl = view.total_value - view.total_cost
    if view.total_cost:
        view.abs_return_pct = (view.pnl / view.total_cost * 100).quantize(Decimal("0.01"))
    view.xirr_pct = xirr(all_flows) if all_flows else None
    return view


def nps_overview(conn: sqlite3.Connection) -> dict:
    """NPS as one pot of money, whatever schemes it sits in. Money in is every contribution (yours and your employer's,
    in any scheme, past or present); the value is what the schemes held now are worth. Moving units between schemes
    ("shifting") and charges are not cash flows, so they are left out of XIRR and of "contributed".

    The return is only worked out when the history is complete: the first statement of each account opens with no
    units, and the statements' periods run on without a gap up to the latest one. Otherwise gain and XIRR are withheld
    and `reason` says what is missing."""
    held = [h for h in current_holdings(conn, "nps") if Decimal(h["units"]) > 0]
    rows = [{"scheme_name": h["scheme_name"], "account": f"{h['institution']} …{h['mask']}", "units": Decimal(h["units"]),
             "nav": Decimal(h["nav"]), "value": Decimal(h["value"]), "as_of": h["as_of"]} for h in held]
    total_value = sum((r["value"] for r in rows), Decimal(0))
    for r in rows:
        r["share_pct"] = (r["value"] / total_value * 100).quantize(Decimal("0.1")) if total_value else None

    flows: list[tuple[date, float]] = []
    contributed = Decimal(0)
    reasons: list[str] = []
    first_statement = None
    accounts = conn.execute("SELECT id FROM accounts WHERE type = 'nps'").fetchall()
    for acct in accounts:
        for isin in [r[0] for r in conn.execute("SELECT DISTINCT isin FROM transactions WHERE account_id=?", (acct["id"],))]:
            flows += _cashflows(conn, acct["id"], isin)
            contributed += sum((Decimal(t["amount"]) for t in conn.execute(
                "SELECT amount FROM transactions WHERE account_id=? AND isin=? AND kind LIKE 'PURCHASE%' "
                "AND amount IS NOT NULL", (acct["id"], isin))), Decimal(0))
        reasons += _nps_history_problems(conn, acct["id"])
        docs = conn.execute("SELECT MIN(d.period_from) f FROM source_documents d JOIN holdings h ON h.doc_id = d.id "
                            "WHERE h.account_id = ?", (acct["id"],)).fetchone()
        if docs and docs["f"] and (first_statement is None or docs["f"] < first_statement):
            first_statement = docs["f"]
    as_of = max((r["as_of"] for r in rows), default=None)
    complete = bool(rows) and not reasons
    xirr_pct = gain = None
    if complete and flows:
        gain = total_value - contributed
        xirr_pct = xirr(flows + [(date.fromisoformat(as_of), float(total_value))])
    return {"rows": rows, "total_value": total_value, "total_contributed": contributed, "gain": gain, "xirr_pct": xirr_pct,
            "as_of": as_of, "complete": complete, "reasons": reasons, "first_statement": first_statement}


def overview(conn: sqlite3.Connection, today: date | None = None) -> dict:
    """Net worth at a glance: what you own (bank balance, fixed deposits, mutual funds, PPF, NPS). Credit card bills are
    reported beside it, not subtracted: nothing says whether a bill is already paid until the next statement arrives, so
    deducting it would misstate your money. Each part is as of its own latest statement, and says so; nothing here
    estimates a value.
    The money classes keep a fixed order and key, which the page ties to a fixed colour.

    Card dues depend on today's date: a card's latest bill is listed only while its due date is today or later. Once the
    date has passed the bill is assumed paid (the statement that would show it is not imported yet), so it is no longer
    shown as due, and the card is listed under `stale` so the page can ask for the newer statement."""
    today = today or date.today()
    acc = accounts_overview(conn)

    def total(kind: str) -> tuple[Decimal, list[dict]]:
        rows = [a for a in acc["accounts"] if a["type"] == kind]
        return sum((Decimal(a["balance"]) for a in rows), Decimal(0)), rows

    bank, bank_rows = total("bank")
    ppf, ppf_rows = total("ppf")
    card_balance, card_rows = total("card")                       # negative while money is owed
    deposits = acc["deposits"]
    fd = sum((Decimal(d["current_balance"]) for d in deposits if d["current_balance"] is not None), Decimal(0))
    funds = portfolio(conn)
    nps = nps_overview(conn)

    latest = {s["account_id"]: s for s in conn.execute(           # each card's latest statement: what to pay and by when
        "SELECT * FROM statement_summaries s WHERE s.statement_date = "
        "(SELECT MAX(statement_date) FROM statement_summaries x WHERE x.account_id = s.account_id)")}
    dues, stale, owed = [], [], Decimal(0)
    for card in card_rows:
        balance = Decimal(card["balance"])
        outstanding = -balance if balance < 0 else Decimal(0)     # a card in credit owes nothing
        s = latest.get(card["id"])
        if s and s["due_date"] and s["due_date"] < today.isoformat():
            stale.append({"card": f"{card['institution']} …{card['mask']}", "statement_date": s["statement_date"], "due_date": s["due_date"]})
            continue
        if outstanding > 0:
            owed += outstanding
            dues.append({"card": f"{card['institution']} …{card['mask']}", "statement_date": s["statement_date"] if s else card["as_of"],
                         "due_date": s["due_date"] if s else None,
                         "payable": Decimal(s["amount_due"]) if s and s["amount_due"] is not None else outstanding,
                         "minimum": Decimal(s["min_due"]) if s and s["min_due"] is not None else None,
                         "outstanding": outstanding})
    dues.sort(key=lambda d: (d["due_date"] is None, d["due_date"] or ""))

    assets = [
        {"key": "bank", "label": "Bank balance", "value": bank, "present": bool(bank_rows), "href": "/accounts",
         "as_of": max((a["as_of"] for a in bank_rows), default=None),
         "lines": [f"{a['name']}: {a['balance']}" for a in bank_rows] if len(bank_rows) > 1 else []},
        {"key": "fd", "label": "Fixed deposits", "value": fd, "present": bool(deposits), "href": "/accounts",
         "as_of": max((d["as_of"] for d in deposits), default=None), "lines": []},
        {"key": "mf", "label": "Mutual funds", "value": funds.total_value, "present": bool(funds.rows), "href": "/portfolio",
         "as_of": funds.as_of, "gain": funds.pnl if funds.total_cost else None, "gain_pct": funds.abs_return_pct,
         "xirr_pct": funds.xirr_pct, "lines": []},
        {"key": "ppf", "label": "PPF", "value": ppf, "present": bool(ppf_rows), "href": "/accounts",
         "as_of": max((a["as_of"] for a in ppf_rows), default=None), "lines": []},
        {"key": "nps", "label": "NPS", "value": nps["total_value"], "present": bool(nps["rows"]), "href": "/accounts",
         "as_of": nps["as_of"], "gain": nps["gain"], "xirr_pct": nps["xirr_pct"], "lines": []},
    ]
    assets_total = sum((a["value"] for a in assets), Decimal(0))
    for a in assets:
        a["share_pct"] = (a["value"] / assets_total * 100).quantize(Decimal("0.1")) if assets_total and a["value"] > 0 else None
    liability = {"key": "card", "label": "Credit card dues", "value": owed, "present": bool(card_rows), "href": "/accounts",
                 "as_of": max((a["as_of"] for a in card_rows), default=None), "dues": dues, "stale": stale,
                 "payable": sum((d["payable"] for d in dues), Decimal(0)),
                 "next_due": min((d["due_date"] for d in dues if d["due_date"]), default=None)}
    return {"assets": assets, "liability": liability, "assets_total": assets_total, "liabilities_total": owed,
            "net_worth": assets_total, "any": any(a["present"] for a in assets) or liability["present"]}


def _nps_history_problems(conn: sqlite3.Connection, account_id: int) -> list[str]:
    """Why an NPS account's history cannot give a true return: it starts with units already held, or there is a stretch
    of time no statement covers."""
    docs = conn.execute(
        "SELECT DISTINCT d.id, d.period_from, d.period_to FROM source_documents d JOIN holdings h ON h.doc_id = d.id "
        "WHERE h.account_id = ? AND d.period_from IS NOT NULL ORDER BY d.period_from, d.period_to", (account_id,)).fetchall()
    if not docs:
        return ["no statement with a period"]
    problems: list[str] = []
    first = docs[0]
    opening = Decimal(0)
    for h in conn.execute("SELECT isin, units FROM holdings WHERE account_id = ? AND doc_id = ?", (account_id, first["id"])):
        rows_units = sum((Decimal(t["units"]) for t in conn.execute(
            "SELECT units FROM transactions WHERE account_id=? AND isin=? AND units IS NOT NULL AND date BETWEEN ? AND ?",
            (account_id, h["isin"], first["period_from"], first["period_to"]))), Decimal(0))
        opening += abs(Decimal(h["units"]) - rows_units)
    if opening > Decimal("0.05"):
        problems.append(f"the earliest statement ({first['period_from']}) already opens with units, so what was contributed "
                        "before it is unknown")
    reach = date.fromisoformat(first["period_to"])
    for d in docs[1:]:
        start, end = date.fromisoformat(d["period_from"]), date.fromisoformat(d["period_to"])
        if start > reach + timedelta(days=1):
            problems.append(f"no statement covers {(reach + timedelta(days=1)).isoformat()} to {(start - timedelta(days=1)).isoformat()}")
        reach = max(reach, end)
    return problems


def redemptions(conn: sqlite3.Connection) -> list[dict]:
    """Every fund with a redemption or switch-out: units and money taken out, the last date, and what is still held
    (from the latest holdings snapshot; 0 means fully redeemed). Also funds that dropped out of a newer statement
    and were closed (automatically, or by answering Review): no redemption transaction exists for those, so their
    units are the last units held, the proceeds are unknown (`recorded` is False) and the date is when they were
    closed. Most recent first."""
    held = {(r["account_id"], r["isin"]): Decimal(r["units"]) for r in current_holdings(conn)}
    out: dict[tuple, dict] = {}
    for t in conn.execute(
            "SELECT t.account_id, t.isin, t.date, t.kind, t.amount, t.units, "
            "COALESCE(NULLIF(t.scheme_name, ''), h.scheme_name) scheme_name, a.institution, a.mask "
            "FROM transactions t JOIN accounts a ON a.id = t.account_id "
            "LEFT JOIN holdings h ON h.account_id = t.account_id AND h.isin = t.isin "
            "WHERE t.isin != '' AND a.type = 'mutual_fund' ORDER BY t.date"):
        if not any(k in t["kind"] for k in INFLOW_KINDS):
            continue
        row = out.setdefault((t["account_id"], t["isin"]), {
            "scheme_name": t["scheme_name"], "amc": t["institution"], "mask": t["mask"], "units": Decimal(0),
            "amount": Decimal(0), "count": 0, "last": t["date"], "held": held.get((t["account_id"], t["isin"]), Decimal(0))})
        row["units"] += abs(Decimal(t["units"])) if t["units"] is not None else Decimal(0)
        row["amount"] += abs(Decimal(t["amount"])) if t["amount"] is not None else Decimal(0)
        row["count"] += 1
        row["last"] = max(row["last"], t["date"])
    for r in current_holdings(conn):
        if Decimal(r["units"]) > 0 or (r["account_id"], r["isin"]) in out:
            continue
        before = conn.execute(
            "SELECT units, scheme_name FROM holdings WHERE account_id=? AND isin=? AND CAST(units AS REAL) > 0 "
            "ORDER BY as_of DESC LIMIT 1", (r["account_id"], r["isin"])).fetchone()
        if before:                                   # held once, now zero, and no statement shows it being sold
            out[(r["account_id"], r["isin"])] = {
                "scheme_name": before["scheme_name"], "amc": r["institution"], "mask": r["mask"],
                "units": Decimal(before["units"]), "amount": None, "count": 0, "last": r["as_of"],
                "held": Decimal(0), "recorded": False}
    for row in out.values():
        row.setdefault("recorded", True)
    return sorted(out.values(), key=lambda r: r["last"], reverse=True)


_SEARCH_COLUMNS = ("t.description", "t.scheme_name", "t.kind", "t.date", "t.amount", "t.units", "t.nav", "t.reference",
                   "t.isin", "a.institution", "a.name", "a.type")


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


TXN_SORTS = {"date_desc": ("Date, newest first", "t.date DESC, t.id DESC"),
             "date_asc": ("Date, oldest first", "t.date ASC, t.id ASC"),
             "amount_desc": ("Amount, largest first", "ABS(CAST(t.amount AS REAL)) DESC, t.date DESC, t.id DESC"),
             "amount_asc": ("Amount, smallest first", "ABS(CAST(t.amount AS REAL)) ASC, t.date DESC, t.id DESC"),
             "desc_asc": ("Description, A to Z", "LOWER(COALESCE(NULLIF(t.description, ''), t.isin, '')) ASC, t.date DESC, t.id DESC")}


def search_transactions(conn: sqlite3.Connection, *, text: str = "", kind: str = "",
                        date_from: str = "", date_to: str = "", account_type: str = "",
                        sort: str = "date_desc", limit: int = 200) -> list[sqlite3.Row]:
    """`text` is split into words; every word must match somewhere (description, fund, type, date, amount,
    units, reference, account), case-insensitively. Amounts match with or without thousands separators."""
    sql = ("SELECT t.*, a.institution, a.mask, a.type account_type, "
           "EXISTS (SELECT 1 FROM transfer_links l WHERE l.txn_out = t.id OR l.txn_in = t.id) AS linked "
           "FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE 1=1")
    args: list = []
    if account_type:
        sql += " AND a.type = ?"; args.append(account_type)
    for word in text.split():
        pattern = _like(word)
        clauses = [f"{c} LIKE ? ESCAPE '\\'" for c in _SEARCH_COLUMNS]
        args += [pattern] * len(_SEARCH_COLUMNS)
        if "," in word:                                   # 5,000 also finds the stored amount 5000
            clauses.append("t.amount LIKE ? ESCAPE '\\'"); args.append(_like(word.replace(",", "")))
        sql += " AND (" + " OR ".join(clauses) + ")"
    if kind:
        sql += " AND t.kind = ?"; args.append(kind)
    if date_from:
        sql += " AND t.date >= ?"; args.append(date_from)
    if date_to:
        sql += " AND t.date <= ?"; args.append(date_to)
    sql += f" ORDER BY {TXN_SORTS.get(sort, TXN_SORTS['date_desc'])[1]} LIMIT ?"
    args.append(min(int(limit), 1000))
    return conn.execute(sql, args).fetchall()


def account_types(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT type FROM accounts ORDER BY type")]


def accounts_overview(conn: sqlite3.Connection) -> dict:
    """Latest closing balance per bank / PPF / card account, and the latest snapshot of each fixed deposit.
    Cards are liabilities (their balance is what is owed). Nothing is summed across kinds here: a fixed
    deposit's balance is shown as reported, since banks differ on whether it includes accrued interest."""
    accounts = [dict(r) for r in conn.execute(
        "SELECT a.id, a.type, a.name, a.institution, a.mask, a.liability, b.as_of, b.balance FROM accounts a "
        "JOIN account_balances b ON b.account_id = a.id "
        "WHERE b.as_of = (SELECT MAX(as_of) FROM account_balances x WHERE x.account_id = a.id) "
        "ORDER BY a.type, a.name")]
    deposits = [dict(r) for r in conn.execute(
        "SELECT f.*, a.mask linked_mask FROM fixed_deposits f LEFT JOIN accounts a ON a.id = f.linked_account_id "
        "WHERE f.as_of = (SELECT MAX(as_of) FROM fixed_deposits x WHERE x.ref_hash = f.ref_hash) "
        "ORDER BY f.maturity_date")]
    return {"accounts": accounts, "deposits": deposits}


def cash_flow_by_month(conn: sqlite3.Connection) -> list[dict]:
    """Per month, for bank accounts: opening balance + money in + money out = closing balance. Every movement is
    counted, including linked transfers (own PPF top-up, a card bill paid from the bank); they are marked in the
    transactions list. Balances come from the running balance printed on each row (summed over accounts that had
    activity that month). Cards have no bank balance; see card_spending_by_month."""
    months: dict[str, dict] = {}

    def month(key: str) -> dict:
        return months.setdefault(key, {"month": key, "opening": None, "closing": None, "money_in": Decimal(0),
                                       "money_out": Decimal(0)})

    per_account: dict[tuple, dict] = {}
    for r in conn.execute(
            "SELECT t.id, t.account_id, a.type, substr(t.date, 1, 7) month, t.amount, t.balance FROM transactions t "
            "JOIN accounts a ON a.id = t.account_id WHERE a.type = 'bank' AND t.amount IS NOT NULL "
            "ORDER BY t.account_id, t.date, t.id"):
        amount, m = Decimal(r["amount"]), month(r["month"])
        m["money_in" if amount > 0 else "money_out"] += amount
        acct = per_account.setdefault((r["account_id"], r["month"]), {"net": Decimal(0), "closing": None})
        acct["net"] += amount
        acct["closing"] = Decimal(r["balance"]) if r["balance"] is not None else None
    for (_, key), acct in per_account.items():
        if acct["closing"] is None:
            continue
        m = month(key)
        m["closing"] = (m["closing"] or Decimal(0)) + acct["closing"]
        m["opening"] = (m["opening"] or Decimal(0)) + acct["closing"] - acct["net"]
    return sorted(months.values(), key=lambda m: m["month"], reverse=True)


def card_spending_by_month(conn: sqlite3.Connection) -> list[dict]:
    """Card spending per month (by transaction date) over all cards, and the statements it is built from.
    Spending = purchases, fees and interest; refunds and reversals are netted off. Bill payments received
    (kind PAYMENT) are never spending: they only settle what was already counted. Each month lists the
    statements its rows came from (a row belongs to the statement that first imported it), with the card, the
    statement period. Statement periods rarely match calendar months, so a statement spanning two months appears under
    both with only that month's rows counted."""
    months: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT t.account_id, a.name card, a.mask, t.doc_id, substr(t.date, 1, 7) month, t.amount, t.kind, "
            "d.period_from, d.period_to, d.filename, s.statement_date "
            "FROM transactions t JOIN accounts a ON a.id = t.account_id JOIN source_documents d ON d.id = t.doc_id "
            "LEFT JOIN statement_summaries s ON s.doc_id = t.doc_id AND s.account_id = t.account_id "
            "WHERE a.type = 'card' AND t.amount IS NOT NULL ORDER BY t.date, t.id"):
        amount = Decimal(r["amount"])
        if r["kind"] == "PAYMENT":
            continue
        m = months.setdefault(r["month"], {"month": r["month"], "purchases": Decimal(0), "refunds": Decimal(0),
                                           "statements": {}})
        st = m["statements"].setdefault((r["account_id"], r["doc_id"]), {
            "card": r["card"], "doc_id": r["doc_id"], "statement_date": r["statement_date"],
            "period_from": r["period_from"], "period_to": r["period_to"], "rows": 0, "purchases": Decimal(0),
            "refunds": Decimal(0), "account_id": r["account_id"]})
        st["rows"] += 1
        if amount < 0:
            m["purchases"] -= amount; st["purchases"] -= amount
        else:
            m["refunds"] += amount; st["refunds"] += amount
    for m in months.values():
        m["net"] = m["purchases"] - m["refunds"]
        m["statements"] = sorted(m["statements"].values(), key=lambda s: (s["card"], s["period_from"] or ""))
    return sorted(months.values(), key=lambda m: m["month"], reverse=True)


def txn_kinds(conn: sqlite3.Connection, account_type: str = "") -> list[str]:
    """Transaction types that exist, for one account type when given (a card has no SIPs, a fund no UPI)."""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT t.kind FROM transactions t JOIN accounts a ON a.id = t.account_id "
        "WHERE (? = '' OR a.type = ?) ORDER BY t.kind", (account_type, account_type))]


def list_documents(conn: sqlite3.Connection, limit: int = 50, *, status: str = "", account: str = "",
                   q: str = "", source: str = "") -> list[sqlite3.Row]:
    sql, args = "SELECT * FROM source_documents WHERE 1=1", []
    if source:
        sql += " AND source = ?"; args.append(source)
    if status:
        sql += " AND status = ?"; args.append(status)
    if account:
        sql += " AND account = ?"; args.append(account)
    if q:
        sql += " AND (sender LIKE ? OR subject LIKE ? OR filename LIKE ? OR message_id LIKE ?)"
        args += [f"%{q}%"] * 4
    sql += " ORDER BY COALESCE(picked_up_at, created_at) DESC, id DESC LIMIT ?"
    return conn.execute(sql, (*args, min(int(limit), 1000))).fetchall()


def document_filters(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    statuses = [r[0] for r in conn.execute("SELECT DISTINCT status FROM source_documents ORDER BY status")]
    accounts = [r[0] for r in conn.execute("SELECT DISTINCT account FROM source_documents WHERE account != '' ORDER BY account")]
    return statuses, accounts


def open_review_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM review_queue WHERE status='open' ORDER BY id DESC").fetchall()


def review_items_detailed(conn: sqlite3.Connection) -> list[dict]:
    """Open review items; folio_missing ones carry the funds that would be closed."""
    items = []
    for r in open_review_items(conn):
        item = dict(r)
        item["funds"] = []
        item["doc"] = None
        item["dup"] = None
        if r["kind"] == "needs_attention":
            doc = conn.execute("SELECT * FROM source_documents WHERE id=?", (int(r["ref"].split(":")[1]),)).fetchone()
            item["doc"] = dict(doc) if doc else None
        if r["kind"] == "duplicate_folio":
            _, pa, pb = r["ref"].split(":")
            rows = current_holdings(conn)
            acct = {int(i): conn.execute("SELECT institution, mask FROM accounts WHERE id=?", (int(i),)).fetchone()
                    for i in (pa, pb)}
            isins = sorted({h["isin"] for h in rows if h["account_id"] in (int(pa), int(pb))})
            def side(acc_id, isin):
                h = next((x for x in rows if x["account_id"] == acc_id and x["isin"] == isin), None)
                return dict(units=h["units"], value=Decimal(h["value"]), as_of=h["as_of"]) if h else None
            item["dup"] = dict(
                primary=dict(id=int(pa), **(dict(acct[int(pa)]) if acct[int(pa)] else {})),
                other=dict(id=int(pb), **(dict(acct[int(pb)]) if acct[int(pb)] else {})),
                funds=[dict(name=next(h["scheme_name"] for h in rows if h["isin"] == i and h["account_id"] in (int(pa), int(pb))),
                            a=side(int(pa), i), b=side(int(pb), i)) for i in isins])
        if r["kind"] == "folio_missing":
            acct = int(r["ref"].split(":")[1])
            item["funds"] = [
                dict(scheme_name=h["scheme_name"], units=h["units"], value=Decimal(h["value"]), as_of=h["as_of"])
                for h in current_holdings(conn) if h["account_id"] == acct and Decimal(h["units"]) > 0
            ]
        items.append(item)
    return items
