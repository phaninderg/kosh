"""SQLite schema and connections. Only finance.ledger writes; everything else opens read-only."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from finance import vault

SCHEMA_VERSION = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY,
    type        TEXT NOT NULL CHECK (type IN ('bank','card','mutual_fund','nps','epf','ppf')),
    institution TEXT NOT NULL,
    name        TEXT NOT NULL,
    mask        TEXT NOT NULL DEFAULT '',          -- last 4 only
    ref_hash    TEXT NOT NULL,                     -- keyed hash of the full identifier
    liability   INTEGER NOT NULL DEFAULT 0,
    currency    TEXT NOT NULL DEFAULT 'INR',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (type, ref_hash)
);

CREATE TABLE IF NOT EXISTS source_documents (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,                     -- upload | imap
    message_id  TEXT NOT NULL DEFAULT '',
    sha256      TEXT NOT NULL,
    parser      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL,                     -- imported | duplicate | failed | needs_attention
    period_from TEXT,
    period_to   TEXT,
    txn_new     INTEGER NOT NULL DEFAULT 0,
    txn_dup     INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (message_id, sha256)
);

CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    doc_id      INTEGER NOT NULL REFERENCES source_documents(id),
    isin        TEXT NOT NULL DEFAULT '',
    scheme_name TEXT NOT NULL DEFAULT '',
    date        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    amount      TEXT,                              -- Decimal as text
    units       TEXT,
    nav         TEXT,
    balance     TEXT,
    idem_key    TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_txn_acct_isin_date ON transactions (account_id, isin, date);

CREATE TABLE IF NOT EXISTS transfer_links (
    id          INTEGER PRIMARY KEY,
    txn_out     INTEGER NOT NULL REFERENCES transactions(id),
    txn_in      INTEGER NOT NULL REFERENCES transactions(id),
    method      TEXT NOT NULL,
    confidence  TEXT NOT NULL,
    UNIQUE (txn_out, txn_in)
);

CREATE TABLE IF NOT EXISTS holdings (
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    isin        TEXT NOT NULL,
    scheme_name TEXT NOT NULL,
    amfi_code   TEXT NOT NULL DEFAULT '',
    as_of       TEXT NOT NULL,
    units       TEXT NOT NULL,
    nav         TEXT NOT NULL,
    cost        TEXT,
    value       TEXT NOT NULL,
    doc_id      INTEGER NOT NULL REFERENCES source_documents(id),
    PRIMARY KEY (account_id, isin, as_of)
);

CREATE TABLE IF NOT EXISTS prices (
    isin   TEXT NOT NULL,
    date   TEXT NOT NULL,
    nav    TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'cas',
    PRIMARY KEY (isin, date)
);

CREATE TABLE IF NOT EXISTS review_queue (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    ref         TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',      -- open | resolved
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT
);
"""


