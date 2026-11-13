"""Stable identity for NPS schemes. NPS has no ISIN and the same scheme is spelled differently over the years and between
the emailed and the portal statements (`ICICI PENSION FUND SCHEME E - TIER I POP`, `ICICI PRUDENTIAL PENSION FUND SCHEME E -
TIER I`), so a scheme is identified by its fund house, scheme letter and tier, not by its full name."""

from __future__ import annotations

import re

_SCHEME = re.compile(r"^(?P<house>[A-Z0-9]+)\b.*?\bSCHEME\s+(?P<letter>[A-Z])\s*-\s*TIER\s+(?P<tier>I{1,2})\b")
_NOISE = re.compile(r"\b(?:PRUDENTIAL|POP)\b")


def scheme_id(name: str) -> str:
    key = re.sub(r"\s+", " ", name).strip().upper()
    if (m := _SCHEME.match(key)):
        return f"NPS:{m.group('house')}-SCHEME-{m.group('letter')}-TIER-{m.group('tier')}"
    return "NPS:" + re.sub(r"[^A-Z0-9]+", "-", _NOISE.sub("", key)).strip("-")
