"""Keyed hashing so the ledger never holds full folio/account numbers.

The HMAC key is derived from the signed-in profile's own data key (see vault.py), so there is no shared key file and nothing
to configure; hashes made for one profile mean nothing to another.
"""

from __future__ import annotations

import hashlib
import hmac

from finance import vault


def _key() -> bytes:
    return vault.require_vault().hash_key()      # raises VaultLocked outside a signed-in context


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
