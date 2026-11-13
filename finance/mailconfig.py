"""Mailbox configuration (non-secret) in data/mailboxes.json. App passwords live in the keyring."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from config import MAIL_CONFIG_PATH

PROVIDERS = {"gmail": "imap.gmail.com", "yahoo": "imap.mail.yahoo.com"}
CAS_SENDERS = ("camsonline.com", "kfintech.com", "mfcentral.com")


class SenderRule(BaseModel):
    """Only mail whose From address matches `match` is fetched. `match` is a domain or full address."""
    match: str
    parser: str = "cas"
    templates: list[str] = Field(default_factory=list)      # PDF password templates for this sender

    @field_validator("match")
    @classmethod
    def _match(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.fullmatch(r"[a-z0-9@._+-]{3,100}", v):      # also keeps IMAP search strings injection-free
            raise ValueError("sender must be a domain or email address (letters, digits, . _ + - @)")
        return v

    @field_validator("parser")
    @classmethod
    def _parser(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9_]{1,40}", v):
            raise ValueError("bad parser name")
        return v

    @field_validator("templates")
    @classmethod
    def _templates(cls, v: list[str]) -> list[str]:
        from finance.passwords import validate_template
        for t in v:
            validate_template(t)
        return v

    def matches(self, from_addr: str) -> bool:
        addr = from_addr.strip().lower()
        if "@" in self.match:
            return addr == self.match
        domain = addr.rsplit("@", 1)[-1]
        return domain == self.match or domain.endswith("." + self.match)


class MailboxAccount(BaseModel):
    name: str
    provider: Literal["gmail", "yahoo", "custom"]
    username: str
    host: str | None = None
    port: int = 993
    folder: str = "INBOX"
    lookback_days: int = Field(365, ge=1, le=3650)         # only used on the first sync
    require_auth: bool = True                              # reject mail whose DKIM/SPF/DMARC explicitly failed
    senders: list[SenderRule] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", v):
            raise ValueError("name: letters, digits, - and _ only (max 40)")
        return v

    @field_validator("folder")
    @classmethod
    def _folder(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9 _./\[\]-]{1,80}", v):
            raise ValueError("unsupported characters in folder name")
        return v

    @property
    def imap_host(self) -> str:
        host = self.host or PROVIDERS.get(self.provider)
        if not host:
            raise ValueError("custom provider needs a host")
        return host

    @property
    def secret_key(self) -> str:
        return f"imap:{self.name}"


class MailConfig(BaseModel):
    accounts: list[MailboxAccount] = Field(default_factory=list)

    def account(self, name: str) -> MailboxAccount | None:
        return next((a for a in self.accounts if a.name == name), None)


def default_cas_rules() -> list[SenderRule]:
    return [SenderRule(match=d, parser="cas") for d in CAS_SENDERS]


def load_config(path: str = MAIL_CONFIG_PATH) -> MailConfig:
    try:
        return MailConfig.model_validate(json.loads(Path(path).read_text()))
    except (OSError, ValueError):
        return MailConfig()


def save_config(cfg: MailConfig, path: str = MAIL_CONFIG_PATH) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(cfg.model_dump_json(indent=2))
    os.replace(tmp, p)


def parse_sender_lines(text: str) -> list[SenderRule]:
    """One rule per line:  domain-or-address | parser | template ; template   (parser/templates optional)."""
    rules = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split("|")]
        try:
            rules.append(SenderRule(
                match=parts[0], parser=(parts[1] if len(parts) > 1 and parts[1] else "cas"),
                templates=[t.strip() for t in parts[2].split(";") if t.strip()] if len(parts) > 2 else []))
        except ValueError as exc:
            raise ValueError(f"line {n}: {exc}") from None
    return rules


def format_sender_lines(rules: list[SenderRule]) -> str:
    return "\n".join(f"{r.match} | {r.parser} | {' ; '.join(r.templates)}".rstrip(" |") for r in rules)
