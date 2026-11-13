"""Keyed hashing so the DB never holds full folio/account numbers.

The HMAC key comes from LEDGER_HASH_KEY, else a 0600 file under data/ (gitignored).
Losing the key only breaks matching of *new* imports to old accounts.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

_KEY_FILE = Path(__file__).resolve().parent.parent / "data" / ".hash_key"


def _key() -> bytes:
    env = os.getenv("LEDGER_HASH_KEY")
    if env:
        return env.encode()
    if not _KEY_FILE.exists():
        _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    return _KEY_FILE.read_text().strip().encode()


def keyed_hash(*parts: object) -> str:
    msg = "\x1f".join(str(p) for p in parts).encode()
    return hmac.new(_key(), msg, hashlib.sha256).hexdigest()


def last4(value: str) -> str:
    digits = "".join(c for c in value if c.isalnum())
    return digits[-4:]


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
