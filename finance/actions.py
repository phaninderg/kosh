"""Job entry point: `python -m finance.actions <action> [json-params]`.

The UI starts a mail sync as a subprocess so that each run has its own log and a crash cannot take the server down.
"""

from __future__ import annotations

import json
import sys

ACTIONS = ("sync",)


def run(action: str, params: dict) -> None:
    if action == "sync":
        from finance.cli import sync
        raise SystemExit(sync(params.get("accounts") or []))
    raise SystemExit(f"unknown action {action!r}; valid: {ACTIONS}")


if __name__ == "__main__":
    run(sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else {})
