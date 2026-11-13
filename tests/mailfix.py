"""Helpers: synthetic encrypted PDFs, RFC822 messages, and a strict fake IMAP server."""

import io
import itertools
import re
from email.message import EmailMessage

from pypdf import PdfWriter

_nonce = itertools.count(1)


def make_pdf(password: str | None = None) -> bytes:
    w = PdfWriter()
    w.add_blank_page(72, 72)
    w.add_metadata({"/Title": f"synthetic-{next(_nonce)}"})        # unique bytes => unique sha256
    if password:
        w.encrypt(password, algorithm="AES-128")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def make_email(*, sender="donotreply@camsonline.com", pdf: bytes | None = None, filename="statement.pdf",
               msgid=None, auth="mx.example; spf=pass dkim=pass", subject="Your statement",
               date="Mon, 02 Jun 2025 10:00:00 +0530") -> bytes:
    m = EmailMessage()
    m["From"] = f"Statements <{sender}>"
    m["To"] = "me@example.invalid"
    m["Subject"] = subject
    m["Date"] = date
    m["Message-ID"] = msgid or f"<m{next(_nonce)}@example>"
    if auth:
        m["Authentication-Results"] = auth
    m.set_content("see attachment")
    if pdf is not None:
        m.add_attachment(pdf, maintype="application", subtype="pdf", filename=filename)
    return m.as_bytes()


FORBIDDEN = {"store", "copy", "expunge", "delete", "append", "create", "rename", "move", "close"}


class FakeImap:
    """Serves {uid: raw message}. Fails loudly on any command that could modify the mailbox."""

    def __init__(self, messages: dict[int, bytes], uidvalidity: int = 7):
        self.messages, self.uidvalidity = messages, uidvalidity
        self.log: list[str] = []
        self.readonly_select = None

    def __getattr__(self, name):
        if name in FORBIDDEN:
            raise AssertionError(f"write command attempted: {name}")
        raise AttributeError(name)

    def login(self, user, password):
        self.log.append("login")

    def select(self, mailbox, readonly=False):
        self.readonly_select = readonly
        assert readonly, "mailbox must be opened read-only (EXAMINE)"
        self.log.append(f"examine {mailbox}")
        return "OK", [b"1"]

    def response(self, name):
        return "OK", [str(self.uidvalidity).encode()]

    def logout(self):
        self.log.append("logout")

    def uid(self, command, *args):
        command = command.upper()
        assert command in ("SEARCH", "FETCH"), f"unexpected UID {command}"
        if command == "SEARCH":
            crit = args[0]
            self.log.append("search " + crit)
            m = re.search(r'FROM "([^"]+)"', crit)
            uids = [u for u, raw in self.messages.items() if m is None or m.group(1).encode() in raw.split(b"\n\n")[0].lower()]
            n = re.search(r"UID (\d+):\*", crit)
            if n:
                start = int(n.group(1))
                hit = [u for u in uids if u >= start]
                uids = hit or ([max(uids)] if uids else [])        # real IMAP quirk: "n:*" always returns the last message
            return "OK", [" ".join(map(str, sorted(uids))).encode()]
        uid, items = int(args[0]), args[1]
        raw = self.messages[uid]
        if "RFC822.SIZE" in items:
            return "OK", [f"{uid} (UID {uid} RFC822.SIZE {len(raw)})".encode()]
        assert "BODY.PEEK[]" in items, "must fetch with BODY.PEEK so \\Seen is untouched"
        self.log.append(f"fetch {uid}")
        return "OK", [(f"{uid} (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
