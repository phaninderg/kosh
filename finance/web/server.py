"""Start the web app. Everything else (creating profiles, signing in, importing statements) is done in the browser."""

from __future__ import annotations

import os
import webbrowser

import uvicorn

from config import ALLOWED_HOSTS, PUBLIC_PORT, TRUST_PROXY, UI_HOST, UI_PORT
from finance import users as users_mod
from finance.web.app import create_app


# Only addresses that cannot be on the public internet may set X-Forwarded-For / -Proto: the local machine (Tailscale serve,
# a proxy on the same host) and private networks (the Caddy container on the compose network).
TRUSTED_PROXIES = "127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"


def serve() -> None:
    users = users_mod.Users()
    shown = "localhost" if UI_HOST == "0.0.0.0" else UI_HOST
    base = f"https://{ALLOWED_HOSTS[0]}" if ALLOWED_HOSTS else f"http://{shown}:{PUBLIC_PORT}"
    setup_code = users.new_setup_code()                   # None once someone has signed up
    print(f"\nKosh is running at {base}", flush=True)
    if setup_code:
        print(f"\nFirst time here? Open {base}/signup and enter this setup code:\n\n    {setup_code}\n"
              f"\n(Only whoever can read this message can create the first profile, who becomes the admin.)\n", flush=True)
    else:
        print("Sign in in the browser. Your data is encrypted; enter your password to unlock it after a restart.\n", flush=True)
    if not ALLOWED_HOSTS and not os.getenv("KOSH_NO_BROWSER"):
        try:
            webbrowser.open(base + ("/signup" if setup_code else "/"))
        except Exception:
            pass
    app = create_app(users=users)
    app.state.autosync.start()                            # per-mailbox automatic sync, only while a person's data is unlocked
    uvicorn.run(app, host=UI_HOST, port=UI_PORT, log_level="warning",
                proxy_headers=TRUST_PROXY, forwarded_allow_ips=TRUSTED_PROXIES if TRUST_PROXY else "127.0.0.1")
