"""Mailbox Agent source: read-only IMAP.

Read-only guarantees:
  * the folder is opened with EXAMINE (select readonly=True);
  * messages are fetched with BODY.PEEK[] so flags (\\Seen) are untouched;
  * the connection is wrapped in ReadOnlyImap, which exposes no store/copy/move/delete/append/expunge.
"""

from __future__ import annotations

import email
import hashlib
import imaplib
import ipaddress
import re
import socket
import ssl
from datetime import date, timedelta, timezone
from email import policy
from email.utils import parseaddr, parsedate_to_datetime
from typing import Callable, Iterator, Protocol

from finance.mailconfig import MailboxAccount
from finance.models import SourceDocument, SourceMessage
from finance.secrets import get_secret
from finance.sources.base import MailboxError

MAX_MESSAGE_BYTES = 30 * 1024 * 1024
MAX_MULTIPART_PARTS = 40
PROVIDER_AUTH_DOMAINS = {"gmail": ("google.com",), "yahoo": ("yahoo.com", "yahoo.net", "yahoodns.net", "aol.com")}


def trusted_authserv(account) -> tuple[str, ...]:
    """Domains whose Authentication-Results we believe: the provider's own, or for a custom server the domain of the mail host."""
    if account.provider in PROVIDER_AUTH_DOMAINS:
        return PROVIDER_AUTH_DOMAINS[account.provider]
    return (".".join(account.imap_host.lower().split(".")[-2:]),)


def authserv_id(header: str) -> str:
    return header.split(";", 1)[0].strip().lower()
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_AUTH_RESULT = re.compile(r"\b(dkim|spf|dmarc)=([a-z]+)", re.I)


def _top_header(headers: list) -> str:
    """Only the topmost Authentication-Results header: the receiving server prepends its own, so anything lower was
    written by the sender (or a relay) and must not be believed."""
    return str(headers[0]) if headers else ""


def auth_summary(headers: list) -> str:
    """Verdict per mechanism from the topmost header, e.g. 'spf=pass dkim=pass'; empty when the server added none."""
    seen: dict[str, str] = {}
    for mech, verdict in _AUTH_RESULT.findall(_top_header(headers)):
        seen.setdefault(mech.lower(), verdict.lower())
    return " ".join(f"{k}={v}" for k, v in seen.items())


def _aligned(domain: str, from_domain: str) -> bool:
    domain, from_domain = domain.lower().strip(">"), from_domain.lower()
    return domain == from_domain or domain.endswith("." + from_domain) or from_domain.endswith("." + domain)


def _passes_for_sender(header: str, from_domain: str) -> bool:
    """Is there a pass that is about the From domain? DMARC pass says so itself; a bare DKIM or SPF pass may be for an
    attacker's own domain, so when the header names the domain (header.d, smtp.mailfrom) it must match From."""
    for clause in header.split(";"):
        m = re.search(r"\b(dkim|spf|dmarc)=(pass|bestguesspass)\b", clause, re.I)
        if not m:
            continue
        if m.group(1).lower() == "dmarc":
            return True
        d = re.search(r"(?:header\.d|smtp\.mailfrom|header\.i)=@?(?:[^@\s;]*@)?([^\s;]+)", clause, re.I)
        if not d or _aligned(d.group(1), from_domain):
            return True
    return False


def auth_failed(headers: list, from_domain: str = "", trusted: tuple[str, ...] | None = None) -> bool:
    """Is the sender not verified as authentic? DMARC is the combined verdict (SPF or DKIM passed AND matched the
    From domain), so when it is present it decides: pass accepts a message even if DKIM alone failed (a relay that
    rewrites a message breaks the signature but not SPF), fail rejects it. Without a DMARC result any explicit failure
    of SPF or DKIM rejects, and so does the absence of a pass for the From domain: a message nobody vouched for is
    not accepted just because nobody said it was bad."""
    top = _top_header(headers)
    if trusted is not None:                               # only a header written by the receiving server counts; anything else is unverified
        serv = authserv_id(top)
        if not any(serv == d or serv.endswith("." + d) for d in trusted):
            return True
    verdicts = dict(p.split("=") for p in auth_summary(headers).split())
    dmarc = verdicts.get("dmarc")
    if dmarc in ("pass", "bestguesspass"):
        return False
    if dmarc == "fail" or any(v == "fail" for v in verdicts.values()):
        return True
    return not _passes_for_sender(top, from_domain)


