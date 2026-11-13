"""Source plugin interface. IMAP is the first implementation; others can follow without
touching the sync runner."""

from __future__ import annotations

from typing import Iterator, Protocol

from finance.models import SourceMessage


class Source(Protocol):
    name: str

    def fetch_new(self) -> Iterator[SourceMessage]:
        """Yield not-yet-seen messages in ascending order."""

    def commit(self, msg: SourceMessage) -> None:
        """Mark `msg` (and everything before it) as handled. Called only after it was recorded."""

    def close(self) -> None: ...


class MailboxError(Exception):
    """Connection/auth/protocol problem. Messages never contain secrets."""
