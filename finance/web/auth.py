"""Login for a hosted instance (KOSH_AUTH=login): a password, optionally a one-time code from an authenticator app, and a
signed session cookie that lasts 30 days. Everything is standard library.

Where things live (all under data/, mode 0600, never committed):
  auth.json       {"password_hash": "scrypt$...", "totp_secret": "<base32>"}   written by `python main.py set-password`
  .session_key    random bytes the session cookies are signed with; replacing it signs every device out

KOSH_PASSWORD_HASH and KOSH_TOTP_SECRET in the environment take precedence over auth.json.
A session cookie is bound to the current password and authenticator secret, so changing either signs everything out.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
SESSION_SECONDS = 30 * 24 * 3600
_SCRYPT = dict(n=2**14, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)


# ── password ──────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=64 * 1024 * 1024, dklen=len(bytes.fromhex(digest)))
        return hmac.compare_digest(got, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


# ── one-time codes (RFC 6238: 30 s steps, 6 digits, HMAC-SHA1, what every authenticator app speaks) ───────────

def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    return f"{(struct.unpack('>I', mac[offset:offset + 4])[0] & 0x7FFFFFFF) % 10**6:06d}"


def totp_uri(secret: str, account: str = "owner", issuer: str = "Kosh") -> str:
    return f"otpauth://totp/{issuer}:{account}?secret={secret}&issuer={issuer}&digits=6&period=30"


class TotpVerifier:
    """Accepts the current code and one step either side (clock drift), and each code only once."""

    def __init__(self, clock=time.time):
        self._clock, self._used = clock, set()

    def verify(self, secret: str, code: str) -> bool:
        code = "".join(code.split())
        if len(code) != 6 or not code.isdigit():
            return False
        now = int(self._clock() // 30)
        for step in (now - 1, now, now + 1):
            if hmac.compare_digest(_hotp(secret, step), code) and (secret, step) not in self._used:
                self._used.add((secret, step))
                return True
        return False


# ── credentials ───────────────────────────────────────────────────────────────

def _auth_file() -> Path:
    return DATA_DIR / "auth.json"


def load_credentials() -> tuple[str, str | None] | None:
    """(password hash, TOTP secret or None), or None when no password has been set."""
    pw, totp = os.getenv("KOSH_PASSWORD_HASH"), os.getenv("KOSH_TOTP_SECRET")
    if not pw:
        try:
            data = json.loads(_auth_file().read_text())
            pw, totp = data.get("password_hash"), totp or data.get("totp_secret")
        except (OSError, ValueError):
            return None
    return (pw, totp or None) if pw else None


def save_credentials(password_hash: str, totp_secret: str | None) -> None:
    path = _auth_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"password_hash": password_hash, "totp_secret": totp_secret}, f)
    os.chmod(path, 0o600)


# ── sessions ──────────────────────────────────────────────────────────────────

def _key_file() -> Path:
    return DATA_DIR / ".session_key"


def _session_key() -> bytes:
    path = _key_file()
    if not path.exists():
        rotate_session_key()
    return path.read_bytes()


def rotate_session_key() -> None:
    """Sign every device out."""
    path = _key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(secrets.token_bytes(32))


def _signing_key(creds: tuple[str, str | None]) -> bytes:
    return hmac.new(_session_key(), hashlib.sha256(f"{creds[0]}|{creds[1] or ''}".encode()).digest(), hashlib.sha256).digest()


def issue_session(creds: tuple[str, str | None], now: float | None = None) -> str:
    expires = int((now or time.time()) + SESSION_SECONDS)
    payload = f"{expires}.{secrets.token_urlsafe(12)}"
    return f"{payload}.{hmac.new(_signing_key(creds), payload.encode(), hashlib.sha256).hexdigest()}"


def session_valid(cookie: str, creds: tuple[str, str | None], now: float | None = None) -> bool:
    try:
        expires, nonce, mac = cookie.split(".")
        good = hmac.new(_signing_key(creds), f"{expires}.{nonce}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(mac, good) and int(expires) > (now or time.time())
    except (ValueError, AttributeError):
        return False


# ── throttling ────────────────────────────────────────────────────────────────

class Throttle:
    """Slows password guessing: five failures from one address lock it out, with the lock doubling each time up to an
    hour; and a global cap so many addresses cannot share the guessing. A correct login resets the address."""

    PER_CLIENT, GLOBAL_PER_HOUR = 5, 30

    def __init__(self, clock=time.time):
        self._clock = clock
        self._fails: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._strikes: dict[str, int] = {}

    def _recent(self, key: str, window: float) -> list[float]:
        now = self._clock()
        kept = [t for t in self._fails.get(key, []) if now - t < window]
        self._fails[key] = kept
        return kept

    def retry_after(self, client: str) -> int:
        """Seconds to wait, 0 when an attempt is allowed."""
        now = self._clock()
        wait = max(self._locked_until.get(client, 0) - now, 0)
        if len(self._recent("*", 3600)) >= self.GLOBAL_PER_HOUR:
            wait = max(wait, 3600 - (now - min(self._fails["*"])))
        return int(wait) + (1 if wait else 0)

    def failed(self, client: str) -> None:
        now = self._clock()
        self._fails.setdefault("*", []).append(now)
        recent = self._recent(client, 900)
        recent.append(now)
        if len(recent) >= self.PER_CLIENT:
            strikes = self._strikes[client] = self._strikes.get(client, 0) + 1
            self._locked_until[client] = now + min(60 * 2 ** (strikes - 1), 3600)
            self._fails[client] = []

    def succeeded(self, client: str) -> None:
        self._fails.pop(client, None)
        self._strikes.pop(client, None)
        self._locked_until.pop(client, None)


def safe_next(target: str) -> str:
    """Where to go after login: a path on this site, never another address."""
    return target if target.startswith("/") and not target.startswith("//") and "\\" not in target else "/"


# ── cookie handling (after openGym's sessions: a __Host- cookie on https, and no guessing between duplicates) ──────

def cookie_name(secure: bool) -> str:
    """Over https the `__Host-` prefix makes the *browser* insist the cookie is host-only and Secure, so a sibling
    subdomain cannot plant a second session cookie that shadows the real one."""
    return "__Host-kosh_session" if secure else "kosh_session"


def cookie_value(cookie_header: str, name: str) -> str:
    """The value of cookie `name`, or "" when absent. If the same name arrives twice with different values, someone
    else set one: refuse both rather than guess which is the real session."""
    values = []
    for part in cookie_header.split(";"):
        key, sep, val = part.partition("=")
        if sep and key.strip() == name:
            values.append(val.strip())
    return values[0] if values and all(v == values[0] for v in values) else ""


# ── password checks that cannot be used to burn the CPU ────────────────────────

import asyncio  # noqa: E402
import ipaddress  # noqa: E402
import threading  # noqa: E402

_HASH_SLOTS = threading.BoundedSemaphore(2)


class Busy(Exception):
    """Two password checks are already running and a third waited too long."""


def _verify_limited(password: str, stored: str) -> bool:
    if not _HASH_SLOTS.acquire(timeout=5):
        raise Busy
    try:
        return verify_password(password, stored)
    finally:
        _HASH_SLOTS.release()


async def check_password(password: str, stored: str) -> bool:
    """scrypt is deliberately expensive, so run it off the event loop and never more than two at a time."""
    return await asyncio.get_running_loop().run_in_executor(None, _verify_limited, password, stored)


def client_key(host: str) -> str:
    """Who to throttle: the address, with an IPv6 address counted as its /64 (one customer holds the whole block, so
    rotating inside it must not reset the count) and an IPv4-mapped IPv6 address as the IPv4 it wraps."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host or "unknown"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


# ── activity log ──────────────────────────────────────────────────────────────

_LOG_LIMIT = 500


def _audit_file() -> Path:
    return DATA_DIR / "audit.log"


def audit(event: str, ok: bool = True, client: str = "") -> None:
    """Append one line to data/audit.log (mode 0600): when, what, whether it worked, from which address. Never a
    password, code or cookie. Keeps the last 500 lines so it cannot grow without bound."""
    path = _audit_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = path.read_text().splitlines()[-(_LOG_LIMIT - 1):] if path.exists() else []
        lines.append(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), "event": event, "ok": ok, "from": client}))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass                                              # a full disk must not lock the owner out


def recent_events(limit: int = 15) -> list[dict]:
    try:
        rows = [json.loads(line) for line in _audit_file().read_text().splitlines()[-limit:]]
    except (OSError, ValueError):
        return []
    return list(reversed(rows))
