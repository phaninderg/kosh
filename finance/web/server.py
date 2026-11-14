"""Launch the local UI: loopback only by default, per-launch access token."""

from __future__ import annotations

import os
import secrets
import webbrowser

import uvicorn

from config import ALLOWED_HOSTS, AUTH_MODE, PUBLIC_PORT, TRUST_PROXY, UI_HOST, UI_PORT
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
    if AUTH_MODE == "login":
        return serve_hosted()
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


def serve_hosted() -> None:
    """A hosted instance: a login instead of the per-launch token, and a refusal to start unprotected."""
    from finance.web import auth
    if auth.load_credentials() is None:
        raise SystemExit("KOSH_AUTH=login but no password is set. Run `python main.py set-password` first.")
    if not ALLOWED_HOSTS:
        raise SystemExit("KOSH_AUTH=login needs KOSH_ALLOWED_HOSTS, the domain people reach this on (e.g. kosh.example.com).")
    mfa = "password + authenticator code" if auth.load_credentials()[1] else "password only (add an authenticator with set-password)"
    print(f"\nKosh is serving {', '.join(ALLOWED_HOSTS)} on {UI_HOST}:{UI_PORT}; sign-in: {mfa}.", flush=True)
    uvicorn.run(create_app("", auth="login"), host=UI_HOST, port=UI_PORT, log_level="warning",
                proxy_headers=TRUST_PROXY, forwarded_allow_ips="*" if TRUST_PROXY else "127.0.0.1")
