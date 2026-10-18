"""Immutable Append-Only Audit Log for PolyArb.
Guarantees database-level immutability using SQLite triggers to block UPDATE and DELETE.
Logs every order placement, fill, cancellation, structure booking, and risk event.
"""
from __future__ import annotations
import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .logger import get_logger

logger = get_logger("audit")


class AuditLog:
    """Immutable audit trail backed by SQLite with trigger-level write-once protection."""
    def __init__(self, db_path: str | Path = "data/audit.sqlite"):
        self.db_path_str = str(db_path)
        self._is_memory = (self.db_path_str == ":memory:")
        if self._is_memory:
            self.db_path = Path(":memory:")
            self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._mem_conn.row_factory = sqlite3.Row
        else:
            self._mem_conn = None
            self.db_path = Path(db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._is_memory and self._mem_conn is not None:
            return self._mem_conn
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _close_conn(self, conn: sqlite3.Connection) -> None:
        if not self._is_memory:
            conn.close()

    def close(self) -> None:
        if self._is_memory and self._mem_conn is not None:
            self._mem_conn.close()
            self._mem_conn = None

    def _init_db(self):
        conn = self._get_conn()
        try:
            with conn:
                # 1. Main audit log table
                conn.execute("""
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
                """)

                # 2. Database triggers enforcing IMMUTABILITY (strict append-only)
                conn.execute("""
                CREATE TRIGGER IF NOT EXISTS audit_no_update
                BEFORE UPDATE ON audit_log
                BEGIN
                    SELECT RAISE(ABORT, 'IMMUTABILITY VIOLATION: Updates to audit_log are prohibited.');
                END;
                """)

                conn.execute("""
                CREATE TRIGGER IF NOT EXISTS audit_no_delete
                BEFORE DELETE ON audit_log
                BEGIN
                    SELECT RAISE(ABORT, 'IMMUTABILITY VIOLATION: Deletions from audit_log are prohibited.');
                END;
                """)
        finally:
            self._close_conn(conn)

    def log(self, action: str, venue: str, market_id: str = "", side: str = "",
            price: float = 0.0, size: float = 0.0, order_id: str = "",
            fill_qty: float = 0.0, fill_price: float = 0.0, status: str = "",
            raw_response: str | dict | None = None, metadata: dict | None = None) -> int:
        """Appends an immutable audit event."""
        now_iso = datetime.now(timezone.utc).isoformat()

        # Compute hash of raw response if provided
        resp_hash = ""
        if raw_response is not None:
            raw_str = json.dumps(raw_response, sort_keys=True) if isinstance(raw_response, dict) else str(raw_response)
            resp_hash = hashlib.sha256(raw_str.encode("utf-8")).hexdigest()

        meta_json = json.dumps(metadata or {})

        conn = self._get_conn()
        try:
            with conn:
                cur = conn.execute("""
                INSERT INTO audit_log (
                    ts, action, venue, market_id, side, price, size,
                    order_id, fill_qty, fill_price, status, raw_response_hash, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    now_iso, action.upper(), venue.lower(), market_id, side.upper(),
                    float(price), float(size), order_id, float(fill_qty),
                    float(fill_price), status.upper(), resp_hash, meta_json
                ))
                record_id = cur.lastrowid or 0
        finally:
            self._close_conn(conn)

        logger.info(f"Audit record #{record_id} [{action}]: {venue} {side} {size}@{price} ({order_id})")
        return record_id

    def get_events(self, limit: int = 100, venue: str | None = None,
                   action: str | None = None) -> list[dict[str, Any]]:
        """Queries recent audit events."""
        query = "SELECT * FROM audit_log WHERE 1=1"
        params: list[Any] = []
        if venue:
            query += " AND venue = ?"
            params.append(venue.lower())
        if action:
            query += " AND action = ?"
            params.append(action.upper())
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        conn = self._get_conn()
        try:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            self._close_conn(conn)

    def export_csv(self, target_path: str | Path) -> Path:
        """Export full audit trail to CSV for regulatory compliance."""
        p = Path(target_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_conn()
        try:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id ASC").fetchall()
            if not rows:
                p.write_text("id,ts,action,venue,market_id,side,price,size,order_id,fill_qty,fill_price,status,raw_response_hash,metadata\n", encoding="utf-8")
                return p

            keys = rows[0].keys()
            with open(p, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(keys)
                for r in rows:
                    writer.writerow([r[k] for k in keys])
            return p
        finally:
            self._close_conn(conn)

    def export_json(self, target_path: str | Path) -> Path:
        """Export full audit trail to JSON."""
        p = Path(target_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        events = self.get_events(limit=1_000_000)
        p.write_text(json.dumps(events, indent=2), encoding="utf-8")
        return p