# Applied in order to databases created at an older version.
MIGRATIONS: dict[int, str] = {
    2: """
    ALTER TABLE source_documents ADD COLUMN sender TEXT NOT NULL DEFAULT '';
    CREATE TABLE IF NOT EXISTS imap_state (
        account     TEXT NOT NULL,
        folder      TEXT NOT NULL,
        uidvalidity INTEGER NOT NULL,
        last_uid    INTEGER NOT NULL,
        last_sync   TEXT,
        PRIMARY KEY (account, folder)
    );
    CREATE TABLE IF NOT EXISTS parser_cache (        -- working password template per sender (never the password)
        sender   TEXT PRIMARY KEY,
        template TEXT NOT NULL
    );
    """,
    3: """
    ALTER TABLE source_documents ADD COLUMN account      TEXT    NOT NULL DEFAULT '';   -- mailbox name
    ALTER TABLE source_documents ADD COLUMN folder       TEXT    NOT NULL DEFAULT '';
    ALTER TABLE source_documents ADD COLUMN uid          INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE source_documents ADD COLUMN received_at  TEXT;                          -- email Date header, UTC
    ALTER TABLE source_documents ADD COLUMN subject      TEXT    NOT NULL DEFAULT '';   -- identifiers masked
    ALTER TABLE source_documents ADD COLUMN filename     TEXT    NOT NULL DEFAULT '';   -- identifiers masked
    ALTER TABLE source_documents ADD COLUMN size         INTEGER NOT NULL DEFAULT 0;    -- attachment bytes
    ALTER TABLE source_documents ADD COLUMN auth         TEXT    NOT NULL DEFAULT '';   -- e.g. 'spf=pass dkim=pass'
    ALTER TABLE source_documents ADD COLUMN picked_up_at TEXT;                          -- first fetched
    ALTER TABLE source_documents ADD COLUMN processed_at TEXT;                          -- last import/attempt
    UPDATE source_documents SET picked_up_at = created_at, processed_at = created_at;
    """,
    4: """
    CREATE TABLE IF NOT EXISTS account_aliases (      -- another spelling of a folio -> the account it belongs to
        ref_hash   TEXT PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES accounts(id)
    );
    CREATE TABLE IF NOT EXISTS txn_ignored (          -- keys of duplicate transactions removed by a merge
        idem_key TEXT PRIMARY KEY
    );
    """,
    5: """
    ALTER TABLE transactions ADD COLUMN reference TEXT NOT NULL DEFAULT '';   -- UPI/IMPS RRN, transfer id (bank rows)
    CREATE INDEX IF NOT EXISTS ix_txn_reference ON transactions (reference) WHERE reference != '';
    CREATE TABLE IF NOT EXISTS account_balances (     -- closing balance per statement (bank, PPF); liabilities are negative
        account_id INTEGER NOT NULL REFERENCES accounts(id),
        as_of      TEXT NOT NULL,
        balance    TEXT NOT NULL,
        doc_id     INTEGER NOT NULL REFERENCES source_documents(id),
        PRIMARY KEY (account_id, as_of)
    );
    CREATE TABLE IF NOT EXISTS fixed_deposits (       -- snapshot per statement; number kept as keyed hash + last 4
        id                INTEGER PRIMARY KEY,
        institution       TEXT NOT NULL,
        ref_hash          TEXT NOT NULL,
        mask              TEXT NOT NULL,
        linked_account_id INTEGER REFERENCES accounts(id),
        open_date         TEXT,
        principal         TEXT,
        rate              TEXT,
        tenure            TEXT NOT NULL DEFAULT '',
        maturity_date     TEXT,
        maturity_amount   TEXT,
        current_balance   TEXT,
        status            TEXT NOT NULL DEFAULT '',
        as_of             TEXT NOT NULL,
        doc_id            INTEGER NOT NULL REFERENCES source_documents(id),
        UNIQUE (ref_hash, as_of)
    );
    """,
    6: """
    CREATE TABLE IF NOT EXISTS statement_summaries (  -- card statement headline per period: what is due, by when
        account_id     INTEGER NOT NULL REFERENCES accounts(id),
        statement_date TEXT NOT NULL,
        period_from    TEXT,
        period_to      TEXT,
        total_due      TEXT NOT NULL,                 -- amount owed (negative = card is in credit)
        min_due        TEXT,
        due_date       TEXT,
        credit_limit   TEXT,
        doc_id         INTEGER NOT NULL REFERENCES source_documents(id),
        PRIMARY KEY (account_id, statement_date)
    );
    """,
    7: """
    ALTER TABLE statement_summaries ADD COLUMN amount_due TEXT;   -- what to pay by the due date (can be below total_due, which is the outstanding balance)
    """,
    8: """
    ALTER TABLE source_documents ADD COLUMN parse_rev INTEGER NOT NULL DEFAULT 1;   -- which reading of the parsers produced this import (see ledger.PARSE_REVISION)
    """,
}


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    has_docs = conn.execute("SELECT 1 FROM sqlite_master WHERE name='source_documents'").fetchone()
    if not has_docs:
        version = 0
    for target in sorted(MIGRATIONS):
        if version < target:
            conn.executescript(MIGRATIONS[target])
            conn.execute(f"PRAGMA user_version = {target}")
    conn.commit()


def _prepare(conn: sqlite3.Connection) -> None:
    """Create or upgrade the schema on a read-write connection."""
    fresh_version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.executescript(SCHEMA)               # CREATE IF NOT EXISTS: v1 tables
    if fresh_version == 0:
        conn.execute("PRAGMA user_version = 1")
    _migrate(conn)


def connect_rw(path: str | Path) -> sqlite3.Connection:
    """Read-write connection. Intended for finance.ledger only. The path `vault:db` means the signed-in profile's
    encrypted ledger; anything else is a plain SQLite file (tests, tools)."""
    if str(path) == vault.VAULT_DB:
        conn = vault.require_vault().connect(writable=True)
        try:
            _prepare(conn)
        except BaseException:
            conn.close()
            raise
        return conn
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    _prepare(conn)
    return conn


def connect_ro(path: str | Path) -> sqlite3.Connection:
    """Read-only connection for analytics, the UI and (later) query tools.

    A missing or older database is created/upgraded first (schema only, no data is touched),
    so a page never queries a table that a newer release added.
    """
    if str(path) == vault.VAULT_DB:
        v = vault.require_vault()
        probe = v.connect(writable=False)
        try:
            needs_upgrade = probe.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION
        finally:
            probe.close()
        if needs_upgrade:
            connect_rw(path).close()
        conn = v.connect(writable=False)
        conn.execute("PRAGMA query_only = ON")
        return conn
    p = Path(path)
    if p.exists():
        probe = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        version = probe.execute("PRAGMA user_version").fetchone()[0]
        probe.close()
        needs_upgrade = version < SCHEMA_VERSION
    else:
        needs_upgrade = True
    if needs_upgrade:
        connect_rw(p).close()
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn
