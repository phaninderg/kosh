"""Small named files (the statement inbox, mailbox settings): sealed in the signed-in profile's vault, or plain files in a
directory for tests and tools. The vault form is what the app always uses."""

from __future__ import annotations

import os
from pathlib import Path

from finance import vault


class Blobs:
    def __init__(self, location: str | Path, prefix: str = ""):
        self.vaulted = str(location) == vault.VAULT_FILES
        self.dir, self.prefix = (None if self.vaulted else Path(location)), prefix

    def write(self, name: str, data: bytes) -> None:
        if self.vaulted:
            vault.require_vault().put(self.prefix + name, data)
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.dir / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)

    def read(self, name: str) -> bytes | None:
        if self.vaulted:
            return vault.require_vault().get(self.prefix + name)
        try:
            return (self.dir / name).read_bytes()
        except OSError:
            return None

    def exists(self, name: str) -> bool:
        return self.read(name) is not None

    def delete(self, name: str) -> None:
        if self.vaulted:
            vault.require_vault().delete(self.prefix + name)
        else:
            (self.dir / name).unlink(missing_ok=True)
