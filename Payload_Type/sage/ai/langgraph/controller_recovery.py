"""Opt-in, same-channel journal for bounded autonomous controller recovery.

Only a completed controller cycle is replayable. A pending policy or effect seam is
intentionally ambiguous after a process death and must be inspected externally.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from runtime_db import database_path


class RecoveryBlocked(RuntimeError):
    """Saved controller state cannot safely be resumed automatically."""


def enabled() -> bool:
    return os.environ.get("SAGE_AUTO_CONTROLLER_RECOVERY") == "1"


def binding_digest(binding: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def binding_for_model(model: Any, contract: Any) -> dict[str, Any]:
    """Bind the journal to native authenticated transport and resolved runtime identity."""
    client = getattr(model, "mythic_client", None)
    resolver = getattr(client, "_eng_key", None)
    engagement = str(resolver() or "") if callable(resolver) else ""
    if not engagement:
        raise RecoveryBlocked("controller engagement identity unavailable")
    return {
        "operation_id": int(model.operation_id),
        "channel_id": int(model.channel_id),
        "apitoken_id": int(model.apitoken_id),
        "request_contract_digest": contract.digest,
        "request_contract_payload": contract.to_payload(),
        "config_signature": str(model._chat_request_config_signature),
        "provider": str(model.provider),
        "model": str(model.model),
        "mode": str(model.mode),
        "autonomous_solve": bool(model._autonomous_solve),
        "policy_mode": str(model.policy_mode),
        "engagement_id": engagement,
    }


class ControllerRecoveryStore:
    """One exclusive process owner and one durable record per operation/channel.

    The fixed file is Sage's existing runtime DB. No request or environment value can
    select a filesystem path. The adjacent lock is held for the entire active run;
    flock releases it on process death while SQLite commits preserve seam markers.
    """

    def __init__(self, operation_id: int, channel_id: int, *, db_path: str | None = None):
        self.operation_id = int(operation_id)
        self.channel_id = int(channel_id)
        self._db_path = Path(db_path if db_path is not None else database_path())
        self._lock_path = f"{self._db_path}.controller.{self.operation_id}.{self.channel_id}.lock"
        self._lock_file = None

    def acquire(self) -> None:
        lock = open(self._lock_path, "a+b")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock.close()
            raise RecoveryBlocked("autonomous controller is already active") from exc
        self._lock_file = lock

    def release(self) -> None:
        if self._lock_file is not None:
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None

    def _connect(self):
        connection = sqlite3.connect(self._db_path, timeout=5)
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS sage_auto_controller_recovery ("
            "operation_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, "
            "binding_digest TEXT NOT NULL, phase TEXT NOT NULL, payload TEXT NOT NULL, "
            "PRIMARY KEY (operation_id, channel_id))"
        )
        return connection

    def load(self, *, read_only: bool = False) -> dict[str, Any] | None:
        if read_only:
            if not self._db_path.is_file():
                return None
            connection = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True, timeout=5)
        else:
            connection = self._connect()
        try:
            try:
                row = connection.execute(
                    "SELECT binding_digest, phase, payload FROM sage_auto_controller_recovery "
                    "WHERE operation_id=? AND channel_id=?",
                    (self.operation_id, self.channel_id),
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if read_only and "no such table" in str(exc):
                    return None
                raise
        finally:
            connection.close()
        if row is None:
            return None
        try:
            payload = json.loads(row[2])
        except (TypeError, ValueError) as exc:
            raise RecoveryBlocked("controller recovery record is corrupt") from exc
        if not isinstance(payload, dict):
            raise RecoveryBlocked("controller recovery record is corrupt")
        return {"binding_digest": row[0], "phase": row[1], "payload": payload}

    def inspect(self) -> dict[str, Any]:
        """Read-only coordinator view; never claims or modifies the run."""
        record = self.load(read_only=True)
        active = False
        if Path(self._lock_path).is_file():
            lock = open(self._lock_path, "rb")
            try:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    active = True
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)
            finally:
                lock.close()
        return {
            "exists": record is not None,
            "active": active,
            "phase": record["phase"] if record else "",
            "original_request_id": record["payload"].get("original_request_id", "") if record else "",
            "terminal_status": record["payload"].get("terminal_status", "") if record else "",
        }

    def write(self, binding: dict[str, Any], phase: str, payload: dict[str, Any]) -> None:
        if self._lock_file is None:
            raise RecoveryBlocked("controller recovery store has no exclusive run lock")
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = binding_digest(binding)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT binding_digest, phase FROM sage_auto_controller_recovery WHERE operation_id=? AND channel_id=?",
                (self.operation_id, self.channel_id),
            ).fetchone()
            if existing is not None and existing[0] != digest:
                raise RecoveryBlocked("controller recovery binding changed")
            if existing is not None and existing[1] == "revoked":
                raise RecoveryBlocked("controller recovery was revoked by operator stop")
            connection.execute(
                "INSERT INTO sage_auto_controller_recovery VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(operation_id, channel_id) DO UPDATE SET phase=excluded.phase, payload=excluded.payload",
                (self.operation_id, self.channel_id, digest, phase, serialized),
            )

    def revoke(self) -> bool:
        """Durably invalidate an unfinished run on explicit operator stop."""
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "UPDATE sage_auto_controller_recovery SET phase='revoked' "
                "WHERE operation_id=? AND channel_id=? AND phase!='terminal'",
                (self.operation_id, self.channel_id),
            )
            return cursor.rowcount > 0

    def require_clean(self, binding: dict[str, Any], original_request_id: str) -> dict[str, Any]:
        record = self.load()
        if record is None:
            raise RecoveryBlocked("no saved autonomous controller run for this channel")
        if record["binding_digest"] != binding_digest(binding):
            raise RecoveryBlocked("controller recovery binding changed")
        payload = record["payload"]
        if payload.get("original_request_id") != original_request_id:
            raise RecoveryBlocked("controller recovery request identity changed")
        if record["phase"] != "clean":
            raise RecoveryBlocked(f"controller recovery blocked at {record['phase']} boundary")
        return payload
