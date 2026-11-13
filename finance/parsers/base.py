"""Parser plugin interface: one class per institution, registered by name."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from finance.models import ParsedStatement


class Parser(Protocol):
    name: str

    def parse(self, path: Path, password: str | None) -> ParsedStatement: ...


REGISTRY: dict[str, Parser] = {}


def register(parser: Parser) -> Parser:
    REGISTRY[parser.name] = parser
    return parser
