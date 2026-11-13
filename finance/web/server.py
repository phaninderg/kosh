"""Launch the local UI: loopback only by default, per-launch access token."""

from __future__ import annotations

import os
import secrets
import webbrowser

import uvicorn

from config import PUBLIC_PORT, UI_HOST, UI_PORT
from finance.web.app import create_app


def scan_for_duplicates() -> None:
    """Queue (never apply) duplicate-folio / duplicate-transaction findings so the Portfolio page can warn."""
    from config import DB_PATH
    from finance.ledger import Ledger
    try:
        led = Ledger(DB_PATH)
        try:
            led.flag_duplicate_accounts()
            led.flag_duplicate_transactions()
        finally:
            led.close()
    except Exception as exc:                      # a failed scan must never stop the UI from starting
        print(f"(duplicate scan skipped: {type(exc).__name__})")


def serve() -> None:
    scan_for_duplicates()
    token = secrets.token_urlsafe(24)
    shown = "localhost" if UI_HOST == "0.0.0.0" else UI_HOST             # inside a container: what the host's browser types
    url = f"http://{shown}:{PUBLIC_PORT}/?token={token}"
    print(f"\nKosh UI → {url}\n(bound to {UI_HOST}; Ctrl+C to stop)\n", flush=True)
    if not os.getenv("KOSH_NO_BROWSER"):
        try:
            webbrowser.open(url)
        except Exception:
            pass
    uvicorn.run(create_app(token), host=UI_HOST, port=UI_PORT, log_level="warning")
