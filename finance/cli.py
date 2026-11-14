"""Command-line front ends for sync and the review queue (the UI calls the same functions)."""

from __future__ import annotations

from config import DB_PATH
from finance import analytics
from finance.db import connect_ro


def sync(accounts: list[str]) -> int:
    from finance.mailconfig import load_config
    from finance.sync import SyncRunner
    cfg = load_config()
    if not cfg.accounts:
        print("No mailboxes configured. Add one in the UI (Mailboxes page) first.")
        return 1
    summary = SyncRunner(config=cfg).run(accounts or None)
    print("\nSync summary\n" + summary.render())
    return 1 if any(a.error for a in summary.accounts) else 0


def import_statement(path: str, parser: str, templates: list[str] | None = None) -> int:
    """Import a bank statement PDF. The password is found by the Decrypt Agent from your Identity templates,
    so it never appears on the command line. `templates` (e.g. '{name4}{ddmm}') are tried first, then your default password."""
    from pathlib import Path
    from finance.importer import import_file
    from finance.mailconfig import load_config
    from finance.parsers.base import REGISTRY
    from finance.passwords import DecryptAgent
    if parser not in REGISTRY:
        print(f"Unknown parser '{parser}'. Available: {', '.join(sorted(REGISTRY))}")
        return 1
    pdf = Path(path)
    if not pdf.is_file():
        print(f"No such file: {path}")
        return 1
    unlocked = DecryptAgent().unlock(pdf.read_bytes(), "", templates or [])
    if not unlocked.ok:
        print(unlocked.reason)
        return 1
    password = unlocked.password.get_secret_value() if unlocked.password else None
    try:
        res = import_file(pdf, parser_name=parser, password=password)
    except ValueError as exc:                    # parser or balance check refused the statement
        print(f"Not imported: {exc}")
        return 1
    print(f"{res.status}: {res.txn_new} new transaction(s), {res.txn_dup} already known")
    for w in res.warnings:
        print(f"  ⚠ {w}")
    return 0


def review(argv: list[str]) -> int:
    if not argv:
        conn = connect_ro(DB_PATH)
        items = analytics.open_review_items(conn)
        if not items:
            print("Review queue is empty.")
        for i in items:
            print(f"[{i['id']}] {i['kind']}: {i['reason']}")
        if items:
            print("\nAct with: python main.py review <id> retry|dismiss|close|keep|merge|separate|clean|resolve")
        return 0

    if argv[0] == "scan":
        from finance.ledger import Ledger
        led = Ledger(DB_PATH)
        try:
            found = led.flag_duplicate_accounts() + led.flag_duplicate_transactions()
        finally:
            led.close()
        print(f"{len(found)} new finding(s) queued. List them with: python main.py review")
        return 0

    if argv[0] == "merge-all":
        from config import BACKUP_DIR
        from finance.ledger import Ledger
        led = Ledger(DB_PATH)
        try:
            print("backup:", led.backup(BACKUP_DIR))
            print(led.merge_all_duplicates())
        finally:
            led.close()
        return 0

    item_id, action = int(argv[0]), argv[1] if len(argv) > 1 else ""
    conn = connect_ro(DB_PATH)
    row = conn.execute("SELECT * FROM review_queue WHERE id=? AND status='open'", (item_id,)).fetchone()
    if not row:
        print("No such open item.")
        return 1
    from finance.ledger import Ledger
    if row["kind"] == "needs_attention" and action in ("retry", "dismiss"):
        from finance.sync import SyncRunner
        doc_id = int(row["ref"].split(":")[1])
        runner = SyncRunner()
        if action == "retry":
            print(runner.retry(doc_id))
        else:
            runner.dismiss(doc_id); print("dismissed")
        return 0
    ledger = Ledger(DB_PATH)
    try:
        if row["kind"] == "duplicate_folio" and action in ("merge", "separate"):
            if action == "separate":
                ledger.keep_separate(item_id)
            else:
                from config import BACKUP_DIR
                print("backup:", ledger.backup(BACKUP_DIR))
                _, pa, pb = row["ref"].split(":")
                print(ledger.merge_accounts(int(pa), int(pb)))
        elif row["kind"] == "duplicate_txns" and action == "clean":
            from config import BACKUP_DIR
            print("backup:", ledger.backup(BACKUP_DIR))
            print("removed:", ledger.remove_duplicate_transactions())
        elif row["kind"] == "folio_missing" and action in ("close", "keep"):
            ledger.resolve_folio_missing(item_id, close=(action == "close"))
        elif action == "resolve":
            ledger.resolve_review(item_id)
        else:
            print(f"'{action}' is not valid for a {row['kind']} item.")
            return 1
    finally:
        ledger.close()
    print("done")
    return 0


def set_password(totp: bool = True) -> int:
    """Create or replace the login for a hosted instance. Replacing it signs every device out."""
    import getpass
    from finance.web import auth
    first = getpass.getpass("New password (12+ characters): ")
    if len(first) < 12:
        print("Too short: use at least 12 characters (a few random words is fine).")
        return 1
    if getpass.getpass("Again: ") != first:
        print("The two entries differ; nothing was changed.")
        return 1
    secret = auth.new_totp_secret() if totp else None
    auth.save_credentials(auth.hash_password(first), secret)
    auth.rotate_session_key()
    print("Password saved (stored as a salted scrypt hash in data/auth.json).")
    if secret:
        print("\nAdd Kosh to your authenticator app (Google Authenticator, Authy, 1Password...):")
        print(f"  key:  {secret}")
        print(f"  link: {auth.totp_uri(secret)}")
        print("This is shown once. Enter a code from the app each time you sign in.")
    else:
        print("No authenticator code: anyone with the password can sign in. Use this only behind a private network.")
    return 0
