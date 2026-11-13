"""Reconciliation Agent: pairs the two legs of a transfer so it is never counted as spending or income.

Two kinds of evidence, strongest first. Writes go through the Ledger; ambiguity goes to Review, never a guess.
  1. Same transaction reference on both legs (a UPI/IMPS RRN, or the id a bank prints on both sides of a
     transfer to its own PPF account): different accounts, equal and opposite amounts.
  2. Card bill payments: a bank debit that reads like a card payment, and a card "payment received" for the
     same amount within a few days. Linked only when exactly one candidate exists on both sides.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from decimal import Decimal

WINDOW_DAYS = 3
# What a bank prints for a card bill payment (CRED pays cards on the user's behalf).
CARD_PAYMENT = re.compile(r"\bCRED\b|CREDIT\s*CARD|\bCC\b|CARD\s*PAYMENT|BILLDESK|BBPS", re.I)


def _linked_ids(conn) -> set[int]:
    ids: set[int] = set()
    for r in conn.execute("SELECT txn_out, txn_in FROM transfer_links"):
        ids.update((r["txn_out"], r["txn_in"]))
    return ids


def match_transfers(ledger, window_days: int = WINDOW_DAYS, doc_id: int | None = None) -> list[str]:
    """Link what can be linked safely; returns notes for the import summary. `doc_id` limits the
    'nothing matched yet' notes to rows of the statement just imported."""
    conn, notes = ledger.conn, []
    linked = _linked_ids(conn)

    # 1. the same reference on both legs
    groups: dict[str, list] = defaultdict(list)
    for r in conn.execute("SELECT id, account_id, amount, reference FROM transactions "
                          "WHERE reference != '' AND amount IS NOT NULL"):
        if r["id"] not in linked:
            groups[r["reference"]].append(r)
    for rows in groups.values():
        if len(rows) != 2:
            continue
        out, inn = sorted(rows, key=lambda r: Decimal(r["amount"]))
        if (out["account_id"] != inn["account_id"] and Decimal(out["amount"]) < 0
                and Decimal(out["amount"]) == -Decimal(inn["amount"])):
            if ledger.link_transfer(out["id"], inn["id"], "reference", "high"):
                linked.update((out["id"], inn["id"]))

    # 2. card bill payments: amount + date window, only when unambiguous
    outs = [r for r in conn.execute(
        "SELECT t.id, t.date, t.amount, t.description FROM transactions t JOIN accounts a ON a.id=t.account_id "
        "WHERE a.type='bank' AND t.amount IS NOT NULL") if r["id"] not in linked
        and Decimal(r["amount"]) < 0 and CARD_PAYMENT.search(r["description"])]
    ins = [r for r in conn.execute(
        "SELECT t.id, t.doc_id, t.date, t.amount FROM transactions t JOIN accounts a ON a.id=t.account_id "
        "WHERE a.type='card' AND t.kind='PAYMENT' AND t.amount IS NOT NULL") if r["id"] not in linked
        and Decimal(r["amount"]) > 0]

    def fits(o, i) -> bool:
        gap = abs((date.fromisoformat(o["date"]) - date.fromisoformat(i["date"])).days)
        return -Decimal(o["amount"]) == Decimal(i["amount"]) and gap <= window_days

    for i in ins:
        cands = [o for o in outs if fits(o, i)]
        rivals = [x for x in ins if cands and fits(cands[0], x)] if len(cands) == 1 else []
        if len(cands) == 1 and len(rivals) == 1:
            if ledger.link_transfer(cands[0]["id"], i["id"], "amount+window", "high"):
                linked.update((cands[0]["id"], i["id"]))
                outs = [o for o in outs if o["id"] != cands[0]["id"]]
        elif cands or len(rivals) > 1:
            ref = f"transfer:{i['id']}"
            if not conn.execute("SELECT 1 FROM review_queue WHERE kind='transfer_ambiguous' AND ref=?",
                                (ref,)).fetchone():
                why = (f"{len(cands)} bank debits of that amount fit" if len(cands) > 1
                       else "several card payments of that amount fit the same bank debit")
                msg = (f"Card payment of ₹{Decimal(i['amount']):,.2f} on {i['date']}: {why}, "
                       f"so it was not linked automatically.")
                ledger.add_review("transfer_ambiguous", ref, msg)
                notes.append(msg)
        elif doc_id is not None and i["doc_id"] == doc_id:
            notes.append(f"Card payment of ₹{Decimal(i['amount']):,.2f} on {i['date']} has no matching bank debit "
                         f"yet; it is linked when the bank statement for that date is imported.")
    return notes
