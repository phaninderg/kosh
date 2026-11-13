"""Import orchestration for one statement file: hash -> parse -> ledger."""

from __future__ import annotations

from pathlib import Path

from config import DB_PATH
from finance.hashing import file_sha256
from finance.ledger import Ledger
from finance.models import ImportResult
from finance.parsers.base import REGISTRY
import finance.parsers.cas  # noqa: F401  (registers the parser)
import finance.parsers.icici  # noqa: F401  (registers the parser)
import finance.parsers.icici_card  # noqa: F401  (registers the parser)
import finance.parsers.hdfc_card  # noqa: F401  (registers the parser)
import finance.parsers.axis_card  # noqa: F401  (registers the parser)
import finance.parsers.tata_card  # noqa: F401  (registers the parser)
import finance.parsers.nps  # noqa: F401  (registers the parser)


# What the user picks on the Import page -> the parsers tried for it, in order. Each refuses a statement that is not
# its own, so the first that accepts it wins. EPF has no parser yet (phase 4).
STATEMENT_TYPES: dict[str, list[str]] = {
    "bank": ["icici"],
    "cas": ["cas"],
    "card": ["icici_card", "hdfc_card", "axis_card", "tata_card"],
    "epf": [],
    "nps": ["nps"],
}
TYPE_LABELS = {"bank": "Bank statement", "cas": "Consolidated CAS (mutual funds)", "card": "Credit card",
               "epf": "EPF", "nps": "NPS"}
DETECT_ORDER = [p for names in STATEMENT_TYPES.values() for p in names]      # "auto": every parser we have
_INTEGRITY = ("add up", "statement says", "printed balance", "page totals", "does not balance", "disagree")


def _parse(path: Path, parser_name: str, password: str | None):
    """(statement, parser name, error). `auto` tries every parser; if none accepts the file, the error is the one from a
    parser that recognised the layout but refused the figures, since that is the useful one to show."""
    if parser_name in REGISTRY:
        return REGISTRY[parser_name].parse(path, password), parser_name
    if parser_name in STATEMENT_TYPES and not STATEMENT_TYPES[parser_name]:
        raise ValueError(f"{TYPE_LABELS[parser_name]} statements are not supported yet.")
    candidates = STATEMENT_TYPES.get(parser_name, DETECT_ORDER)
    if len(candidates) == 1 and candidates[0] in REGISTRY:       # one parser for this type: its own message is the useful one
        return REGISTRY[candidates[0]].parse(path, password), candidates[0]
    errors: dict[str, Exception] = {}
    for name in (n for n in candidates if n in REGISTRY):
        try:
            return REGISTRY[name].parse(path, password), name
        except Exception as exc:                               # a parser that does not recognise the file refuses it
            errors[name] = exc
    name, exc = next(((n, e) for n, e in errors.items() if any(k in str(e) for k in _INTEGRITY)),
                     (candidates[-1], ValueError(f"This file was not recognised as the selected statement type ({TYPE_LABELS.get(parser_name, 'any')}).")))
    raise _AutoFailed(name, exc)


class _AutoFailed(Exception):
    def __init__(self, parser: str, cause: Exception):
        super().__init__(str(cause))
        self.parser, self.cause = parser, cause


def import_any(path: str | Path, parser_name: str = "auto", password: str | None = None,
               db_path: str = DB_PATH, original_name: str | None = None) -> tuple[ImportResult, str]:
    """Import one statement file; returns (result, the parser that read it). Raises the parser's error if refused."""
    from finance.passwords import Identity
    from finance.redact import make_redactor, redact
    path = Path(path)
    sha = file_sha256(path)
    ident = Identity()
    meta = {"account": "upload", "size": path.stat().st_size,
            "filename": redact(original_name or path.name, ident.get("name"), ident.get("pan"), ident.get("mobile"))}
    ledger = Ledger(db_path)
    try:
        try:
            stmt, used = _parse(path, parser_name, password)
        except Exception as exc:
            failed_parser, err = (exc.parser, exc.cause) if isinstance(exc, _AutoFailed) else (parser_name, exc)
            # Never include the password or file content; the error text comes from the parser.
            ledger.record_failure(sha256=sha, parser=failed_parser, error=type(err).__name__ + ": " + str(err))
            raise err
        return ledger.import_statement(
            stmt, sha256=sha, meta=meta,
            redactor=make_redactor(ident.get("name"), ident.get("pan"), ident.get("mobile"))), used
    finally:
        ledger.close()


def import_file(path: str | Path, parser_name: str = "cas", password: str | None = None,
                db_path: str = DB_PATH, original_name: str | None = None) -> ImportResult:
    return import_any(path, parser_name, password, db_path, original_name)[0]
