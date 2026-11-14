"""Settings, from environment variables (a `.env` file next to this one is read if python-dotenv is installed)."""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

_ROOT = Path(__file__).parent

# Everything a person stores lives under data/ (never committed): data/users.json lists the profiles, and each profile's
# ledger, secrets, mailbox settings, statements and logs are sealed under its own key in data/users/<id>/.

# 127.0.0.1 unless set: the Docker image sets KOSH_HOST=0.0.0.0 inside the container and publishes the port on the host's
# loopback only (see docker-compose.yml). Never expose the port on a network.
UI_HOST = os.getenv("KOSH_HOST", "127.0.0.1")
UI_PORT = int(os.getenv("UI_PORT", "8765"))
# KOSH_ALLOWED_HOSTS names the domain(s) the app answers to, e.g. "kosh.example.com"; KOSH_TRUST_PROXY=1
# says a reverse proxy (Caddy, nginx, Tailscale serve) in front is the only thing that can reach the app.
ALLOWED_HOSTS = [h.strip().lower() for h in os.getenv("KOSH_ALLOWED_HOSTS", "").split(",") if h.strip()]
TRUST_PROXY = os.getenv("KOSH_TRUST_PROXY") == "1"
PUBLIC_PORT = int(os.getenv("KOSH_PUBLIC_PORT", UI_PORT))     # the port your browser uses, when a container publishes it elsewhere
