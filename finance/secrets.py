"""Secret access: the signed-in profile's vault, and nothing else.

Mailbox app passwords, personal details (name, date of birth, PAN, mobile), the default PDF password and remembered PDF
passwords are stored sealed inside the profile's own vault (see finance/vault.py). There is no OS keyring, no environment
variable and no shared file behind this, because anything global would be readable by every profile on the server.
Outside a signed-in context (for example a parser unit test) a read finds nothing and a write is refused.

Keys:  imap:<account>            app password for a mailbox
       id:name|dob|pan|mobile|password|card4|extra_passwords   identity values used to build PDF passwords
"""

from __future__ import annotations

import json

from finance import vault

_FILE = "secrets.json"


def _load(v: vault.Vault) -> dict:
    raw = v.get(_FILE)
    return json.loads(raw) if raw else {}


def get_secret(key: str) -> str | None:
    v = vault.current_vault.get()
    return (_load(v).get(key) or None) if v else None


def has_secret(key: str) -> bool:
    return get_secret(key) is not None


def set_secret(key: str, value: str) -> None:
    v = vault.require_vault()
    with v._lock:
        data = _load(v)
        data[key] = value
        v.put(_FILE, json.dumps(data).encode())


def delete_secret(key: str) -> None:
    v = vault.current_vault.get()
    if not v:
        return
    with v._lock:
        data = _load(v)
        if data.pop(key, None) is not None:
            v.put(_FILE, json.dumps(data).encode())
