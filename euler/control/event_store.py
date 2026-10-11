"""SQLite append-only event store for the research-control plane.

Stdlib ``sqlite3`` only. The store owns the database file and schema, including
budget tables used by ``BudgetLedger``. Event rows are immutable; idempotent
retries return the original event. Payload hash and event id are recomputed on
read and during integrity scans.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from euler.control.ids import canonical_json, event_id, payload_hash
from euler.control.schema import EventEnvelope, SchemaValidationError

STORE_SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000
_SYNCHRONOUS_FULL = 2

_REQUIRED_TABLES = frozenset(
    {"schema_meta", "events", "budget_accounts", "budget_reservations"}
)

# One statement per execute(): sqlite3.execute rejects scripts, and
# executescript() issues an implicit COMMIT that would break schema init.
_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE schema_meta (
        version INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        schema_version INTEGER NOT NULL,
        project_id TEXT NOT NULL,
        timestamp_utc TEXT NOT NULL,
        actor TEXT NOT NULL,
        event_type TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        payload TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        causation_id TEXT,
        correlation_id TEXT,
        UNIQUE (project_id, event_type, idempotency_key)
    )
    """,
    """
    CREATE TABLE budget_accounts (
        project_id TEXT PRIMARY KEY,
        limit_kind TEXT NOT NULL,
        limit_microusd INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (
            (limit_kind = 'UNLIMITED' AND limit_microusd IS NULL)
            OR (
                limit_kind = 'LIMITED'
                AND limit_microusd IS NOT NULL
                AND limit_microusd >= 0
            )
        )
    )
    """,
    """
    CREATE TABLE budget_reservations (
        reservation_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        reserved_cost_microusd INTEGER NOT NULL,
        reserved_input_tokens INTEGER NOT NULL,
        reserved_output_tokens INTEGER NOT NULL,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        settled_at TEXT,
        usage_json TEXT,
        consumed_cost_microusd INTEGER NOT NULL,
        FOREIGN KEY (project_id) REFERENCES budget_accounts(project_id),
        UNIQUE (project_id, idempotency_key),
        CHECK (reserved_cost_microusd >= 0),
        CHECK (reserved_input_tokens >= 0),
        CHECK (reserved_output_tokens >= 0),
        CHECK (consumed_cost_microusd >= 0),
        CHECK (state IN ('RESERVED', 'SETTLED', 'RELEASED', 'EXPIRED'))
    )
    """,
    "CREATE INDEX idx_events_project_seq ON events (project_id, seq)",
    """
    CREATE INDEX idx_reservations_project_state
        ON budget_reservations (project_id, state)
    """,
    """
    CREATE TRIGGER events_no_update BEFORE UPDATE ON events
    BEGIN
        SELECT RAISE(ABORT, 'events are append-only');
    END
    """,
    """
    CREATE TRIGGER events_no_delete BEFORE DELETE ON events
    BEGIN
        SELECT RAISE(ABORT, 'events are append-only');
    END
    """,
)

__all__ = [
    "STORE_SCHEMA_VERSION",
    "BUSY_TIMEOUT_MS",
    "EventStoreError",
    "IdempotencyConflict",
    "StoreSchemaError",
    "EventIntegrityError",
    "AppendResult",
    "EventStore",
]


class EventStoreError(Exception):
    """Base error for event-store failures."""


class IdempotencyConflict(EventStoreError):
    """Same idempotency key was reused with a different payload."""


class StoreSchemaError(EventStoreError):
    """Database schema is missing, corrupt, or an unsupported version."""


class EventIntegrityError(EventStoreError):
    """Stored event payload hash or event id does not recompute."""


@dataclass(frozen=True)
class AppendResult:
    event: EventEnvelope
    created: bool


