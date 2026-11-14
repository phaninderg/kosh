"""Sign-in plumbing for the web app: signed session cookies, throttling, capped password work and the activity log.

A session cookie says "profile <uid>, session version <sv>, until <time>", signed with a random server key (data/.session_key,
mode 0600). It is checked against the profile's current version on every request, so **Sign out everywhere**, a password change,
or disabling a profile ends every cookie at once. A cookie never contains, and cannot produce, a data key: opening a profile's
data takes its password (or recovery key), so a stolen cookie or a stolen disk image is not enough to read anything.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import secrets
import threading
import time
from pathlib import Path

from finance import vault

SESSION_SECONDS = 30 * 24 * 3600


# ── sessions ──────────────────────────────────────────────────────────────────

def _key_file() -> Path:
    return vault.DATA_DIR / ".session_key"


def _session_key() -> bytes:
    path = _key_file()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        vault._atomic_write(path, secrets.token_bytes(32))
    return path.read_bytes()


def _mac(payload: str) -> str:
    return hmac.new(_session_key(), payload.encode(), hashlib.sha256).hexdigest()


def issue_session(uid: str, sv: int, now: float | None = None) -> str:
    payload = f"{uid}.{int((now or time.time()) + SESSION_SECONDS)}.{sv}"
    return f"{payload}.{_mac(payload)}"


def read_session(cookie: str, now: float | None = None) -> tuple[str, int, int] | None:
    """(uid, session version, expiry) when the cookie is genuine and unexpired, else None."""
    try:
        uid, expires, sv, mac = cookie.split(".")
        if not hmac.compare_digest(mac, _mac(f"{uid}.{expires}.{sv}")) or int(expires) <= (now or time.time()):
            return None
        return uid, int(sv), int(expires)
    except (ValueError, AttributeError):
        return None


def issued_at(expires: int) -> float:
    return expires - SESSION_SECONDS


# ── cookies (after openGym: a __Host- cookie on https, and no guessing between duplicates) ─────────────────

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


def safe_next(target: str) -> str:
    """Where to go after login: a path on this site, never another address."""
    ok = target.startswith("/") and not target.startswith("//") and all(0x21 <= ord(c) <= 0x7E for c in target) and "\\" not in target
    return target if ok else "/"                         # printable ASCII only: a tab or newline can turn "/\t/x" into "//x" in a browser


# ── password work that cannot be used to burn the CPU ──────────────────────────────

_SLOTS = threading.BoundedSemaphore(2)


class Busy(Exception):
    """Two password checks are already running and a third waited too long."""


def _limited(fn, *args):
    if not _SLOTS.acquire(timeout=5):
        raise Busy
    try:
        return fn(*args)
    finally:
        _SLOTS.release()


async def run_limited(fn, *args):
    """scrypt is deliberately expensive, so run it off the event loop and never more than two at a time."""
    return await asyncio.get_running_loop().run_in_executor(None, _limited, fn, *args)


# ── throttling ────────────────────────────────────────────────────────────────

class Throttle:
    """Slows password guessing: five failures from one key lock it out, with the lock doubling each time up to an hour.
    An attempt is counted *before* the slow password check (`reserve`) and forgiven on success, so a burst of parallel
    guesses cannot all slip through. The key is the client's address, or for someone already signed in, their own profile,
    so a stranger can never lock a signed-in person out. There is deliberately no global lock: it would let anyone block
    everybody."""

    PER_CLIENT = 5

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
        wait = max(self._locked_until.get(client, 0) - self._clock(), 0)
        return int(wait) + (1 if wait else 0)

    def reserve(self, client: str) -> int:
        """Allow an attempt (returning 0) and count it as a failure until `succeeded` says otherwise; or return the seconds to wait."""
        wait = self.retry_after(client)
        if not wait:
            self.failed(client)
        return wait

    def failed(self, client: str) -> None:
        now = self._clock()
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

_LOG_LIMIT = 1000


def _audit_file() -> Path:
    return vault.DATA_DIR / "audit.log"


def audit(event: str, ok: bool = True, client: str = "", who: str = "") -> None:
    """Append one line to data/audit.log (mode 0600): when, who, what, whether it worked, from which address. Never a
    password, code or cookie, and nothing about anyone's money. Keeps the last 1000 lines."""
    path = _audit_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = path.read_text().splitlines()[-(_LOG_LIMIT - 1):] if path.exists() else []
        lines.append(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), "who": who, "event": event,
                                 "ok": ok, "from": client}))
        vault._atomic_write(path, ("\n".join(lines) + "\n").encode())
    except OSError:
        pass                                              # a full disk must not lock the owner out


def recent_events(limit: int = 15, who: str | None = None) -> list[dict]:
    """Newest first. `who` limits it to one profile's name; None is everyone (the admin's view)."""
    try:
        rows = [json.loads(line) for line in _audit_file().read_text().splitlines()]
    except (OSError, ValueError):
        return []
    if who is not None:
        rows = [r for r in rows if r.get("who") == who]
    return list(reversed(rows[-limit:]))