def received_utc(msg) -> str | None:
    try:
        dt = parsedate_to_datetime(str(msg.get("Date", "")))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


class ImapStateStore(Protocol):
    def get(self, account: str, folder: str) -> tuple[int, int] | None: ...        # (uidvalidity, last_uid)
    def set(self, account: str, folder: str, uidvalidity: int, last_uid: int) -> None: ...


class ReadOnlyImap:
    """The only IMAP operations this app can perform."""

    def __init__(self, conn):
        self._c = conn

    def select(self, folder: str) -> int:
        typ, _ = self._c.select(f'"{folder}"', readonly=True)           # EXAMINE
        if typ != "OK":
            raise MailboxError(f"cannot open folder {folder!r}")
        typ, data = self._c.response("UIDVALIDITY")
        try:
            return int(data[0])
        except (TypeError, ValueError, IndexError):
            return 0

    def search(self, criteria: str) -> list[int]:
        typ, data = self._c.uid("SEARCH", criteria)
        if typ != "OK":
            raise MailboxError("IMAP search failed")
        return [int(x) for x in (data[0] or b"").split()]

    def size(self, uid: int) -> int:
        typ, data = self._c.uid("FETCH", str(uid), "(RFC822.SIZE)")
        m = re.search(rb"RFC822\.SIZE (\d+)", b" ".join(x if isinstance(x, bytes) else x[0] for x in data if x))
        return int(m.group(1)) if m else 0

    def message(self, uid: int) -> bytes:
        typ, data = self._c.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK":
            raise MailboxError(f"cannot fetch uid {uid}")
        for part in data:
            if isinstance(part, tuple):
                return part[1]
        raise MailboxError(f"empty fetch for uid {uid}")

    def logout(self) -> None:
        try:
            self._c.logout()
        except Exception:
            pass


def _imap_date(d: date) -> str:
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def _default_connect(host: str, port: int):
    """Connect over verified TLS, but only to a public address: a custom host must not turn the server into a scanner for its own network."""
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise MailboxError(f"cannot resolve {host}") from exc
    if not addresses or not all(ipaddress.ip_address(a).is_global for a in addresses):
        raise MailboxError(f"{host} is not a public mail server")
    return imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=30)   # verifies the certificate and host name


