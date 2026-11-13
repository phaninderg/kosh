"""Mask personal identifiers in free text (email subjects, attachment names) before it is stored."""

from __future__ import annotations

import re

_PAN = re.compile(r"\b[A-Za-z]{5}\d{4}[A-Za-z]\b")
_LONG_DIGITS = re.compile(r"\d[\d\s-]{6,}\d")           # phone, account, folio-like runs


def redact(text: str, name: str | None = None, pan: str | None = None, mobile: str | None = None,
           limit: int = 200) -> str:
    out = text or ""
    for part in re.split(r"\s+", (name or "").strip()):
        if len(part) >= 3:
            out = re.sub(re.escape(part), "•••", out, flags=re.I)
    if pan:
        out = re.sub(re.escape(pan), "•••", out, flags=re.I)
    digits = re.sub(r"\D", "", mobile or "")
    if len(digits) >= 6:
        out = re.sub(re.escape(digits[-10:]), "•••", out)
    out = _PAN.sub("•••", out)
    out = _LONG_DIGITS.sub("•••", out)
    return out[:limit]


def make_redactor(name: str | None = None, pan: str | None = None, mobile: str | None = None, limit: int = 300):
    """A one-argument masker bound to the user's own identifiers, for text the ledger stores (descriptions)."""
    return lambda text: redact(text, name, pan, mobile, limit)
