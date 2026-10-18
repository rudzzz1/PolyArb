"""SQLite Schema Migration Engine (Phase 6.2).
Provides versioned, transactional migrations for the internal SQLite database.
Tracks applied versions in `_schema_versions` and applies sequential upgrades
without relying on bare except handlers.
"""
from __future__ import annotations
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .logger import get_logger

logger = get_logger("migrations")


@dataclass
class Migration:
    version: int
    description: str
    up: str | Callable[[sqlite3.Connection], None]


# Ordered migration scripts for polyarb SQLite storage
MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        description="Create base audit_log table and immutability triggers",
        up="""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            venue TEXT NOT NULL,
            market_id TEXT,
            side TEXT,
            price REAL,
            size REAL,
            order_id TEXT,
            fill_qty REAL,
            fill_price REAL,
            status TEXT,
            raw_response_hash TEXT,
            metadata TEXT
        );

        CREATE TRIGGER IF NOT EXISTS audit_no_update
        BEFORE UPDATE ON audit_log
        BEGIN
            SELECT RAISE(ABORT, 'IMMUTABILITY VIOLATION: Updates to audit_log are prohibited.');
        END;

        CREATE TRIGGER IF NOT EXISTS audit_no_delete
        BEFORE DELETE ON audit_log
        BEGIN
            SELECT RAISE(ABORT, 'IMMUTABILITY VIOLATION: Deletions from audit_log are prohibited.');
        END;
        """,
    ),
    Migration(
        version=2,
        description="Add performance indexes on audit timestamp, action, and order_id",
        up="""
        CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_audit_action_venue ON audit_log(action, venue);
        CREATE INDEX IF NOT EXISTS idx_audit_order_id ON audit_log(order_id);
        """,
    ),
    Migration(
        version=3,
        description="Create settlement_reconciliation log table",
        up="""
        CREATE TABLE IF NOT EXISTS settlement_reconciliations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            structure_id TEXT NOT NULL,
            leg_id TEXT NOT NULL,
            venue TEXT NOT NULL,
            status TEXT NOT NULL,
            desync_detected INTEGER DEFAULT 0,
            recorded_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_recon_struct ON settlement_reconciliations(structure_id);
        """,
    ),
]


class SchemaMigrator:
    """Runs versioned schema upgrades for SQLite databases."""

    @staticmethod
    def get_current_version(conn: sqlite3.Connection) -> int:
        """Returns highest applied migration version."""
        with conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS _schema_versions (
                version INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
            """)
            cur = conn.execute("SELECT MAX(version) FROM _schema_versions")
            row = cur.fetchone()
            return int(row[0]) if (row and row[0] is not None) else 0

    @classmethod
    def apply_migrations(cls, conn: sqlite3.Connection) -> int:
        """Applies all pending migrations in sequence within transactions."""
        current_v = cls.get_current_version(conn)
        applied_count = 0

        for m in MIGRATIONS:
            if m.version > current_v:
                logger.info(f"Applying SQLite migration v{m.version}: {m.description}")
                now_iso = datetime.now(timezone.utc).isoformat()
                with conn:
                    if isinstance(m.up, str):
                        conn.executescript(m.up)
                    else:
                        m.up(conn)
                    conn.execute(
                        "INSERT INTO _schema_versions (version, description, applied_at) VALUES (?, ?, ?)",
                        (m.version, m.description, now_iso),
                    )
                applied_count += 1
                logger.info(f"Successfully applied migration v{m.version}")

        return applied_count
