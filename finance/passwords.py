"""Decrypt Agent: unlock password-protected statement PDFs.

Passwords are *templates* over identity values held in the keyring/env, e.g. {name4}{ddmm}.
Candidates are tried in order (cached working template for the sender, the sender's own
templates, then the defaults). The resolved password exists only in memory.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime

from pydantic import BaseModel, SecretStr

from finance import secrets

_TOKEN = re.compile(r"\{([A-Za-z0-9_]+)\}")
_SIMPLE = {"ddmm", "ddmmyy", "ddmmyyyy", "yyyy", "pan", "PAN", "password", "cas_password", "card4"}
_PARAM = re.compile(r"(name|NAME|mobile)(\d{1,2})$")
CARD4_SPACE = 10000          # every 4-digit card ending; the last-resort search for a {card4} template


class MissingValue(Exception):
    def __init__(self, fields: list[str]):
        super().__init__("missing identity value(s): " + ", ".join(fields))
        self.fields = fields


def validate_template(template: str) -> str:
    if not template or len(template) > 100:
        raise ValueError("template must be 1-100 characters")
    tokens = _TOKEN.findall(template)
    if not tokens:
        raise ValueError("template needs at least one {token}")
    for t in tokens:
        if t not in _SIMPLE and not _PARAM.match(t):
            raise ValueError(f"unknown token {{{t}}}. Valid: name4 NAME4 mobile5 ddmm ddmmyy ddmmyyyy yyyy pan PAN card4 password")
    return template


class Identity:
    """Reads identity values lazily from the keyring/env."""

    def __init__(self, values: dict[str, str] | None = None):
        self._values = values

    def get(self, field: str) -> str | None:
        if self._values is not None:
            return self._values.get(field)
        return secrets.get_secret(f"id:{field}")

    def extra_passwords(self) -> list[str]:
        """Literal passwords the user chose to remember (statements requested with a custom password)."""
        import json
        if self._values is not None and "extra_passwords" in self._values:
            return list(self._values["extra_passwords"])
        try:
            return [str(x) for x in json.loads(secrets.get_secret("id:extra_passwords") or "[]")]
        except ValueError:
            return []

    def dob(self) -> date | None:
        raw = self.get("dob")
        if not raw:
            return None
        for fmt in ("%Y-%m-%d", "%d%m%Y", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                pass
        return None

    def card4_values(self) -> list[str]:
        """Last 4 digits of cards the user chose to remember (comma separated); ledger cards are added by the caller."""
        return [d for d in re.split(r"[\s,]+", self.get("card4") or "") if re.fullmatch(r"\d{4}", d)]

    def remember_card4(self, digits: str) -> None:
        """Keep a card ending found by search so the next statement unlocks at once (keyring/env only)."""
        if self._values is not None or digits in self.card4_values():
            return
        secrets.set_secret("id:card4", ",".join(self.card4_values() + [digits]))

    def resolve(self, template: str, card4: str = "") -> str:
        missing: list[str] = []

        def sub(m: re.Match) -> str:
            tok = m.group(1)
            if tok in ("ddmm", "ddmmyy", "ddmmyyyy", "yyyy"):
                d = self.dob()
                if not d:
                    missing.append("dob"); return ""
                return {"ddmm": d.strftime("%d%m"), "ddmmyy": d.strftime("%d%m%y"),
                        "ddmmyyyy": d.strftime("%d%m%Y"), "yyyy": d.strftime("%Y")}[tok]
            if tok == "card4":
                if not card4:
                    missing.append("card4"); return ""
                return card4
            if tok in ("pan", "PAN"):
                v = self.get("pan")
                if not v:
                    missing.append("pan"); return ""
                return v.upper() if tok == "PAN" else v.lower()
            if tok in ("password", "cas_password"):                  # cas_password: the old name of the same field
                v = self.get("password")
                if not v:
                    missing.append("password"); return ""
                return v
            kind, n = _PARAM.match(tok).groups()
            if kind == "mobile":
                v = re.sub(r"\D", "", self.get("mobile") or "")
                if not v:
                    missing.append("mobile"); return ""
                return v[-int(n):]
            letters = re.sub(r"[^A-Za-z]", "", self.get("name") or "")
            if not letters:
                missing.append("name"); return ""
            part = letters[: int(n)]
            return part.upper() if kind == "NAME" else part.lower()

        out = _TOKEN.sub(sub, template)
        if missing:
            raise MissingValue(sorted(set(missing)))
        return out


FALLBACK_TEMPLATES = ["{password}"]       # always tried last: the default password from Identity, if one is set


class UnlockResult(BaseModel):
    ok: bool
    encrypted: bool = False
    password: SecretStr | None = None      # masked in repr/logs
    template: str | None = None
    card4: str | None = None               # the card ending a {card4} template matched (not secret by itself)
    reason: str = ""


def pdf_opens(pdf: bytes, password: str) -> tuple[bool, bool]:
    """(opens_with_password, is_encrypted). Uses pdfminer, already installed with casparser."""
    from pdfminer.pdfdocument import PDFDocument, PDFPasswordIncorrect
    from pdfminer.pdfparser import PDFParser
    try:
        doc = PDFDocument(PDFParser(io.BytesIO(pdf)), password)
    except PDFPasswordIncorrect:
        return False, True
    return True, doc.encryption is not None


class DecryptAgent:
    def __init__(self, identity: Identity | None = None, cache: dict[str, str] | None = None,
                 card4s: list[str] | None = None):
        self.identity = identity or Identity()
        self.card4s = list(dict.fromkeys((card4s or []) + self.identity.card4_values()))   # known cards first
        self.cache = cache or {}                       # sender -> last working template

    def unlock(self, pdf: bytes, sender: str, templates: list[str],
               defaults: list[str] | None = None, priority: list[str] | None = None) -> UnlockResult:
        """Order: `priority` literals (a password the user just typed), cached template, the sender's
        templates, the defaults, then remembered literal passwords."""
        defaults = FALLBACK_TEMPLATES if defaults is None else defaults
        ok, encrypted = pdf_opens(pdf, "")
        if ok and not encrypted:
            return UnlockResult(ok=True, encrypted=False)

        ordered: list[str] = []
        for t in ([self.cache[sender]] if sender in self.cache else []) + templates + defaults:
            if t not in ordered:
                ordered.append(t)

        for pw in priority or []:
            if pdf_opens(pdf, pw)[0]:
                return UnlockResult(ok=True, encrypted=True, password=SecretStr(pw), template=None)

        tried, missing = 0, set()

        def candidates(t: str, digits: list[str]):
            for d in digits if "{card4}" in t else [""]:
                yield d, self.identity.resolve(t, d)

        for t in ordered:                               # a {card4} template tries the known cards first
            try:
                for d, pw in candidates(t, self.card4s):
                    tried += 1
                    if pdf_opens(pdf, pw)[0]:
                        return UnlockResult(ok=True, encrypted=True, password=SecretStr(pw), template=t, card4=d or None)
            except MissingValue as exc:
                missing.update(exc.fields)
        for t in (t for t in ordered if "{card4}" in t):  # last resort, a new card: every 4-digit ending, once
            seen = set(self.card4s)
            try:
                for n in range(CARD4_SPACE):
                    d = f"{n:04d}"
                    if d in seen:
                        continue
                    tried += 1
                    pw = self.identity.resolve(t, d)
                    if pdf_opens(pdf, pw)[0]:
                        return UnlockResult(ok=True, encrypted=True, password=SecretStr(pw), template=t, card4=d)
            except MissingValue as exc:
                missing.update(exc.fields)

        saved = self.identity.extra_passwords()
        for pw in saved:
            tried += 1
            if pdf_opens(pdf, pw)[0]:
                return UnlockResult(ok=True, encrypted=True, password=SecretStr(pw), template=None)

        reason = f"PDF is password-protected; tried {tried} password candidate(s), none worked."
        if missing:
            reason += f" Templates skipped because these values are not set: {', '.join(sorted(missing))}."
        if not ordered and not saved:
            reason = "PDF is password-protected and no password templates are configured."
        return UnlockResult(ok=False, encrypted=True, reason=reason)