class ImapSource:
    def __init__(self, account: MailboxAccount, state: ImapStateStore,
                 connect: Callable = _default_connect, today: Callable[[], date] = date.today):
        self.account, self.state, self._connect, self._today = account, state, connect, today
        self.name = f"imap:{account.name}"
        self._ro: ReadOnlyImap | None = None
        self._uidvalidity = 0
        self._last = 0

    # ── connection ────────────────────────────────────────────────────────────

    def _open(self) -> ReadOnlyImap:
        password = get_secret(self.account.secret_key)
        if not password:
            raise MailboxError(f"no app password stored for mailbox '{self.account.name}'")
        try:
            conn = self._connect(self.account.imap_host, self.account.port)
            conn.login(self.account.username, password)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise MailboxError(f"cannot sign in to {self.account.imap_host}: {type(exc).__name__}") from None
        self._ro = ReadOnlyImap(conn)
        self._uidvalidity = self._ro.select(self.account.folder)
        return self._ro

    def check(self) -> int:
        """Connectivity test used by the UI: returns the number of allowlisted messages visible."""
        ro = self._open()
        try:
            return len({u for r in self.account.senders for u in ro.search(f'FROM "{r.match}"')})
        finally:
            ro.logout()

    # ── Source interface ──────────────────────────────────────────────────────

    def fetch_new(self) -> Iterator[SourceMessage]:
        ro = self._open()
        saved = self.state.get(self.account.name, self.account.folder)
        self._last = saved[1] if saved and saved[0] == self._uidvalidity else 0   # UIDVALIDITY changed => start over;
        since = _imap_date(self._today() - timedelta(days=self.account.lookback_days))  # dedupe is by message-id+hash
        uids: set[int] = set()
        for rule in self.account.senders:
            scope = f"UID {self._last + 1}:*" if self._last else f"SINCE {since}"
            uids |= {u for u in ro.search(f'FROM "{rule.match}" {scope}') if u > self._last}
        for uid in sorted(uids):
            try:
                yield self._load(ro, uid)
            except Exception as exc:                      # a message that cannot be read is recorded as rejected, never retried forever
                yield SourceMessage(uid=uid, message_id=f"uid:{self.account.name}:{self.account.folder}:{self._uidvalidity}:{uid}",
                                    sender="", skip_reason=f"message could not be read ({type(exc).__name__})", reject=True)

    def commit(self, msg: SourceMessage) -> None:
        self._last = max(self._last, msg.uid)
        self.state.set(self.account.name, self.account.folder, self._uidvalidity, self._last)

    def close(self) -> None:
        if self._ro:
            if self._uidvalidity:                        # record validity + last sync even if nothing was new
                self.state.set(self.account.name, self.account.folder, self._uidvalidity, self._last)
            self._ro.logout()
            self._ro = None

    # ── message parsing ───────────────────────────────────────────────────────

    def _load(self, ro: ReadOnlyImap, uid: int) -> SourceMessage:
        fallback_id = f"uid:{self.account.name}:{self.account.folder}:{self._uidvalidity}:{uid}"
        if ro.size(uid) > MAX_MESSAGE_BYTES:
            return SourceMessage(uid=uid, message_id=fallback_id, sender="", skip_reason="message too large",
                                 reject=True)
        raw = ro.message(uid)
        if raw.count(b"ultipart/") > MAX_MULTIPART_PARTS:                          # deep MIME nesting crashes the parser
            return SourceMessage(uid=uid, message_id=fallback_id, sender="", skip_reason="message structure too complex",
                                 reject=True)
        msg = email.message_from_bytes(raw, policy=policy.default)
        sender = parseaddr(str(msg.get("From", "")))[1].lower()
        message_id = str(msg.get("Message-ID", "")).strip() or fallback_id
        subject = str(msg.get("Subject", ""))[:200]
        out = SourceMessage(uid=uid, message_id=message_id, sender=sender, subject=subject)
        received = received_utc(msg)
        auth = auth_summary(msg.get_all("Authentication-Results", []))

        rule = next((r for r in self.account.senders if r.matches(sender)), None)
        if not rule:                                      # server-side FROM is a substring match: re-check strictly
            out.skip_reason = "sender not on allowlist"
            return out
        out.rule_match = rule.match
        if self.account.require_auth and auth_failed(msg.get_all("Authentication-Results", []), sender.rsplit("@", 1)[-1], trusted_authserv(self.account)):
            out.skip_reason = f"sender authentication failed ({auth}) for {sender}, email dated {received or 'unknown'}"
            out.reject = True
            return out

        for part in msg.walk():
            name = part.get_filename() or ""
            if part.get_content_type() != "application/pdf" and not name.lower().endswith(".pdf"):
                continue
            data = part.get_payload(decode=True)
            if data:
                out.docs.append(SourceDocument(
                    account=self.account.name, message_id=message_id, sender=sender, subject=subject,
                    filename=name, sha256=hashlib.sha256(data).hexdigest(), data=data,
                    folder=self.account.folder, uid=uid, received_at=received, auth=auth))
        return out
