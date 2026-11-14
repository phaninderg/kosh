"""
Kosh: a self-hosted personal finance tracker for Indian assets. Entry point.

Commands:
  python main.py ui                          — the local web UI (net worth overview, accounts, transactions, import, ...)
  python main.py import-cas <pdf>            — import a mutual fund CAS PDF into the ledger (idempotent)
  python main.py import-statement <pdf> [--parser=icici|icici_card|hdfc_card|axis_card|tata_card|nps] [--template={name4}{ddmm}]
                                             — import a bank, card or NPS statement PDF
  python main.py sync [--account=NAME]       — fetch new statements from your mailboxes (read-only IMAP)
  python main.py review [id action]          — list / act on the review queue (retry|dismiss|close|keep|merge|separate|clean|resolve)
  python main.py set-password [--no-totp]    — set the login password (and an authenticator code) for a hosted instance
  python main.py review scan | merge-all     — find duplicates in existing data / merge every open duplicate-folio pair
"""

from __future__ import annotations

import os
import sys

COMMANDS = ("ui", "import-cas", "import-statement", "sync", "review", "set-password")


def main(argv: list[str]) -> int:
    os.umask(0o077)                      # the ledger, logs and backups are readable by you alone, whatever the shell's umask
    command = argv[0] if argv else ""
    if command not in COMMANDS:
        print(__doc__)
        return 0 if not command or command in ("-h", "--help", "help") else 1

    if command == "ui":
        from finance.web.server import serve
        serve()
        return 0
    if command == "set-password":
        from finance.cli import set_password
        return set_password(totp="--no-totp" not in argv)
    if command == "import-cas":
        from config import CAS_PASSWORD
        from finance.importer import import_file
        if len(argv) < 2:
            print("Usage: python main.py import-cas <pdf>")
            return 1
        res = import_file(argv[1], password=CAS_PASSWORD)
        print(res.model_dump_json(indent=2))
        return 0
    if command == "import-statement":
        from finance.cli import import_statement
        files = [a for a in argv[1:] if not a.startswith("--")]
        if not files:
            print("Usage: python main.py import-statement <pdf> [--parser=NAME] [--template='{name4}{ddmm}']")
            return 1
        parser = next((a.split("=", 1)[1] for a in argv[1:] if a.startswith("--parser=")), "icici")
        templates = [a.split("=", 1)[1] for a in argv[1:] if a.startswith("--template=")]
        return import_statement(files[0], parser, templates)
    if command == "sync":
        from finance.cli import sync
        return sync([a.split("=", 1)[1] for a in argv[1:] if a.startswith("--account=")])
    from finance.cli import review
    return review(argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
