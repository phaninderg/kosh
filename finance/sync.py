"""Sync runner (phase 2): Mailbox -> Decrypt -> Parser -> Ledger, one account at a time.

A failure to unlock or parse a document never stops the run: it goes to the needs-attention
queue (the encrypted PDF is kept in data/inbox so it can be retried after fixing templates or
secrets). A failure of one mailbox never stops the others. The full Orchestrator (retries,
scheduling) arrives in phase 5 and will drive this same logic.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from config import DB_PATH, INBOX_DIR
from finance.ledger import Ledger
from finance.mailconfig import MailConfig, MailboxAccount, SenderRule, load_config
from finance.models import SourceDocument
from finance.parsers.base import REGISTRY
import finance.parsers.cas  # noqa: F401  (registers the parser)
import finance.parsers.icici  # noqa: F401  (registers the parser)
import finance.parsers.icici_card  # noqa: F401  (registers the parser)
import finance.parsers.hdfc_card  # noqa: F401  (registers the parser)
import finance.parsers.axis_card  # noqa: F401  (registers the parser)
import finance.parsers.tata_card  # noqa: F401  (registers the parser)
import finance.parsers.nps  # noqa: F401  (registers the parser)
from finance.passwords import DecryptAgent, Identity
from finance.redact import make_redactor, redact
from finance.sources.base import MailboxError, Source
from finance.sources.imap import ImapSource


@dataclass
class AccountSummary:
    name: str
    messages: int = 0
    ignored: int = 0
    rejected: int = 0
    imported: int = 0
    duplicate: int = 0
    attention: int = 0
    txn_new: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class SyncSummary:
    accounts: list[AccountSummary] = field(default_factory=list)

    def render(self) -> str:
        lines = []
        for a in self.accounts:
            if a.error:
                lines.append(f"  ✗ {a.name}: {a.error}")
                continue
            lines.append(f"  ✓ {a.name}: {a.messages} message(s) — {a.imported} imported ({a.txn_new} new txns), "
                         f"{a.duplicate} already known, {a.attention} need attention, "
                         f"{a.rejected} rejected, {a.ignored} ignored")
            lines += [f"      ⚠ {w}" for w in a.warnings]
        return "\n".join(lines) or "  no mailboxes configured"


class _LedgerState:
    """Adapts the Ledger to the ImapStateStore protocol."""
    def __init__(self, ledger: Ledger): self.ledger = ledger
    def get(self, account, folder): return self.ledger.get_imap_state(account, folder)
    def set(self, account, folder, uidvalidity, last_uid):
        self.ledger.set_imap_state(account, folder, uidvalidity, last_uid)


def _default_factory(account: MailboxAccount, state) -> Source:
    return ImapSource(account, state)


class SyncRunner:
    def __init__(self, db_path: str | None = None, config: MailConfig | None = None,
                 source_factory: Callable = _default_factory, identity: Identity | None = None,
                 inbox_dir: str | None = None, log: Callable[[str], None] = print):
        self.db_path, self.cfg = db_path or DB_PATH, config or load_config()
        self.source_factory, self.identity = source_factory, identity or Identity()
        self.inbox = Path(inbox_dir or INBOX_DIR)
        self.log = log

    # ── full sync ─────────────────────────────────────────────────────────────

    def run(self, account_names: list[str] | None = None) -> SyncSummary:
        summary = SyncSummary()
        ledger = Ledger(self.db_path)
        try:
            decrypt = DecryptAgent(self.identity, ledger.password_templates(), ledger.card_last4s())
            for acct in self.cfg.accounts:
                if account_names and acct.name not in account_names:
                    continue
                s = AccountSummary(acct.name)
                summary.accounts.append(s)
                self.log(f"[sync] {acct.name}: connecting…")
                source = self.source_factory(acct, _LedgerState(ledger))
                try:
                    for msg in source.fetch_new():
                        s.messages += 1
                        self._handle_message(ledger, decrypt, acct, msg, s)
                        source.commit(msg)                     # only after the message is recorded
                except MailboxError as exc:
                    s.error = str(exc)
                except Exception as exc:                       # protocol/infra problem: keep going with other mailboxes
                    s.error = f"{type(exc).__name__}: {str(exc)[:200]}"
                finally:
                    source.close()
                self.log(f"[sync] {acct.name}: done")
        finally:
            ledger.close()
        return summary

    def _handle_message(self, ledger, decrypt, acct, msg, s: AccountSummary) -> None:
        if msg.skip_reason:
            if msg.reject:
                s.rejected += 1
                ledger.add_review("rejected_mail", msg.message_id,
                                  f"{msg.skip_reason} (mailbox {acct.name}, subject: "
                                  f"{redact(msg.subject, self.identity.get('name'), self.identity.get('pan'), self.identity.get('mobile'), 80)})")
            else:
                s.ignored += 1
            return
        rule = next(r for r in acct.senders if r.match == msg.rule_match)
        for doc in msg.docs:
            self._process(ledger, decrypt, doc, rule, s)

    def _meta(self, doc: SourceDocument) -> dict:
        """Email details to store. Free text is masked with the user's own identifiers."""
        ident = self.identity
        mask = lambda t: redact(t, ident.get("name"), ident.get("pan"), ident.get("mobile"))
        return {"account": doc.account, "folder": doc.folder, "uid": doc.uid, "received_at": doc.received_at,
                "subject": mask(doc.subject), "filename": mask(doc.filename), "size": len(doc.data),
                "auth": doc.auth}

    # ── one document ──────────────────────────────────────────────────────────

    def _process(self, ledger: Ledger, decrypt: DecryptAgent, doc: SourceDocument, rule: SenderRule,
                 s: AccountSummary, retry: bool = False, priority: list[str] | None = None) -> str:
        known = ledger.known_document(doc.message_id, doc.sha256)
        if known and not retry and known != "failed":
            row = ledger.conn.execute("SELECT id FROM source_documents WHERE message_id=? AND sha256=?",
                                      (doc.message_id, doc.sha256)).fetchone()
            ledger.fill_missing_meta(row["id"], self._meta(doc))            # e.g. after a re-scan
            if known != "needs_attention":                      # queued items are not counted twice
                s.duplicate += 1
            return "known"

        unlocked = decrypt.unlock(doc.data, doc.sender, rule.templates, priority=priority)
        if not unlocked.ok:
            return self._attention(ledger, doc, rule, unlocked.reason, s)

        parser = REGISTRY.get(rule.parser)
        if not parser:
            return self._attention(ledger, doc, rule, f"no parser named '{rule.parser}' is installed", s)

        workdir = tempfile.mkdtemp(prefix="pf-")
        try:
            path = Path(workdir) / "statement.pdf"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(doc.data)
            password = unlocked.password.get_secret_value() if unlocked.password else None
            try:
                stmt = parser.parse(path, password)
            except Exception as exc:
                return self._attention(ledger, doc, rule, f"{rule.parser} parser failed: {type(exc).__name__}: "
                                       f"{str(exc)[:200]}", s)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        try:
            res = self._import(ledger, stmt, doc)
        except ValueError as exc:                               # e.g. the balance check refused the statement
            return self._attention(ledger, doc, rule, f"{rule.parser}: {str(exc)[:300]}", s)
        (self.inbox / f"{doc.sha256}.pdf").unlink(missing_ok=True)
        if unlocked.card4:
            self.identity.remember_card4(unlocked.card4)
        if unlocked.template:
            ledger.cache_template(doc.sender, unlocked.template)
            decrypt.cache[doc.sender] = unlocked.template
        if res.status == "duplicate":
            s.duplicate += 1
        else:
            s.imported += 1
            s.txn_new += res.txn_new
            s.warnings += res.warnings
        return res.status

    def _import(self, ledger: Ledger, stmt, doc: SourceDocument):
        ident = self.identity
        return ledger.import_statement(stmt, sha256=doc.sha256, source="imap", message_id=doc.message_id,
                                      sender=doc.sender, meta=self._meta(doc),
                                      redactor=make_redactor(ident.get("name"), ident.get("pan"), ident.get("mobile")))

    def _attention(self, ledger: Ledger, doc: SourceDocument, rule: SenderRule, reason: str,
                   s: AccountSummary) -> str:
        self.inbox.mkdir(parents=True, exist_ok=True)
        path = self.inbox / f"{doc.sha256}.pdf"
        if not path.exists():                                   # keep the (still encrypted) file so Retry works offline
            fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(doc.data)
        ledger.record_attention(sha256=doc.sha256, message_id=doc.message_id, sender=doc.sender,
                                parser=rule.parser, reason=reason, meta=self._meta(doc))
        s.attention += 1
        return "attention"

    # ── retry one queued document (no network) ────────────────────────────────

    def retry(self, doc_id: int, password: str | None = None, remember: bool = False) -> str:
        """Re-run a queued document offline. `password` is a one-off literal tried first; it lives in
        memory only unless `remember` is set AND it actually opened the file (then it goes to the keyring)."""
        ledger = Ledger(self.db_path)
        try:
            row = ledger.document(doc_id)
            if not row or row["status"] != "needs_attention":
                return "not waiting for attention"
            path = self.inbox / f"{row['sha256']}.pdf"
            if not path.exists():
                return "the saved PDF is gone; re-sync after resetting the mailbox, or dismiss"
            rule = next((r for a in self.cfg.accounts for r in a.senders if r.matches(row["sender"])),
                        SenderRule(match=(row["sender"] or "unknown.invalid"), parser=row["parser"] or "cas"))
            doc = SourceDocument(account=row["account"], message_id=row["message_id"], sender=row["sender"],
                                 subject=row["subject"], filename=row["filename"], sha256=row["sha256"],
                                 folder=row["folder"], uid=row["uid"], received_at=row["received_at"],
                                 auth=row["auth"], data=path.read_bytes())
            s = AccountSummary("retry")
            decrypt = DecryptAgent(self.identity, ledger.password_templates(), ledger.card_last4s())
            outcome = self._process(ledger, decrypt, doc, rule, s, retry=True,
                                    priority=[password] if password else None)
            if remember and password and outcome in ("imported", "duplicate"):
                self.remember_password(password)
            if outcome == "attention":
                return "still needs attention: " + (ledger.document(doc_id) or {"error": ""})["error"]
            return outcome
        finally:
            ledger.close()

    @staticmethod
    def remember_password(password: str) -> None:
        import json
        from finance import secrets
        saved = Identity().extra_passwords()
        if password not in saved:
            secrets.set_secret("id:extra_passwords", json.dumps(saved + [password]))

    def dismiss(self, doc_id: int) -> None:
        ledger = Ledger(self.db_path)
        try:
            row = ledger.document(doc_id)
            ledger.dismiss_attention(doc_id)
            if row:
                (self.inbox / f"{row['sha256']}.pdf").unlink(missing_ok=True)
        finally:
            ledger.close()
