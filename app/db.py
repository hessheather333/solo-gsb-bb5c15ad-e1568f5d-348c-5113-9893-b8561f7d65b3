"""SQLite persistence and schema management."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import settings

# Versioned schema migrations. Every script is idempotent (IF NOT EXISTS) and
# stamps PRAGMA user_version so existing databases are upgraded in place.
MIGRATIONS: tuple[str, ...] = (
    # v1: budgets, releases and transfers.
    """
    CREATE TABLE IF NOT EXISTS budgets (
        dataset_id TEXT PRIMARY KEY,
        total      REAL NOT NULL,
        used       REAL NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS releases (
        id              TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        dataset_id      TEXT NOT NULL,
        query_id        TEXT NOT NULL,
        epsilon         REAL NOT NULL,
        sensitivity     REAL NOT NULL,
        true_value      REAL NOT NULL,
        noise           REAL NOT NULL,
        released_value  REAL NOT NULL,
        budget_used     REAL NOT NULL,
        budget_remaining REAL NOT NULL,
        created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );

    CREATE INDEX IF NOT EXISTS idx_releases_dataset ON releases(dataset_id);

    CREATE TABLE IF NOT EXISTS transfers (
        id               TEXT PRIMARY KEY,
        source_dataset   TEXT NOT NULL,
        target_dataset   TEXT NOT NULL,
        epsilon          REAL NOT NULL,
        source_used      REAL NOT NULL,
        source_remaining REAL NOT NULL,
        target_used      REAL NOT NULL,
        target_remaining REAL NOT NULL,
        created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );

    CREATE INDEX IF NOT EXISTS idx_transfers_source ON transfers(source_dataset);
    CREATE INDEX IF NOT EXISTS idx_transfers_target ON transfers(target_dataset);

    PRAGMA user_version = 1;
    """,
    # v2: API-key authentication.
    """
    CREATE TABLE IF NOT EXISTS api_keys (
        key_id     TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        -- SHA-256 hex digest of the full plaintext token. The plaintext is
        -- only ever returned once, at creation time.
        key_hash   TEXT NOT NULL UNIQUE,
        -- Short non-secret display prefix of the token ("dpk_" + 8 chars).
        key_prefix TEXT NOT NULL,
        -- UTC expiry in ISO-8601; NULL means the key never expires.
        expires_at TEXT,
        revoked    INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );

    CREATE TABLE IF NOT EXISTS api_key_scopes (
        key_id     TEXT NOT NULL,
        dataset_id TEXT NOT NULL,
        PRIMARY KEY (key_id, dataset_id),
        FOREIGN KEY (key_id) REFERENCES api_keys(key_id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_api_key_scopes_dataset
        ON api_key_scopes(dataset_id);

    PRAGMA user_version = 2;
    """,
)


def connect(database_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(
        database_path or settings.database_path,
        timeout=10.0,
        isolation_level=None,  # autocommit; transactions are managed explicitly
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    # WAL allows concurrent readers while a writer holds the lock.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(database_path: str | None = None) -> None:
    """Create or upgrade the schema at ``database_path``.

    A brand-new database gets every migration in order; an existing database
    created before migrations existed (user_version == 0, tables already
    present) is stamped by the idempotent v1 script and then upgraded.
    """
    path = Path(database_path or settings.database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(str(path))
    try:
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        for index, script in enumerate(MIGRATIONS, start=1):
            if index <= current:
                continue
            # executescript commits any pending transaction and runs the
            # whole migration atomically enough for DDL + the version stamp.
            conn.executescript(script)
    finally:
        conn.close()


@contextmanager
def immediate_transaction() -> Iterator[sqlite3.Connection]:
    """Open an IMMEDIATE write transaction.

    Acquires SQLite's reserved lock up-front, so concurrent requests racing
    for the last slice of a budget serialize: exactly one can commit a
    deduction, the others see the updated balance and fail with 402. The same
    transaction serializes concurrent key creation/revocation.
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
