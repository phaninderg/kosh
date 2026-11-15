"""Ledger Agent: the ONLY module that writes to the database.

Every import is idempotent: documents are keyed by content hash, transactions by a
keyed hash of their identifying fields, holdings by (account, isin, as_of).
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path
import time
from decimal import Decimal

from finance.db import connect_rw
from finance.hashing import keyed_hash, last4
from finance.models import (ImportResult, ParsedAccount, ParsedAccountStatement, ParsedDeposit,
                            ParsedStatement, ParsedScheme)

UNITS_TOLERANCE = Decimal("0.001")
UNKNOWN_AMC = {"", "n/a", "na", "unknown", "none"}      # casparser reports "N/A" for summary statements
PLACEHOLDER_AMC = "Unknown fund house"
SAME_MASK_TYPES = ("bank", "ppf")                    # accounts one statement masks and another prints in full

# Email/attachment details stored with each document (subject and filename arrive already masked).
META_FIELDS = ("account", "folder", "uid", "received_at", "subject", "filename", "size", "auth")


def dec_str(d: Decimal | None) -> str | None:
    """Canonical text for a Decimal ('100.00' and '100' map to the same string)."""
    if d is None:
        return None
    s = format(d.normalize(), "f")
    return "0" if s in ("-0", "") else s


def strong_key(isin: str, kind: str, amount, units, nav, balance) -> tuple | None:
    """Identity of a unit-moving transaction that does not depend on its date: statements disagree on the
    date (order vs allotment date) but never on the running unit balance and the NAV applied.
    Rows without units/nav/balance (stamp duty, misc) have no such identity and return None."""
    if units is None or balance is None or nav is None:
        return None
    return (isin, kind, amount, units, nav, balance)


class Ledger:
    def __init__(self, db_path: str):
        self.conn: sqlite3.Connection = connect_rw(db_path)

    def close(self) -> None:
        self.conn.close()

    # ── statements ────────────────────────────────────────────────────────────

    def _dedupe_document(self, sha256: str, message_id: str, meta: dict | None):
        """(early result, meta, picked_up). A known, finished document is a duplicate; one that failed or awaited
        attention is cleared so the retry can import it, keeping its original pickup time and email details."""
        existing = self.conn.execute(
            "SELECT * FROM source_documents WHERE message_id=? AND sha256=?", (message_id, sha256)
        ).fetchone()
        meta = dict(meta or {})
        if existing and existing["status"] not in ("failed", "needs_attention"):
            self.fill_missing_meta(existing["id"], meta)
            return ImportResult(status="duplicate", doc_id=existing["id"]), meta, None
        picked_up = None
        if existing:                                     # earlier attempt failed / awaited attention: retry
            picked_up = existing["picked_up_at"]
            for f in META_FIELDS:
                if not meta.get(f) and existing[f]:
                    meta[f] = existing[f]
            with self.conn:
                self.conn.execute("DELETE FROM source_documents WHERE id=?", (existing["id"],))
                self.conn.execute(
                    "UPDATE review_queue SET status='resolved', resolved_at=datetime('now') "
                    "WHERE kind='needs_attention' AND ref=?", (f"doc:{existing['id']}",))
        return None, meta, picked_up

    def _insert_document(self, stmt, sha256: str, source: str, message_id: str, sender: str,
                         meta: dict, picked_up: str | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO source_documents (source, message_id, sha256, parser, status, period_from, period_to, "
            "sender, account, folder, uid, received_at, subject, filename, size, auth, picked_up_at, processed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, COALESCE(?, datetime('now')), datetime('now'))",
            (source, message_id, sha256, stmt.parser, "imported",
             stmt.period_from and stmt.period_from.isoformat(),
             stmt.period_to and stmt.period_to.isoformat(), sender,
             meta.get("account", ""), meta.get("folder", ""), meta.get("uid", 0), meta.get("received_at"),
             meta.get("subject", ""), meta.get("filename", ""), meta.get("size", 0), meta.get("auth", ""),
             picked_up),
        )
        return cur.lastrowid

    def import_statement(
        self, stmt: ParsedStatement | ParsedAccountStatement, *, sha256: str, source: str = "upload",
        message_id: str = "", sender: str = "", meta: dict | None = None, redactor=None,
    ) -> ImportResult:
        if isinstance(stmt, ParsedAccountStatement):
            return self.import_account_statement(stmt, sha256=sha256, source=source, message_id=message_id,
                                                 sender=sender, meta=meta, redactor=redactor)
        early, meta, picked_up = self._dedupe_document(sha256, message_id, meta)
        if early:
            return early

        result = ImportResult(status="imported", warnings=list(stmt.warnings))
        with self.conn:                                  # one transaction: all or nothing
            doc_id = result.doc_id = self._insert_document(stmt, sha256, source, message_id, sender, meta, picked_up)

            as_of = (stmt.period_to or max(
                (s.valuation_date for f in stmt.folios for s in f.schemes), default=None))
            present: set[int] = set()
            for folio in stmt.folios:
                acct_id, via_alias = self._upsert_account(
                    stmt.account_type, folio.amc, folio.folio, tuple(sc.isin for sc in folio.schemes),
                    {sc.isin: sc.close_units for sc in folio.schemes})
                present.add(acct_id)
                seen_isins: set[str] = set()
                for scheme in folio.schemes:
                    seen_isins.add(scheme.isin)
                    self._write_scheme(acct_id, doc_id, folio.folio, scheme, result, via_alias)
                if as_of:
                    self._close_missing(acct_id, doc_id, as_of.isoformat(), seen_isins)

            if stmt.kind == "summary":
                result.warnings.append(
                    "Summary statement: holdings and values were updated, but it carries no transactions, so XIRR "
                    "for these funds needs a detailed statement.")
                n_unknown = self.conn.execute(
                    "SELECT COUNT(DISTINCT account_id) FROM holdings WHERE doc_id=? AND account_id IN "
                    "(SELECT id FROM accounts WHERE institution=?)", (doc_id, PLACEHOLDER_AMC)).fetchone()[0]
                if n_unknown:
                    result.warnings.append(
                        f"{n_unknown} folio(s) in this summary could not be matched to a known folio; they are listed "
                        f"under '{PLACEHOLDER_AMC}' until a detailed statement identifies them.")
            if as_of and stmt.account_type == "mutual_fund":      # an NPS statement says nothing about fund folios
                result.warnings += self._flag_missing_folios(doc_id, present, as_of.isoformat())
            result.warnings += self.flag_duplicate_accounts()
            result.warnings += self.flag_duplicate_transactions()
            self.conn.execute(
                "UPDATE source_documents SET txn_new=?, txn_dup=? WHERE id=?",
                (result.txn_new, result.txn_dup, doc_id),
            )
        return result

    # ── bank / PPF / card statements ──────────────────────────────────────────

    def import_account_statement(
        self, stmt: ParsedAccountStatement, *, sha256: str, source: str = "upload", message_id: str = "",
        sender: str = "", meta: dict | None = None, redactor=None,
    ) -> ImportResult:
        """Idempotent. A statement whose opening balance plus movements does not equal its closing balance
        is refused (ValueError) before anything is written. `redactor` masks free text (the caller knows the
        user's identifiers; this module never reads secrets)."""
        for acct in stmt.accounts:
            errs = acct.integrity_errors()
            if errs:
                raise ValueError(f"{acct.label or acct.type} account …{last4(acct.identifier)}: {errs[0]}")
        early, meta, picked_up = self._dedupe_document(sha256, message_id, meta)
        if early:
            return early

        result = ImportResult(status="imported", warnings=list(stmt.warnings))
        with self.conn:
            doc_id = result.doc_id = self._insert_document(stmt, sha256, source, message_id, sender, meta, picked_up)
            ids: dict[str, int] = {}
            for acct in stmt.accounts:
                acct_id, _ = self._upsert_account(
                    acct.type, acct.institution, acct.identifier, label=acct.label,
                    liability=1 if acct.type == "card" else 0)
                ids[acct.identifier] = acct_id
                self._write_account_txns(acct_id, doc_id, acct, result, redactor)
                if acct.period_to:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO account_balances (account_id, as_of, balance, doc_id) VALUES (?,?,?,?)",
                        (acct_id, acct.period_to.isoformat(), dec_str(acct.closing_balance), doc_id))
                if acct.statement_date:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO statement_summaries (account_id, statement_date, period_from, period_to, "
                        "total_due, min_due, due_date, credit_limit, doc_id, amount_due) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (acct_id, acct.statement_date.isoformat(), acct.period_from and acct.period_from.isoformat(),
                         acct.period_to and acct.period_to.isoformat(), dec_str(-acct.closing_balance),
                         dec_str(acct.min_due), acct.due_date and acct.due_date.isoformat(),
                         dec_str(acct.credit_limit), doc_id,
                         dec_str(acct.amount_due if acct.amount_due is not None else -acct.closing_balance)))
            for dep in stmt.deposits:
                self._write_deposit(dep, ids.get(dep.linked_account), doc_id)
            if stmt.fd_complete and stmt.as_of:                      # a statement that states an account's linked deposits, and was read in full
                for ident in stmt.fd_summary:
                    if ident in ids:
                        listed = {keyed_hash("fd", d.institution.lower(), d.number) for d in stmt.deposits if d.linked_account == ident}
                        self._close_unlisted_deposits(ids[ident], listed, stmt.as_of, doc_id)
            self.conn.execute("UPDATE source_documents SET txn_new=?, txn_dup=? WHERE id=?",
                              (result.txn_new, result.txn_dup, doc_id))
        self.merge_same_account_spellings()
        from finance.reconcile import match_transfers              # Reconciliation Agent; writes only through this Ledger
        result.warnings += match_transfers(self, doc_id=doc_id)    # after the import committed: it commits on its own
        return result

    def _write_account_txns(self, acct_id: int, doc_id: int, acct: ParsedAccount, result: ImportResult,
                            redactor) -> None:
        ref = self.conn.execute("SELECT ref_hash FROM accounts WHERE id=?", (acct_id,)).fetchone()["ref_hash"]
        occurrences: Counter = Counter()
        for t in acct.txns:
            # The running balance makes a row unique even when amount and date repeat; the occurrence index
            # covers rows (cards, which print no balance) that are identical in every field.
            fields = (ref, t.date.isoformat(), dec_str(t.amount), dec_str(t.balance))
            n = occurrences[fields]
            occurrences[fields] += 1
            key = keyed_hash("acct_txn", *fields, n)
            desc = (redactor(t.description) if redactor else t.description)[:300]
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO transactions "
                "(account_id, doc_id, date, kind, description, amount, balance, reference, idem_key) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (acct_id, doc_id, t.date.isoformat(), t.kind, desc, dec_str(t.amount), dec_str(t.balance),
                 t.reference, key))
            if cur.rowcount:
                result.txn_new += 1
            else:
                result.txn_dup += 1

    def _write_deposit(self, dep: ParsedDeposit, linked_id: int | None, doc_id: int) -> None:
        ref = keyed_hash("fd", dep.institution.lower(), dep.number)
        self.conn.execute(
            "INSERT OR REPLACE INTO fixed_deposits (institution, ref_hash, mask, linked_account_id, open_date, "
            "principal, rate, tenure, maturity_date, maturity_amount, current_balance, status, as_of, doc_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (dep.institution, ref, last4(dep.number), linked_id,
             dep.open_date and dep.open_date.isoformat(), dec_str(dep.principal), dec_str(dep.rate), dep.tenure,
             dep.maturity_date and dep.maturity_date.isoformat(), dec_str(dep.maturity_amount),
             dec_str(dep.current_balance), dep.status, dep.as_of.isoformat(), doc_id))

    def _close_unlisted_deposits(self, account_id: int, listed: set[str], as_of: date, doc_id: int) -> None:
        """A deposit this account had at its last statement and that a newer statement no longer lists has been closed or
        has matured and been paid out: record that as a closing snapshot, so it stops counting towards net worth. Older
        statements never close anything, and the deposit's history stays."""
        rows = self.conn.execute(
            "SELECT * FROM fixed_deposits f WHERE f.linked_account_id=? AND f.status != 'Closed' AND f.as_of < ? "
            "AND f.as_of = (SELECT MAX(as_of) FROM fixed_deposits x WHERE x.ref_hash = f.ref_hash)",
            (account_id, as_of.isoformat())).fetchall()
        for r in rows:
            if r["ref_hash"] not in listed:
                self.conn.execute(
                    "INSERT OR IGNORE INTO fixed_deposits (institution, ref_hash, mask, linked_account_id, open_date, principal, rate, "
                    "tenure, maturity_date, maturity_amount, current_balance, status, as_of, doc_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["institution"], r["ref_hash"], r["mask"], r["linked_account_id"], r["open_date"], r["principal"], r["rate"],
                     r["tenure"], r["maturity_date"], r["maturity_amount"], "0", "Closed", as_of.isoformat(), doc_id))

    def link_transfer(self, txn_out: int, txn_in: int, method: str, confidence: str) -> bool:
        """Record that two transactions are the two legs of one transfer (not an expense, not income)."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO transfer_links (txn_out, txn_in, method, confidence) VALUES (?,?,?,?)",
                (txn_out, txn_in, method, confidence))
        return bool(cur.rowcount)

    # ── folios missing from a newer statement ─────────────────────────────────

    def _flag_missing_folios(self, doc_id: int, present: set[int], as_of: str) -> list[str]:
        """A folio we hold units in that a *newer* statement omits is ambiguous: sold, or just
        not covered by this statement. Never guess: queue it for the user to decide."""
        for acct_id in present:                           # folio is back: any open question is moot
            self.conn.execute(
                "UPDATE review_queue SET status='resolved', resolved_at=datetime('now') "
                "WHERE kind='folio_missing' AND status='open' AND ref LIKE ?",
                (f"folio_missing:{acct_id}:%",),
            )
        rows = self.conn.execute(
            "SELECT h.account_id, a.institution, a.mask, COUNT(*) n, MAX(h.as_of) last_asof "
            "FROM holdings h JOIN accounts a ON a.id = h.account_id "
            "WHERE a.type='mutual_fund' AND CAST(h.units AS REAL) > 0 "
            "AND h.as_of = (SELECT MAX(as_of) FROM holdings x WHERE x.account_id=h.account_id AND x.isin=h.isin) "
            "GROUP BY h.account_id"
        ).fetchall()
        warnings: list[str] = []
        for r in rows:
            if r["account_id"] in present or r["last_asof"] >= as_of:
                continue
            prefix = f"folio_missing:{r['account_id']}:{as_of}:"
            if self.conn.execute("SELECT 1 FROM review_queue WHERE kind='folio_missing' AND ref LIKE ?",
                                 (prefix + "%",)).fetchone():
                continue
            msg = (f"{r['institution']} folio …{r['mask']} ({r['n']} fund(s)) is missing from the statement "
                   f"dated {as_of}. Sold, or just not covered by this statement?")
            self.conn.execute("INSERT INTO review_queue (kind, ref, reason) VALUES ('folio_missing', ?, ?)",
                              (f"{prefix}{doc_id}", msg))
            warnings.append(msg)
        return warnings

    def resolve_folio_missing(self, item_id: int, close: bool) -> None:
        """close=True: treat the folio as fully redeemed as of the statement date. close=False: keep it."""
        with self.conn:
            item = self.conn.execute(
                "SELECT * FROM review_queue WHERE id=? AND kind='folio_missing' AND status='open'",
                (item_id,)).fetchone()
            if not item:
                raise ValueError("no such open folio review item")
            _, acct, as_of, doc = item["ref"].split(":")
            if close:
                for h in self.conn.execute(
                    "SELECT isin, scheme_name, amfi_code FROM holdings h WHERE account_id=? "
                    "AND CAST(units AS REAL) > 0 AND as_of = (SELECT MAX(as_of) FROM holdings x "
                    "WHERE x.account_id=h.account_id AND x.isin=h.isin)", (int(acct),)
                ).fetchall():
                    self.conn.execute(
                        "INSERT OR IGNORE INTO holdings (account_id, isin, scheme_name, amfi_code, as_of, units, nav, cost, value, doc_id) "
                        "VALUES (?,?,?,?,?,'0','0',NULL,'0',?)",
                        (int(acct), h["isin"], h["scheme_name"], h["amfi_code"], as_of, int(doc)),
                    )
            self.conn.execute(
                "UPDATE review_queue SET status='resolved', resolved_at=datetime('now'), reason=reason||? WHERE id=?",
                (" → marked closed" if close else " → kept as held", item_id),
            )

    def _upsert_account(self, type_: str, institution: str, identifier: str,
                        isins: tuple = (), units: dict | None = None, *, label: str = "",
                        liability: int = 0) -> tuple[int, bool]:
        """(account id, reached through an alias). An alias is a spelling of the folio that a merge or match
        taught us belongs to an existing account. Summary statements do not name the fund house, so for
        those the folio is matched against known accounts instead of creating a new one."""
        inst_key = institution.strip().lower()
        if inst_key in UNKNOWN_AMC:
            return self._account_for_unknown_amc(type_, identifier, isins, units or {})
        ref = keyed_hash(type_, inst_key, identifier)
        row = self.conn.execute("SELECT id FROM accounts WHERE type=? AND ref_hash=?", (type_, ref)).fetchone()
        if row:
            return row["id"], False
        alias = self.conn.execute("SELECT account_id FROM account_aliases WHERE ref_hash=?", (ref,)).fetchone()
        if alias:
            return alias["account_id"], True
        adopted = self._adopt_placeholder(type_, institution, identifier, isins, ref)
        if adopted:
            return adopted, False
        mask = last4(identifier)
        if type_ in SAME_MASK_TYPES and mask:                    # a monthly email masks the number, a requested statement prints it whole
            same = self.conn.execute("SELECT id FROM accounts WHERE type=? AND lower(institution)=? AND mask=? ORDER BY id",
                                     (type_, inst_key, mask)).fetchall()
            if len(same) == 1:
                self.conn.execute("INSERT OR REPLACE INTO account_aliases (ref_hash, account_id) VALUES (?,?)", (ref, same[0]["id"]))
                return same[0]["id"], False
        name = f"{institution} {label} …{mask}" if label else f"{institution} …{mask}"
        cur = self.conn.execute(
            "INSERT INTO accounts (type, institution, name, mask, ref_hash, liability) VALUES (?,?,?,?,?,?)",
            (type_, institution, name, mask, ref, liability),
        )
        return cur.lastrowid, False

    @staticmethod
    def _mask_variants(identifier: str) -> set[str]:
        """Last-4 of the folio as written, and of its core before a '/ check-digit' suffix."""
        return {last4(identifier), last4(identifier.split("/")[0])} - {""}

    def _holding_isins(self, acct_id: int) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT DISTINCT isin FROM holdings WHERE account_id=?", (acct_id,))}

    def _account_for_unknown_amc(self, type_: str, identifier: str, isins: tuple, units: dict) -> tuple[int, bool]:
        names = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT institution FROM accounts WHERE type=? AND institution != ?", (type_, PLACEHOLDER_AMC))]
        # 1. the folio exactly as an earlier statement (or a merge alias) spelled it, under any known fund house
        hits: dict[int, bool] = {}
        for name in names:
            ref = keyed_hash(type_, name.lower(), identifier)
            r = self.conn.execute("SELECT id FROM accounts WHERE type=? AND ref_hash=?", (type_, ref)).fetchone()
            if r:
                hits[r["id"]] = False
            a = self.conn.execute("SELECT account_id FROM account_aliases WHERE ref_hash=?", (ref,)).fetchone()
            if a:
                hits.setdefault(a["account_id"], True)
        placeholder_ref = keyed_hash(type_, "n/a", identifier)
        if not hits:
            a = self.conn.execute("SELECT account_id FROM account_aliases WHERE ref_hash=?", (placeholder_ref,)).fetchone()
            if a:
                hits[a["account_id"]] = True
        if len(hits) > 1:                                    # same folio number at two fund houses: use the funds held
            narrowed = {i: v for i, v in hits.items() if self._holding_isins(i) & set(isins)}
            hits = narrowed or hits
        if len(hits) == 1:
            acct, via = next(iter(hits.items()))
            return acct, via

        # 2. a known account that holds the same fund and whose folio ends the same way
        masks = self._mask_variants(identifier)
        cands = [r["id"] for r in self.conn.execute(
            "SELECT id, mask FROM accounts WHERE type=? AND institution != ?", (type_, PLACEHOLDER_AMC))
            if r["mask"] in masks and self._holding_isins(r["id"]) & set(isins)]
        # 3. otherwise, one account holding the same funds with exactly the same units
        if not cands and units:
            for r in self.conn.execute("SELECT id FROM accounts WHERE type=? AND institution != ?",
                                       (type_, PLACEHOLDER_AMC)).fetchall():
                latest = {h["isin"]: Decimal(h["units"]) for h in self.conn.execute(
                    "SELECT isin, units FROM holdings h WHERE account_id=? AND as_of=(SELECT MAX(as_of) FROM holdings x "
                    "WHERE x.account_id=h.account_id AND x.isin=h.isin)", (r["id"],))}
                common = [i for i in units if i in latest]
                if common and all(abs(latest[i] - units[i]) <= UNITS_TOLERANCE for i in common) \
                        and any(latest[i] > 0 for i in common):
                    cands.append(r["id"])
        if len(cands) == 1:
            self.conn.execute("INSERT OR IGNORE INTO account_aliases (ref_hash, account_id) VALUES (?,?)",
                              (placeholder_ref, cands[0]))
            return cands[0], True

        # 4. unknown folio: keep it under a placeholder until a detailed statement names the fund house
        row = self.conn.execute("SELECT id FROM accounts WHERE type=? AND ref_hash=?", (type_, placeholder_ref)).fetchone()
        if row:
            return row["id"], False
        mask = last4(identifier)
        cur = self.conn.execute(
            "INSERT INTO accounts (type, institution, name, mask, ref_hash, liability) VALUES (?,?,?,?,?,0)",
            (type_, PLACEHOLDER_AMC, f"{PLACEHOLDER_AMC} …{mask}", mask, placeholder_ref))
        return cur.lastrowid, False

    def _adopt_placeholder(self, type_: str, institution: str, identifier: str, isins: tuple, real_ref: str) -> int | None:
        """A detailed statement names the fund house of a folio first seen in a summary: rename, don't duplicate."""
        placeholder_ref = keyed_hash(type_, "n/a", identifier)
        row = self.conn.execute("SELECT id FROM accounts WHERE type=? AND ref_hash=?", (type_, placeholder_ref)).fetchone()
        ids = [row["id"]] if row else []
        if not ids:
            masks = self._mask_variants(identifier)
            ids = [r["id"] for r in self.conn.execute(
                "SELECT id, mask FROM accounts WHERE type=? AND institution=?", (type_, PLACEHOLDER_AMC))
                if r["mask"] in masks and self._holding_isins(r["id"]) & set(isins)]
        if len(ids) != 1:
            return None
        acct = ids[0]
        self.conn.execute("UPDATE accounts SET institution=?, name=?, ref_hash=? WHERE id=?",
                          (institution, f"{institution} …{last4(identifier)}", real_ref, acct))
        self.conn.execute("INSERT OR IGNORE INTO account_aliases (ref_hash, account_id) VALUES (?,?)",
                          (placeholder_ref, acct))
        return acct

    def _write_scheme(self, acct_id: int, doc_id: int, folio: str, scheme: ParsedScheme,
                      result: ImportResult, via_alias: bool = False) -> None:
        # Identical fields on the same date (e.g. two stamp-duty rows) get an occurrence index,
        # so re-importing an overlapping statement yields the same keys.
        occurrences: Counter = Counter()
        strong_seen: Counter = Counter()
        for t in scheme.txns:
            fields = (folio, scheme.isin, t.date.isoformat(), t.kind,
                      dec_str(t.amount), dec_str(t.units), dec_str(t.balance))
            n = occurrences[fields]
            occurrences[fields] += 1
            key = keyed_hash("txn", *fields, n)
            sk = strong_key(scheme.isin, t.kind, dec_str(t.amount), dec_str(t.units), dec_str(t.nav), dec_str(t.balance))
            n_strong = strong_seen[sk] if sk else 0
            if sk:
                strong_seen[sk] += 1
            if self._known_duplicate(acct_id, key, scheme.isin, t, n, via_alias, sk, n_strong):
                result.txn_dup += 1
                continue
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO transactions "
                "(account_id, doc_id, isin, scheme_name, date, kind, description, amount, units, nav, balance, idem_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (acct_id, doc_id, scheme.isin, scheme.scheme_name, t.date.isoformat(), t.kind,
                 t.description, dec_str(t.amount), dec_str(t.units), dec_str(t.nav),
                 dec_str(t.balance), key),
            )
            if cur.rowcount:
                result.txn_new += 1
            else:
                result.txn_dup += 1

        as_of = scheme.valuation_date.isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO holdings "
            "(account_id, isin, scheme_name, amfi_code, as_of, units, nav, cost, value, doc_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (acct_id, scheme.isin, scheme.scheme_name, scheme.amfi_code, as_of,
             dec_str(scheme.close_units), dec_str(scheme.nav), dec_str(scheme.cost),
             dec_str(scheme.value), doc_id),
        )
        result.holdings += 1
        self.conn.execute(
            "INSERT OR IGNORE INTO prices (isin, date, nav) VALUES (?,?,?)",
            (scheme.isin, as_of, dec_str(scheme.nav)),
        )

        # Balance check: the statement's own closing units vs. its transaction running total.
        if scheme.close_calculated is not None and \
                abs(scheme.close_units - scheme.close_calculated) > UNITS_TOLERANCE:
            msg = (f"{scheme.scheme_name}: closing units {scheme.close_units} differ from "
                   f"transaction total {scheme.close_calculated}")
            result.warnings.append(msg)
            self.conn.execute(
                "INSERT INTO review_queue (kind, ref, reason) VALUES ('balance_mismatch', ?, ?)",
                (f"doc:{doc_id}", msg),
            )

    def _known_duplicate(self, acct_id: int, key: str, isin: str, t, n: int, via_alias: bool,
                         sk: tuple | None, n_strong: int) -> bool:
        """True if this transaction is already stored: under a removed duplicate's key, under another date
        (strong identity), or under another spelling of the folio (weak rows, exact date)."""
        if self.conn.execute("SELECT 1 FROM txn_ignored WHERE idem_key=?", (key,)).fetchone():
            return True
        if self.conn.execute("SELECT 1 FROM transactions WHERE idem_key=?", (key,)).fetchone():
            return False                                  # exact repeat of an earlier import: INSERT OR IGNORE counts it
        if sk:
            have = self.conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE account_id=? AND isin=? AND kind=? AND amount IS ? "
                "AND units IS ? AND nav IS ? AND balance IS ?", (acct_id, *sk)).fetchone()[0]
        elif via_alias:
            have = self.conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE account_id=? AND isin=? AND date=? AND kind=? "
                "AND amount IS ? AND units IS ? AND balance IS ?",
                (acct_id, isin, t.date.isoformat(), t.kind, dec_str(t.amount), dec_str(t.units),
                 dec_str(t.balance))).fetchone()[0]
            n_strong = n
        else:
            return False
        if have > n_strong:
            self.conn.execute("INSERT OR IGNORE INTO txn_ignored (idem_key) VALUES (?)", (key,))
            return True
        return False

    def _close_missing(self, acct_id: int, doc_id: int, as_of: str, seen: set[str]) -> None:
        """A newer statement that lists this folio but omits a scheme means it was fully redeemed."""
        rows = self.conn.execute(
            "SELECT h.isin, h.scheme_name, h.amfi_code FROM holdings h "
            "WHERE h.account_id=? AND h.as_of < ? AND CAST(h.units AS REAL) > 0 "
            "AND h.as_of = (SELECT MAX(as_of) FROM holdings x WHERE x.account_id=h.account_id AND x.isin=h.isin)",
            (acct_id, as_of),
        ).fetchall()
        for r in rows:
            if r["isin"] not in seen:
                self.conn.execute(
                    "INSERT OR IGNORE INTO holdings (account_id, isin, scheme_name, amfi_code, as_of, units, nav, cost, value, doc_id) "
                    "VALUES (?,?,?,?,?,'0','0',NULL,'0',?)",
                    (acct_id, r["isin"], r["scheme_name"], r["amfi_code"], as_of, doc_id),
                )

    # ── review queue ──────────────────────────────────────────────────────────

    def resolve_review(self, item_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE review_queue SET status='resolved', resolved_at=datetime('now') WHERE id=?",
                (item_id,),
            )

    def record_failure(self, *, sha256: str, parser: str, error: str, source: str = "upload",
                       message_id: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO source_documents (source, message_id, sha256, parser, status, error) "
                "VALUES (?,?,?,?, 'failed', ?)",
                (source, message_id, sha256, parser, error[:500]),
            )

    # ── needs-attention queue (documents we could not process) ────────────────

    def record_attention(self, *, sha256: str, message_id: str, sender: str, parser: str,
                         reason: str, source: str = "imap", meta: dict | None = None) -> int:
        """Remember a document that needs the user (locked PDF, unparseable layout...)."""
        reason = reason[:400]
        with self.conn:
            row = self.conn.execute(
                "SELECT id FROM source_documents WHERE message_id=? AND sha256=?", (message_id, sha256)).fetchone()
            meta = meta or {}
            if row:
                doc_id = row["id"]
                self.conn.execute("UPDATE source_documents SET status='needs_attention', error=?, parser=?, "
                                  "processed_at=datetime('now') WHERE id=?", (reason, parser, doc_id))
                self.fill_missing_meta(doc_id, meta)
            else:
                doc_id = self.conn.execute(
                    "INSERT INTO source_documents (source, message_id, sha256, parser, status, error, sender, account, "
                    "folder, uid, received_at, subject, filename, size, auth, picked_up_at, processed_at) "
                    "VALUES (?,?,?,?, 'needs_attention', ?,?,?,?,?,?,?,?,?,?, datetime('now'), datetime('now'))",
                    (source, message_id, sha256, parser, reason, sender, meta.get("account", ""), meta.get("folder", ""),
                     meta.get("uid", 0), meta.get("received_at"), meta.get("subject", ""), meta.get("filename", ""),
                     meta.get("size", 0), meta.get("auth", ""))).lastrowid
            open_item = self.conn.execute(
                "SELECT id FROM review_queue WHERE kind='needs_attention' AND ref=? AND status='open'",
                (f"doc:{doc_id}",)).fetchone()
            if open_item:
                self.conn.execute("UPDATE review_queue SET reason=? WHERE id=?", (reason, open_item["id"]))
            else:
                self.conn.execute("INSERT INTO review_queue (kind, ref, reason) VALUES ('needs_attention', ?, ?)",
                                  (f"doc:{doc_id}", reason))
        return doc_id

    def dismiss_attention(self, doc_id: int) -> None:
        """User chose to ignore this document; it will not be re-flagged or re-processed."""
        with self.conn:
            self.conn.execute("UPDATE source_documents SET status='dismissed' WHERE id=? AND status='needs_attention'",
                              (doc_id,))
            self.conn.execute(
                "UPDATE review_queue SET status='resolved', resolved_at=datetime('now'), reason=reason||' → dismissed' "
                "WHERE kind='needs_attention' AND ref=? AND status='open'", (f"doc:{doc_id}",))

    def add_review(self, kind: str, ref: str, reason: str) -> None:
        with self.conn:
            if not self.conn.execute("SELECT 1 FROM review_queue WHERE kind=? AND ref=?", (kind, ref)).fetchone():
                self.conn.execute("INSERT INTO review_queue (kind, ref, reason) VALUES (?,?,?)",
                                  (kind, ref, reason[:400]))

    def fill_missing_meta(self, doc_id: int, meta: dict) -> None:
        """Back-fill email details on a document first stored before they were recorded."""
        row = self.conn.execute("SELECT * FROM source_documents WHERE id=?", (doc_id,)).fetchone()
        updates = {f: meta[f] for f in META_FIELDS if meta.get(f) and not row[f]}
        if updates:
            with self.conn:
                self.conn.execute(
                    f"UPDATE source_documents SET {', '.join(f'{k}=?' for k in updates)} WHERE id=?",
                    (*updates.values(), doc_id))

    def reset_imap_state(self, account: str) -> None:
        """Forget where a mailbox was up to, so the next sync re-scans (safe: imports are idempotent)."""
        with self.conn:
            self.conn.execute("DELETE FROM imap_state WHERE account=?", (account,))

    def document(self, doc_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM source_documents WHERE id=?", (doc_id,)).fetchone()

    def known_document(self, message_id: str, sha256: str) -> str | None:
        row = self.conn.execute("SELECT status FROM source_documents WHERE message_id=? AND sha256=?",
                                (message_id, sha256)).fetchone()
        return row["status"] if row else None

    # ── mailbox sync state ────────────────────────────────────────────────────

    def get_imap_state(self, account: str, folder: str) -> tuple[int, int] | None:
        r = self.conn.execute("SELECT uidvalidity, last_uid FROM imap_state WHERE account=? AND folder=?",
                              (account, folder)).fetchone()
        return (r["uidvalidity"], r["last_uid"]) if r else None

    def set_imap_state(self, account: str, folder: str, uidvalidity: int, last_uid: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO imap_state (account, folder, uidvalidity, last_uid, last_sync) "
                "VALUES (?,?,?,?,datetime('now')) ON CONFLICT(account, folder) DO UPDATE SET "
                "uidvalidity=excluded.uidvalidity, last_uid=excluded.last_uid, last_sync=excluded.last_sync",
                (account, folder, uidvalidity, last_uid))

    # ── working password template per sender ──────────────────────────────────

    def card_last4s(self) -> list[str]:
        """Last 4 digits of the cards already in the ledger (for {card4} password templates)."""
        return [r["mask"] for r in self.conn.execute("SELECT DISTINCT mask FROM accounts WHERE type='card'") if r["mask"].isdigit() and len(r["mask"]) == 4]

    def password_templates(self) -> dict[str, str]:
        return {r["sender"]: r["template"] for r in self.conn.execute("SELECT sender, template FROM parser_cache")}

    def cache_template(self, sender: str, template: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO parser_cache (sender, template) VALUES (?,?) "
                              "ON CONFLICT(sender) DO UPDATE SET template=excluded.template", (sender, template))

    # ── duplicate folios (the same folio written differently by different statements) ──

    def flag_duplicate_accounts(self) -> list[str]:
        """Queue pairs of same-AMC accounts that share identical transactions (same fund, type, amount, units
        and running unit balance; dates may differ between statements). Never merges by itself."""
        pairs = self.conn.execute(
            "SELECT a.account_id pa, b.account_id pb, x.institution inst, x.mask ma, y.mask mb, "
            "COUNT(DISTINCT a.id) overlap "
            "FROM transactions a JOIN transactions b ON a.isin=b.isin AND a.kind=b.kind AND a.amount=b.amount "
            "AND a.units=b.units AND a.nav=b.nav AND a.balance=b.balance AND a.account_id < b.account_id "
            "JOIN accounts x ON x.id=a.account_id JOIN accounts y ON y.id=b.account_id "
            "WHERE x.type='mutual_fund' AND y.type='mutual_fund' AND lower(x.institution)=lower(y.institution) "
            "GROUP BY a.account_id, b.account_id").fetchall()
        counts = {r[0]: r[1] for r in self.conn.execute("SELECT account_id, COUNT(*) FROM transactions GROUP BY account_id")}
        out = []
        for p in pairs:
            smaller = min(counts.get(p["pa"], 0), counts.get(p["pb"], 0))
            if p["overlap"] < max(1, smaller // 2):
                continue
            ref = f"dup:{p['pa']}:{p['pb']}"
            if self.conn.execute("SELECT 1 FROM review_queue WHERE kind='duplicate_folio' AND ref=?", (ref,)).fetchone():
                continue
            msg = (f"{p['inst']} folios …{p['ma']} and …{p['mb']} share {p['overlap']} identical transactions "
                   f"({counts.get(p['pa'], 0)} vs {counts.get(p['pb'], 0)} recorded). Probably one folio written "
                   f"two ways by different statements; until merged, holdings and XIRR are double counted.")
            with self.conn:
                self.conn.execute("INSERT INTO review_queue (kind, ref, reason) VALUES ('duplicate_folio', ?, ?)", (ref, msg))
            out.append(msg)

        # Folios first seen in a summary statement (fund house unknown) that look like a known folio.
        for ph in self.conn.execute("SELECT id, mask FROM accounts WHERE type='mutual_fund' AND institution=?",
                                    (PLACEHOLDER_AMC,)).fetchall():
            held = self._holding_isins(ph["id"])
            reals = [r for r in self.conn.execute(
                "SELECT id, institution, mask FROM accounts WHERE type='mutual_fund' AND institution != ?",
                (PLACEHOLDER_AMC,)) if self._holding_isins(r["id"]) & held]
            likely = [r for r in reals if r["mask"] == ph["mask"]] or (reals if len(reals) == 1 else [])
            for r in likely:
                ref = f"dup:{r['id']}:{ph['id']}"
                if self.conn.execute("SELECT 1 FROM review_queue WHERE kind='duplicate_folio' AND ref=?", (ref,)).fetchone():
                    continue
                msg = (f"A folio from a summary statement (fund house unknown, …{ph['mask']}) holds the same fund(s) as "
                       f"{r['institution']} …{r['mask']}. Probably the same folio; until merged, holdings are double counted.")
                with self.conn:
                    self.conn.execute("INSERT INTO review_queue (kind, ref, reason) VALUES ('duplicate_folio', ?, ?)", (ref, msg))
                out.append(msg)
        return out

    def backup(self, directory: str | None = None) -> str:
        """A copy of the ledger before a destructive change. A vaulted ledger is copied sealed, so a backup is as private as the
        ledger; `directory` applies only to plain files."""
        if hasattr(self.conn, "_vault"):
            return str(self.conn._vault.snapshot())
        Path(directory).mkdir(parents=True, exist_ok=True)
        dest = str(Path(directory) / f"finance-{time.strftime('%Y%m%d-%H%M%S')}.db")
        self.conn.execute("VACUUM INTO ?", (dest,))
        Path(dest).chmod(0o600)
        return dest

    def merge_accounts(self, primary: int, dup: int) -> dict:
        """Fold folio `dup` into `primary`: move holdings/transactions, drop transactions the primary already
        has, and remember dup's spelling as an alias so future imports land on `primary`."""
        a = self.conn.execute("SELECT * FROM accounts WHERE id=?", (primary,)).fetchone()
        b = self.conn.execute("SELECT * FROM accounts WHERE id=?", (dup,)).fetchone()
        if not a or not b or primary == dup or a["type"] != b["type"] or (
                a["institution"].lower() != b["institution"].lower() and PLACEHOLDER_AMC not in (a["institution"], b["institution"])):
            raise ValueError("accounts cannot be merged")
        moved = dropped = 0
        with self.conn:
            if a["institution"] == PLACEHOLDER_AMC and b["institution"] != PLACEHOLDER_AMC:
                self.conn.execute("UPDATE accounts SET institution=?, name=?, ref_hash=? WHERE id=?",
                                  (b["institution"], b["name"], b["ref_hash"], primary))
                self.conn.execute("INSERT OR REPLACE INTO account_aliases (ref_hash, account_id) VALUES (?,?)",
                                  (a["ref_hash"], primary))
            self.conn.execute("INSERT OR REPLACE INTO account_aliases (ref_hash, account_id) VALUES (?,?)",
                              (b["ref_hash"], primary))
            self.conn.execute("UPDATE account_aliases SET account_id=? WHERE account_id=?", (primary, dup))

            # Identity across spellings: see strong_key (no date); rows without units/balance match on exact date.
            sem = lambda r: (strong_key(r["isin"], r["kind"], r["amount"], r["units"], r["nav"], r["balance"])
                             or ("weak", r["isin"], r["date"], r["kind"], r["amount"]))
            have = Counter(sem(r) for r in self.conn.execute("SELECT * FROM transactions WHERE account_id=?", (primary,)))
            rekeyed: Counter = Counter()                    # rows already under the primary with the same (date, amount, balance)
            for r in self.conn.execute("SELECT date, amount, balance FROM transactions WHERE account_id=?", (primary,)):
                rekeyed[(a["ref_hash"], r["date"], r["amount"], r["balance"])] += 1
            for t in self.conn.execute("SELECT * FROM transactions WHERE account_id=? ORDER BY id", (dup,)).fetchall():
                if have[sem(t)] > 0:
                    have[sem(t)] -= 1
                    self.conn.execute("INSERT OR IGNORE INTO txn_ignored (idem_key) VALUES (?)", (t["idem_key"],))
                    self.conn.execute("DELETE FROM transfer_links WHERE txn_out=? OR txn_in=?", (t["id"], t["id"]))
                    self.conn.execute("DELETE FROM transactions WHERE id=?", (t["id"],))
                    dropped += 1
                else:
                    self.conn.execute("UPDATE transactions SET account_id=? WHERE id=?", (primary, t["id"]))
                    moved += 1
                    if a["type"] in SAME_MASK_TYPES:                 # later statements key rows by the primary's hash
                        f = (a["ref_hash"], t["date"], t["amount"], t["balance"])
                        self.conn.execute("UPDATE transactions SET idem_key=? WHERE id=?",
                                          (keyed_hash("acct_txn", *f, rekeyed[f]), t["id"]))
                        rekeyed[f] += 1

            for h in self.conn.execute("SELECT isin, as_of FROM holdings WHERE account_id=?", (dup,)).fetchall():
                clash = self.conn.execute("SELECT 1 FROM holdings WHERE account_id=? AND isin=? AND as_of=?",
                                          (primary, h["isin"], h["as_of"])).fetchone()
                if clash:
                    self.conn.execute("DELETE FROM holdings WHERE account_id=? AND isin=? AND as_of=?",
                                      (dup, h["isin"], h["as_of"]))
                else:
                    self.conn.execute("UPDATE holdings SET account_id=? WHERE account_id=? AND isin=? AND as_of=?",
                                      (primary, dup, h["isin"], h["as_of"]))

            for table, key in (("account_balances", "as_of"), ("statement_summaries", "statement_date")):
                self.conn.execute(f"UPDATE OR IGNORE {table} SET account_id=? WHERE account_id=?", (primary, dup))
                self.conn.execute(f"DELETE FROM {table} WHERE account_id=?", (dup,))
            self.conn.execute("UPDATE fixed_deposits SET linked_account_id=? WHERE linked_account_id=?", (primary, dup))
            self.conn.execute("DELETE FROM accounts WHERE id=?", (dup,))
            for like in (f"folio_missing:{dup}:%", f"folio_missing:{primary}:%", f"dup:{primary}:{dup}",
                         f"dup:%:{dup}", f"dup:{dup}:%"):
                self.conn.execute(
                    "UPDATE review_queue SET status='resolved', resolved_at=datetime('now') "
                    "WHERE status='open' AND kind IN ('folio_missing','duplicate_folio') AND ref LIKE ?", (like,))
        return {"moved": moved, "dropped": dropped}

    def merge_same_account_spellings(self) -> int:
        """Merge bank/PPF accounts of one institution that share their last 4 (the same account read from a masked
        and an unmasked statement). The older account wins; returns how many were merged."""
        groups: dict[tuple, list[int]] = {}
        for r in self.conn.execute("SELECT id, type, lower(institution) AS inst, mask FROM accounts "
                                   "WHERE type IN (%s) AND mask != '' ORDER BY id" % ",".join("?" * len(SAME_MASK_TYPES)),
                                   tuple(SAME_MASK_TYPES)):
            groups.setdefault((r["type"], r["inst"], r["mask"]), []).append(r["id"])
        merged = 0
        for ids in groups.values():
            for dup in ids[1:]:
                self.merge_accounts(ids[0], dup)
                merged += 1
        if merged:
            from finance.reconcile import match_transfers
            match_transfers(self)
        return merged

    def merge_all_duplicates(self) -> dict:
        """Merge every open duplicate_folio pair (older account wins). Pairs that no longer apply are skipped."""
        merged = moved = dropped = skipped = 0
        for item in self.conn.execute(
                "SELECT ref FROM review_queue WHERE kind='duplicate_folio' AND status='open' ORDER BY id").fetchall():
            _, pa, pb = item["ref"].split(":")
            try:
                r = self.merge_accounts(int(pa), int(pb))
                merged, moved, dropped = merged + 1, moved + r["moved"], dropped + r["dropped"]
            except ValueError:
                skipped += 1                       # an earlier merge already absorbed one side
        return {"merged": merged, "moved": moved, "dropped": dropped, "skipped": skipped}

    def keep_separate(self, item_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE review_queue SET status='resolved', resolved_at=datetime('now'), "
                              "reason=reason||' → kept as separate folios' WHERE id=? AND kind='duplicate_folio' "
                              "AND status='open'", (item_id,))

    # ── duplicate transactions already stored (same transaction, different date, different statements) ──

    def _duplicate_txn_ids(self) -> list[tuple[int, str]]:
        """Rows to delete: within one account, identical strong identity coming from DIFFERENT documents.
        Rows from the same document are genuine repeats and are never touched."""
        groups: dict[tuple, list] = {}
        for t in self.conn.execute(
            "SELECT id, account_id, doc_id, isin, kind, amount, units, nav, balance, idem_key FROM transactions "
            "WHERE units IS NOT NULL AND balance IS NOT NULL AND nav IS NOT NULL ORDER BY id"):
            groups.setdefault((t["account_id"], t["isin"], t["kind"], t["amount"], t["units"], t["nav"], t["balance"]), []).append(t)
        doomed = []
        for rows in groups.values():
            if len({r["doc_id"] for r in rows}) < 2:
                continue
            per_doc = Counter(r["doc_id"] for r in rows)
            keep = max(per_doc.values())                   # one statement lists the true multiplicity
            doomed += [(r["id"], r["idem_key"]) for r in rows[keep:]]
        return doomed

    def unify_nps_scheme_ids(self) -> int:
        """Re-key NPS rows stored under an older scheme identity (an exact name) to the stable one (fund house, scheme
        letter, tier), so the same scheme spelled two ways becomes one scheme and its overlapping rows can be seen as
        duplicates. A snapshot that would collide with an existing one (same scheme, same date) is the same state
        twice, so the variant is dropped. Returns the number of schemes re-keyed."""
        from finance.schemes import scheme_id
        pairs = self.conn.execute(
            "SELECT DISTINCT x.account_id, x.isin, x.scheme_name FROM (SELECT account_id, isin, scheme_name FROM transactions "
            "UNION SELECT account_id, isin, scheme_name FROM holdings) x JOIN accounts a ON a.id = x.account_id "
            "WHERE a.type = 'nps' AND x.isin LIKE 'NPS:%' AND x.scheme_name != ''").fetchall()
        changed = 0
        with self.conn:
            for p in pairs:
                new = scheme_id(p["scheme_name"])
                if new == p["isin"]:
                    continue
                changed += 1
                self.conn.execute("UPDATE transactions SET isin=? WHERE account_id=? AND isin=? AND scheme_name=?",
                                  (new, p["account_id"], p["isin"], p["scheme_name"]))
                self.conn.execute("UPDATE OR IGNORE holdings SET isin=? WHERE account_id=? AND isin=? AND scheme_name=?",
                                  (new, p["account_id"], p["isin"], p["scheme_name"]))
                self.conn.execute("DELETE FROM holdings WHERE account_id=? AND isin=? AND scheme_name=?",
                                  (p["account_id"], p["isin"], p["scheme_name"]))      # the ones that collided
                self.conn.execute("UPDATE OR IGNORE prices SET isin=? WHERE isin=?", (new, p["isin"]))
            self.conn.execute("DELETE FROM prices WHERE isin LIKE 'NPS:%' AND isin NOT IN "
                              "(SELECT isin FROM holdings UNION SELECT isin FROM transactions)")
        return changed

    def flag_duplicate_transactions(self) -> list[str]:
        self.unify_nps_scheme_ids()
        doomed = self._duplicate_txn_ids()
        if not doomed:
            return []
        msg = (f"{len(doomed)} transaction(s) are stored twice because different statements gave them different "
               f"dates (same fund, type, amount, units, NAV and unit balance). They inflate money invested and distort XIRR.")
        with self.conn:
            open_item = self.conn.execute("SELECT id FROM review_queue WHERE kind='duplicate_txns' AND status='open'").fetchone()
            if open_item:
                self.conn.execute("UPDATE review_queue SET reason=? WHERE id=?", (msg, open_item["id"]))
            else:
                self.conn.execute("INSERT INTO review_queue (kind, ref, reason) VALUES ('duplicate_txns', 'all', ?)", (msg,))
        return [msg]

    def remove_duplicate_transactions(self) -> int:
        """Delete the later-imported copy of each duplicate (the first import wins) and remember their keys."""
        doomed = self._duplicate_txn_ids()
        with self.conn:
            for tid, key in doomed:
                self.conn.execute("INSERT OR IGNORE INTO txn_ignored (idem_key) VALUES (?)", (key,))
                self.conn.execute("DELETE FROM transactions WHERE id=?", (tid,))
            self.conn.execute("UPDATE review_queue SET status='resolved', resolved_at=datetime('now') "
                              "WHERE kind='duplicate_txns' AND status='open'")
        return len(doomed)
