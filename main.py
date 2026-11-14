"""
Kosh: a self-hosted personal finance tracker for Indian assets. Entry point.

  python main.py ui      start the web app (everything else, from creating a profile to importing statements,
                         is done in the browser)
"""

from __future__ import annotations

import os
import sys

COMMANDS = ("ui",)


def main(argv: list[str]) -> int:
    os.umask(0o077)                      # data files are readable by you alone, whatever the shell's umask
    command = argv[0] if argv else ""
    if command not in COMMANDS:
        print(__doc__)
        return 0 if not command or command in ("-h", "--help", "help") else 1
    from finance.web.server import serve
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
