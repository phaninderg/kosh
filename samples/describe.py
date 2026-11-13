"""Summarise what a parser read from a statement, in the form `manifest.json` stores.

    python -m samples.describe path/to/statement.pdf --parser icici [--password SECRET]

Prints the manifest entry to paste. Run it only on a file you are sure contains no real data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import finance.importer  # noqa: F401  (registers every parser)
from finance.parsers.base import REGISTRY


def summarize(st) -> dict:
    """What the sample must keep producing: how many accounts/schemes, rows, and the closing figures."""
    if hasattr(st, "accounts"):
        return {"accounts": [{"type": a.type, "last4": a.identifier[-4:], "rows": len(a.txns),
                              "opening": str(a.opening_balance), "closing": str(a.closing_balance)} for a in st.accounts],
                "deposits": len(st.deposits), "warnings": len(st.warnings)}
    return {"folios": [{"folio": f.folio[-4:], "schemes": len(f.schemes), "rows": sum(len(s.txns) for s in f.schemes),
                        "value": str(sum((s.value or 0) for s in f.schemes))} for f in st.folios],
            "warnings": len(st.warnings)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--parser", required=True, choices=sorted(REGISTRY))
    ap.add_argument("--password")
    a = ap.parse_args()
    st = REGISTRY[a.parser].parse(a.pdf, a.password)
    print(json.dumps({"file": a.pdf.name, "parser": a.parser, "password": a.password, "expect": summarize(st)}, indent=2))


if __name__ == "__main__":
    main()
