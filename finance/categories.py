"""Mutual fund category from a scheme name (the first matching pattern wins, so more specific patterns come first)."""

from __future__ import annotations

import re

# Order matters: more specific patterns first to avoid mis-categorisation.
# E.g. "ELSS" before "large_cap" since ELSS funds can mention cap sizes.

_SEGMENT_PATTERNS: list[tuple[str, list[re.Pattern]]] = [
    ("elss", [
        re.compile(r"\belss\b", re.I),
        re.compile(r"tax\s*sav", re.I),
        re.compile(r"tax\s*gain", re.I),
    ]),
    ("gold", [
        re.compile(r"\bgold\b", re.I),
    ]),
    ("silver", [
        re.compile(r"\bsilver\b", re.I),
    ]),
    ("reit", [
        re.compile(r"\breit\b", re.I),
        re.compile(r"\binvit\b", re.I),
        re.compile(r"real\s*estate", re.I),
        re.compile(r"infrastructure\s*trust", re.I),
    ]),
    # Index BEFORE cap-based segments (many index fund names contain "midcap"/"smallcap")
    ("index", [
        re.compile(r"index\s*fund", re.I),
        re.compile(r"nifty\s*\d+\s*index", re.I),
        re.compile(r"sensex\s*index", re.I),
        re.compile(r"index\s*-?\s*direct", re.I),
        re.compile(r"\bindex\b.*\bgrowth\b", re.I),
    ]),
    ("small_cap", [
        re.compile(r"small\s*cap", re.I),
        re.compile(r"smallcap", re.I),
    ]),
    ("mid_cap", [
        re.compile(r"mid\s*cap", re.I),
        re.compile(r"midcap", re.I),
    ]),
    ("large_cap", [
        re.compile(r"large\s*cap", re.I),
        re.compile(r"largecap", re.I),
        re.compile(r"bluechip", re.I),
        re.compile(r"blue\s*chip", re.I),
    ]),
    ("flexi_cap", [
        re.compile(r"flexi\s*cap", re.I),
        re.compile(r"flexicap", re.I),
        re.compile(r"multi\s*cap", re.I),
        re.compile(r"multicap", re.I),
    ]),
    ("international", [
        re.compile(r"\bnasdaq\b", re.I),
        re.compile(r"s\s*&?\s*p\s*500", re.I),
        re.compile(r"\binternational\b", re.I),
        re.compile(r"\bglobal\b", re.I),
        re.compile(r"\bus\s*equity\b", re.I),
        re.compile(r"\bus\s*total\b", re.I),
        re.compile(r"hang\s*seng", re.I),
        re.compile(r"greater\s*china", re.I),
    ]),
    ("hybrid", [
        re.compile(r"aggressive\s*hybrid", re.I),
        re.compile(r"balanced\s*advantage", re.I),
        re.compile(r"equity\s*(&|and)\s*debt", re.I),
        re.compile(r"equity\s*hybrid", re.I),
        re.compile(r"dynamic\s*asset\s*alloc", re.I),
        re.compile(r"multi\s*asset\s*alloc", re.I),
    ]),
    ("debt", [
        re.compile(r"short\s*term\s*(debt|bond)", re.I),
        re.compile(r"corporate\s*bond", re.I),
        re.compile(r"banking\s*(&|and)\s*psu", re.I),
        re.compile(r"\bliquid\b", re.I),
        re.compile(r"ultra\s*short", re.I),
        re.compile(r"money\s*market", re.I),
        re.compile(r"\bovernight\b", re.I),
        re.compile(r"\bgilt\b", re.I),
        re.compile(r"dynamic\s*bond", re.I),
        re.compile(r"medium\s*(duration|term)", re.I),
        re.compile(r"long\s*duration", re.I),
        re.compile(r"credit\s*risk", re.I),
        re.compile(r"\bfloater\b", re.I),
        re.compile(r"conservative\s*hybrid", re.I),
        re.compile(r"low\s*duration", re.I),
    ]),
]


def categorize_fund(scheme_name: str) -> str | None:
    """Return segment key for a fund name, or None if uncategorised."""
    for segment, patterns in _SEGMENT_PATTERNS:
        for pat in patterns:
            if pat.search(scheme_name):
                return segment
    return None
