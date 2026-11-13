"""Settings, from environment variables (a `.env` file next to this one is read if python-dotenv is installed)."""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

_ROOT = Path(__file__).parent

# CAS password for the command line and the Import page, when it is not typed or built from your Identity page.
CAS_PASSWORD = os.getenv("CAS_PASSWORD", "")

# Where things live. Everything under data/ stays on your machine and is never committed.
DB_PATH          = os.getenv("FINANCE_DB_PATH", str(_ROOT / "data" / "finance.db"))
MAIL_CONFIG_PATH = str(_ROOT / "data" / "mailboxes.json")
INBOX_DIR        = str(_ROOT / "data" / "inbox")                # encrypted attachments awaiting attention
BACKUP_DIR       = str(_ROOT / "data" / "backups")
JOBS_DIR         = str(_ROOT / "data" / "jobs")                 # logs of runs started from the UI (mail sync)

# 127.0.0.1 unless set: the Docker image sets KOSH_HOST=0.0.0.0 inside the container and publishes the port on the host's
# loopback only (see docker-compose.yml). Never expose the port on a network.
UI_HOST = os.getenv("KOSH_HOST", "127.0.0.1")
UI_PORT = int(os.getenv("UI_PORT", "8765"))
PUBLIC_PORT = int(os.getenv("KOSH_PUBLIC_PORT", UI_PORT))     # the port your browser uses, when a container publishes it elsewhere