class EventStore:
    """Append-only SQLite event log with WAL and fail-closed schema checks."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._closed = False
        self._tx_depth = 0
        self._tx_failed = False
        self._conn = self._connect()
        try:
            self._configure_connection()
            self._init_or_check_schema()
        except BaseException:
            self._conn.close()
            self._closed = True
            raise

    @classmethod
    def open(cls, path: str | Path) -> EventStore:
        return cls(path)

    def close(self) -> None:
        if self._closed:
            return
        if self._tx_depth > 0:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            self._tx_depth = 0
            self._tx_failed = False
        self._conn.close()
        self._closed = True

    def __enter__(self) -> EventStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Run a (possibly nested) transaction. Outermost commit/rollback only.

        If a nested body raised and the caller caught that exception inside the
        outer body, the outer context still rolls back and raises EventStoreError.
        It never returns successfully after such a rollback.
        """
        self._ensure_open()
        if self._tx_depth > 0:
            self._tx_depth += 1
            try:
                yield self._conn
            except BaseException:
                self._tx_failed = True
                raise
            finally:
                self._tx_depth -= 1
            return

        self._conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        self._tx_depth = 1
        self._tx_failed = False
        try:
            yield self._conn
            if self._tx_failed:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise EventStoreError(
                    "a nested transaction failed; the outer transaction was "
                    "rolled back"
                )
            self._conn.execute("COMMIT")
        except BaseException:
            if self._tx_depth > 0:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            self._tx_depth = 0
            self._tx_failed = False

    def append(self, envelope: EventEnvelope) -> AppendResult:
        """Insert ``envelope`` or return the original row for an identical retry."""
        self._ensure_open()
        self._require_envelope(envelope)
        try:
            envelope.validate()
        except SchemaValidationError as exc:
            raise EventStoreError(f"invalid event envelope: {exc}") from exc
        self._verify_envelope(envelope)
        if envelope.schema_version != STORE_SCHEMA_VERSION:
            raise StoreSchemaError(
                f"unsupported event schema_version {envelope.schema_version}; "
                f"supported: {STORE_SCHEMA_VERSION}"
            )
        with self.transaction(immediate=True):
            return self._append_locked(envelope)

    def get(self, event_id: str) -> EventEnvelope | None:
        self._ensure_open()
        _require_nonempty_str("event_id", event_id)
        row = self._conn.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        envelope = self._row_to_envelope(row)
        self._verify_envelope(envelope)
        return envelope

    def iter_project(
        self, project_id: str, after_seq: int = 0
    ) -> Iterator[tuple[int, EventEnvelope]]:
        """Yield ``(seq, envelope)`` for ``project_id`` with ``seq > after_seq``."""
        self._ensure_open()
        _require_nonempty_str("project_id", project_id)
        if not isinstance(after_seq, int) or isinstance(after_seq, bool) or after_seq < 0:
            raise EventStoreError("after_seq must be a non-negative int")
        rows = self._conn.execute(
            """
            SELECT * FROM events
            WHERE project_id = ? AND seq > ?
            ORDER BY seq ASC
            """,
            (project_id, after_seq),
        )
        for row in rows:
            envelope = self._row_to_envelope(row)
            self._verify_envelope(envelope)
            yield int(row["seq"]), envelope

    def verify_integrity(self) -> int:
        """Recompute hash and event id for every stored event. Return the count."""
        self._ensure_open()
        checked = 0
        for row in self._conn.execute("SELECT * FROM events ORDER BY seq ASC"):
            envelope = self._row_to_envelope(row)
            self._verify_envelope(envelope)
            checked += 1
        return checked

    def _connect(self) -> sqlite3.Connection:
        if self.path.exists() and self.path.is_dir():
            raise EventStoreError(f"event store path is a directory: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path),
            isolation_level=None,
            timeout=BUSY_TIMEOUT_MS / 1000,
        )
        conn.row_factory = sqlite3.Row
        return conn

    def _configure_connection(self) -> None:
        conn = self._conn
        conn.execute("PRAGMA foreign_keys = ON")
        journal = conn.execute("PRAGMA journal_mode = WAL").fetchone()
        mode = journal[0] if journal is not None else None
        if str(mode).lower() != "wal":
            raise StoreSchemaError(f"failed to enable WAL journal_mode, got {mode!r}")
        conn.execute("PRAGMA synchronous = FULL")
        syn = conn.execute("PRAGMA synchronous").fetchone()
        if syn is None or int(syn[0]) != _SYNCHRONOUS_FULL:
            raise StoreSchemaError(
                f"failed to set synchronous=FULL, got {None if syn is None else syn[0]!r}"
            )
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()
        if timeout is None or int(timeout[0]) != BUSY_TIMEOUT_MS:
            raise StoreSchemaError(
                f"failed to set busy_timeout={BUSY_TIMEOUT_MS}, "
                f"got {None if timeout is None else timeout[0]!r}"
            )
        fk = conn.execute("PRAGMA foreign_keys").fetchone()
        if fk is None or int(fk[0]) != 1:
            raise StoreSchemaError("failed to enable foreign_keys")

    def _init_or_check_schema(self) -> None:
        conn = self._conn
        existing = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "schema_meta" not in existing:
            if existing:
                raise StoreSchemaError(
                    "schema_meta is missing; refusing to initialize a non-empty "
                    "database"
                )
            self._create_schema()
            return
        missing = _REQUIRED_TABLES - existing
        if missing:
            raise StoreSchemaError(
                "store schema is incomplete; missing tables: "
                + ", ".join(sorted(missing))
            )
        versions = conn.execute("SELECT version FROM schema_meta").fetchall()
        if len(versions) != 1:
            raise StoreSchemaError(
                "schema_meta must contain exactly one version row, "
                f"found {len(versions)}"
            )
        version = versions[0][0]
        if not isinstance(version, int) or isinstance(version, bool):
            raise StoreSchemaError(f"schema_meta.version is not an int: {version!r}")
        if version != STORE_SCHEMA_VERSION:
            raise StoreSchemaError(
                f"unsupported store schema version {version}; "
                f"supported: {STORE_SCHEMA_VERSION}"
            )

    def _create_schema(self) -> None:
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_meta (version) VALUES (?)",
                (STORE_SCHEMA_VERSION,),
            )
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def _append_locked(self, envelope: EventEnvelope) -> AppendResult:
        conn = self._conn
        existing_row = conn.execute(
            """
            SELECT * FROM events
            WHERE project_id = ? AND event_type = ? AND idempotency_key = ?
            """,
            (envelope.project_id, envelope.event_type, envelope.idempotency_key),
        ).fetchone()
        if existing_row is not None:
            existing = self._row_to_envelope(existing_row)
            self._verify_envelope(existing)
            if (
                existing.payload_hash != envelope.payload_hash
                or existing.event_id != envelope.event_id
            ):
                raise IdempotencyConflict(
                    "idempotency key reused with a different payload: "
                    f"project_id={envelope.project_id!r} "
                    f"event_type={envelope.event_type!r} "
                    f"idempotency_key={envelope.idempotency_key!r}"
                )
            return AppendResult(event=existing, created=False)

        conn.execute("SAVEPOINT event_insert")
        try:
            conn.execute(
                """
                INSERT INTO events (
                    event_id, schema_version, project_id, timestamp_utc, actor,
                    event_type, idempotency_key, payload, payload_hash,
                    causation_id, correlation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    envelope.event_id,
                    envelope.schema_version,
                    envelope.project_id,
                    envelope.timestamp_utc,
                    envelope.actor,
                    envelope.event_type,
                    envelope.idempotency_key,
                    canonical_json(envelope.payload),
                    envelope.payload_hash,
                    envelope.causation_id,
                    envelope.correlation_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK TO SAVEPOINT event_insert")
            conn.execute("RELEASE SAVEPOINT event_insert")
            conflict = conn.execute(
                """
                SELECT * FROM events
                WHERE project_id = ? AND event_type = ? AND idempotency_key = ?
                """,
                (envelope.project_id, envelope.event_type, envelope.idempotency_key),
            ).fetchone()
            if conflict is not None:
                existing = self._row_to_envelope(conflict)
                self._verify_envelope(existing)
                if (
                    existing.payload_hash != envelope.payload_hash
                    or existing.event_id != envelope.event_id
                ):
                    raise IdempotencyConflict(
                        "idempotency key reused with a different payload: "
                        f"project_id={envelope.project_id!r} "
                        f"event_type={envelope.event_type!r} "
                        f"idempotency_key={envelope.idempotency_key!r}"
                    ) from exc
                return AppendResult(event=existing, created=False)
            raise EventStoreError(f"event insert conflict: {exc}") from exc
        conn.execute("RELEASE SAVEPOINT event_insert")
        return AppendResult(event=envelope, created=True)

    def _row_to_envelope(self, row: sqlite3.Row) -> EventEnvelope:
        schema_version = row["schema_version"]
        if schema_version != STORE_SCHEMA_VERSION:
            raise StoreSchemaError(
                f"unsupported event schema_version {schema_version}; "
                f"supported: {STORE_SCHEMA_VERSION}"
            )
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError as exc:
            raise EventIntegrityError(
                f"event {row['event_id']} payload is not valid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise EventIntegrityError(
                f"event {row['event_id']} payload is not a JSON object"
            )
        data = {
            "schema_version": schema_version,
            "event_id": row["event_id"],
            "project_id": row["project_id"],
            "timestamp_utc": row["timestamp_utc"],
            "actor": row["actor"],
            "event_type": row["event_type"],
            "idempotency_key": row["idempotency_key"],
            "payload": payload,
            "payload_hash": row["payload_hash"],
            "causation_id": row["causation_id"],
            "correlation_id": row["correlation_id"],
        }
        try:
            return EventEnvelope.from_dict(data)
        except (SchemaValidationError, TypeError, ValueError) as exc:
            raise EventIntegrityError(
                f"event {row['event_id']} failed to rehydrate: {exc}"
            ) from exc

    def _verify_envelope(self, envelope: EventEnvelope) -> None:
        try:
            envelope.validate()
        except SchemaValidationError as exc:
            raise EventIntegrityError(
                f"event {envelope.event_id} failed validation: {exc}"
            ) from exc
        except EventIntegrityError:
            raise
        except (TypeError, ValueError) as exc:
            raise EventIntegrityError(
                f"event {envelope.event_id} failed validation: {exc}"
            ) from exc
        try:
            digest = payload_hash(envelope.payload)
            recomputed_id = event_id(
                envelope.project_id,
                envelope.event_type,
                envelope.idempotency_key,
                envelope.payload_hash,
            )
        except (TypeError, ValueError) as exc:
            raise EventIntegrityError(
                f"event {envelope.event_id} is not verifiable: {exc}"
            ) from exc
        if digest != envelope.payload_hash:
            raise EventIntegrityError(
                f"event {envelope.event_id} payload hash mismatch"
            )
        if recomputed_id != envelope.event_id:
            raise EventIntegrityError(
                f"event {envelope.event_id} event id mismatch"
            )

    def _require_envelope(self, envelope: EventEnvelope) -> None:
        if not isinstance(envelope, EventEnvelope):
            raise EventStoreError(
                f"append requires EventEnvelope, got {type(envelope).__name__}"
            )

    def _ensure_open(self) -> None:
        if self._closed:
            raise EventStoreError("EventStore is closed")


def _require_nonempty_str(name: str, value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise EventStoreError(f"{name} must be a nonempty str")
    return value
