"""Secret access: OS keyring first, then KOSH_* environment variables. Values are never logged,
never written to the DB, and never returned by status checks.

Entries saved by this app's predecessor (keyring service `portfolio-agent`, environment prefix `PF_`) are still read, so
nothing has to be re-entered; anything saved from now on goes under `kosh` / `KOSH_`.

Keys:  imap:<account>            app password for a mailbox
       id:name|dob|pan|mobile|password   identity values used to build PDF passwords
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

SERVICE = "kosh"
LEGACY_SERVICE = "portfolio-agent"          # read-only fallback


def _env_name(key: str, prefix: str = "KOSH_") -> str:
    return prefix + re.sub(r"\W+", "_", key).strip("_").upper()


class _FileStore:
    """Secrets in data/secrets.json (mode 0600), for containers and headless machines that have no OS keyring.
    Chosen only by KOSH_SECRET_STORE=file: plain text on disk, protected by file permissions and by whoever can read
    the data folder, so it is no substitute for a keyring on a shared machine."""

    def __init__(self, path):
        self.path = path

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    def get_password(self, service: str, key: str) -> str | None:
        return self._load().get(f"{service}:{key}")

    def set_password(self, service: str, key: str, value: str) -> None:
        data = self._load()
        data[f"{service}:{key}"] = value
        self._save(data)

    def delete_password(self, service: str, key: str) -> None:
        data = self._load()
        data.pop(f"{service}:{key}", None)
        self._save(data)


def _keyring():
    if os.getenv("KOSH_SECRET_STORE") == "file":
        return _FileStore(Path(__file__).resolve().parent.parent / "data" / "secrets.json")
    try:
        import keyring
        return keyring
    except Exception:
        return None


def get_secret(key: str) -> str | None:
    kr = _keyring()
    if kr:
        for service in (SERVICE, LEGACY_SERVICE):
            try:
                value = kr.get_password(service, key)
                if value:
                    return value
            except Exception:
                pass
    value = os.getenv(_env_name(key)) or os.getenv(_env_name(key, "PF_"))
    if not value and key == "id:password":
        return get_secret("id:cas_password") or os.getenv("CAS_PASSWORD") or None   # the name it had when it was CAS-only
    return value or None


def has_secret(key: str) -> bool:
    return get_secret(key) is not None


def set_secret(key: str, value: str) -> None:
    kr = _keyring()
    if not kr:
        raise RuntimeError("keyring is not installed; set the KOSH_* environment variable instead")
    kr.set_password(SERVICE, key, value)


def delete_secret(key: str) -> None:
    kr = _keyring()
    if kr:
        for service in (SERVICE, LEGACY_SERVICE):          # both, or a deleted secret would come back from the old entry
            try:
                kr.delete_password(service, key)
            except Exception:
                pass
