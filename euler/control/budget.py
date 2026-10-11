"""Attempt budget ledger on top of the research-control event store.

Money is integer micro-USD. ``UNLIMITED`` is an explicit kind, never encoded as
null/missing. Reserve/settle/release/expire run under ``BEGIN IMMEDIATE``.
Unknown usage is stored as unknown and is never rewritten to zero.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from euler.control.event_store import EventStore
from euler.control.ids import canonical_json, stable_id
from euler.control.schema import (
    BudgetLimit,
    BudgetLimitKind,
    BudgetReservation,
    EventEnvelope,
    ObservationStatus,
    ReservationState,
    SchemaValidationError,
    UsageRecord,
)

_RESERVATION_DOMAIN = "reservation"
_RESERVATION_ID_VERSION = 1
_SQLITE_MAX_INT = 2**63 - 1
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DEFAULT_ACTOR = "budget_ledger"
_EVENT_ACCOUNT_CREATED = "budget.account_created"
_EVENT_RESERVED = "budget.reserved"
_EVENT_SETTLED = "budget.settled"
_EVENT_RELEASED = "budget.released"
_EVENT_EXPIRED = "budget.expired"

__all__ = [
    "BudgetError",
    "BudgetExceeded",
    "ReservationConflict",
    "InvalidReservationTransition",
    "BudgetAccount",
    "BudgetLedger",
]


class BudgetError(Exception):
    """Base error for budget-ledger failures."""


class BudgetExceeded(BudgetError):
    """A reservation would exceed a LIMITED account."""


class ReservationConflict(BudgetError):
    """Idempotency key reused with a different reservation payload."""


class InvalidReservationTransition(BudgetError):
    """Illegal state change, including any non-identical terminal replay."""


@dataclass(frozen=True, slots=True, kw_only=True)
class BudgetAccount:
    project_id: str
    limit: BudgetLimit
    reserved_cost_microusd: int
    settled_cost_microusd: int
    consumed_cost_microusd: int

    @property
    def remaining_cost_microusd(self) -> int | None:
        if self.limit.kind is BudgetLimitKind.UNLIMITED:
            return None
        if self.limit.value is None:
            raise BudgetError("LIMITED account is missing a micro-USD value")
        return self.limit.value - self.consumed_cost_microusd


class BudgetLedger:
    """Per-project reservation ledger sharing ``EventStore``'s SQLite file."""

    def __init__(self, store: EventStore) -> None:
        if not isinstance(store, EventStore):
            raise BudgetError("BudgetLedger requires an EventStore")
        self._store = store

    def ensure_account(
        self,
        project_id: str,
        limit: BudgetLimit,
        *,
        actor: str = _DEFAULT_ACTOR,
        timestamp_utc: str | None = None,
    ) -> BudgetAccount:
        project_id = _require_project_id(project_id)
        if not isinstance(limit, BudgetLimit):
            raise BudgetError(
                f"limit must be BudgetLimit, got {type(limit).__name__}"
            )
        actor = _require_nonempty_str("actor", actor)
        kind, value = _split_limit(limit)
        ts = timestamp_utc if timestamp_utc is not None else _utc_now()
        with self._store.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM budget_accounts WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is not None:
                existing = self._account_from_row(conn, row)
                if (
                    existing.limit.kind is limit.kind
                    and existing.limit.value == limit.value
                ):
                    return existing
                raise BudgetError(
                    f"budget account {project_id!r} already exists with a "
                    "different limit"
                )
            conn.execute(
                """
                INSERT INTO budget_accounts (
                    project_id, limit_kind, limit_microusd, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (project_id, kind, value, ts, ts),
            )
            self._append_budget_event(
                event_type=_EVENT_ACCOUNT_CREATED,
                project_id=project_id,
                idempotency_key=f"account:{project_id}",
                payload={
                    "project_id": project_id,
                    "limit": limit.to_dict(),
                    "created_at": ts,
                },
                actor=actor,
                timestamp_utc=ts,
                correlation_id=project_id,
            )
            account = self._get_account_locked(conn, project_id)
            if account is None:
                raise BudgetError(f"failed to create budget account {project_id!r}")
            return account

    def reserve(
        self,
        project_id: str,
        attempt_id: str,
        idempotency_key: str,
        reserved_cost_microusd: int,
        reserved_input_tokens: int = 0,
        reserved_output_tokens: int = 0,
        *,
        actor: str = _DEFAULT_ACTOR,
        timestamp_utc: str | None = None,
    ) -> BudgetReservation:
        project_id = _require_project_id(project_id)
        attempt_id = _require_nonempty_str("attempt_id", attempt_id)
        idempotency_key = _require_nonempty_str("idempotency_key", idempotency_key)
        cost = _require_nonneg_int("reserved_cost_microusd", reserved_cost_microusd)
        input_tokens = _require_nonneg_int(
            "reserved_input_tokens", reserved_input_tokens
        )
        output_tokens = _require_nonneg_int(
            "reserved_output_tokens", reserved_output_tokens
        )
        actor = _require_nonempty_str("actor", actor)
        ts = timestamp_utc if timestamp_utc is not None else _utc_now()
        reservation_id = stable_id(
            _RESERVATION_DOMAIN,
            _RESERVATION_ID_VERSION,
            project_id,
            attempt_id,
            idempotency_key,
        )
        with self._store.transaction(immediate=True) as conn:
            existing = self._get_reservation_by_key(conn, project_id, idempotency_key)
            if existing is not None:
                if not _same_reserve_payload(
                    existing,
                    attempt_id=attempt_id,
                    reserved_cost_microusd=cost,
                    reserved_input_tokens=input_tokens,
                    reserved_output_tokens=output_tokens,
                ):
                    raise ReservationConflict(
                        "idempotency key reused with a different reservation "
                        f"payload for project {project_id!r}"
                    )
                return existing

            account = self._get_account_locked(conn, project_id)
            if account is None:
                raise BudgetError(f"no budget account for project {project_id!r}")
            if account.limit.kind is BudgetLimitKind.LIMITED:
                remaining = account.remaining_cost_microusd
                if remaining is None:
                    raise BudgetError(
                        f"LIMITED account for project {project_id!r} has no remaining value"
                    )
                if cost > remaining:
                    raise BudgetExceeded(
                        f"reservation of {cost} micro-USD exceeds remaining "
                        f"{remaining} for project {project_id!r}"
                    )

            reservation = _build_reservation(
                reservation_id=reservation_id,
                project_id=project_id,
                attempt_id=attempt_id,
                idempotency_key=idempotency_key,
                reserved_cost_microusd=cost,
                reserved_input_tokens=input_tokens,
                reserved_output_tokens=output_tokens,
                state=ReservationState.RESERVED,
                created_at=ts,
            )
            stored = self._insert_reservation(conn, reservation, consumed=cost)
            self._append_budget_event(
                event_type=_EVENT_RESERVED,
                project_id=stored.project_id,
                idempotency_key=f"reserve:{stored.idempotency_key}",
                payload=stored.to_dict(),
                actor=actor,
                timestamp_utc=stored.created_at,
                correlation_id=stored.attempt_id,
            )
            return stored

    def settle(
        self,
        reservation_id: str,
        usage: UsageRecord,
        *,
        actor: str = _DEFAULT_ACTOR,
        timestamp_utc: str | None = None,
    ) -> BudgetReservation:
        reservation_id = _require_nonempty_str("reservation_id", reservation_id)
        if not isinstance(usage, UsageRecord):
            raise BudgetError(
                f"usage must be UsageRecord, got {type(usage).__name__}"
            )
        actor = _require_nonempty_str("actor", actor)
        ts = timestamp_utc if timestamp_utc is not None else _utc_now()
        with self._store.transaction(immediate=True) as conn:
            current = self._get_reservation_locked(conn, reservation_id)
            if current is None:
                raise BudgetError(f"unknown reservation {reservation_id!r}")
            if current.state is ReservationState.SETTLED:
                if _usage_equal(current.actual_usage, usage):
                    return current
                raise ReservationConflict(
                    f"reservation {reservation_id!r} already SETTLED with "
                    "a different usage payload"
                )
            if current.state is not ReservationState.RESERVED:
                raise InvalidReservationTransition(
                    f"cannot settle reservation {reservation_id!r} in state "
                    f"{current.state.value}"
                )
            consumed = _consumed_after_settle(current.reserved_cost_microusd, usage)
            updated = _build_reservation(
                reservation_id=current.reservation_id,
                project_id=current.project_id,
                attempt_id=current.attempt_id,
                idempotency_key=current.idempotency_key,
                reserved_cost_microusd=current.reserved_cost_microusd,
                reserved_input_tokens=current.reserved_input_tokens,
                reserved_output_tokens=current.reserved_output_tokens,
                state=ReservationState.SETTLED,
                created_at=current.created_at,
                settled_at=ts,
                actual_usage=usage,
            )
            self._update_reservation(conn, updated, consumed=consumed)
            self._append_budget_event(
                event_type=_EVENT_SETTLED,
                project_id=updated.project_id,
                idempotency_key=f"settle:{updated.reservation_id}",
                payload=updated.to_dict(),
                actor=actor,
                timestamp_utc=ts,
                correlation_id=updated.attempt_id,
            )
            return updated

    def release(
        self,
        reservation_id: str,
        *,
        actor: str = _DEFAULT_ACTOR,
        timestamp_utc: str | None = None,
    ) -> BudgetReservation:
        return self._terminal_without_usage(
            reservation_id,
            target=ReservationState.RELEASED,
            event_type=_EVENT_RELEASED,
            event_key_prefix="release",
            actor=actor,
            timestamp_utc=timestamp_utc,
        )

    def expire(
        self,
        reservation_id: str,
        *,
        actor: str = _DEFAULT_ACTOR,
        timestamp_utc: str | None = None,
    ) -> BudgetReservation:
        return self._terminal_without_usage(
            reservation_id,
            target=ReservationState.EXPIRED,
            event_type=_EVENT_EXPIRED,
            event_key_prefix="expire",
            actor=actor,
            timestamp_utc=timestamp_utc,
        )

    def get_account(self, project_id: str) -> BudgetAccount | None:
        project_id = _require_project_id(project_id)
        conn = self._store._conn
        row = conn.execute(
            "SELECT * FROM budget_accounts WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            return None
        return self._account_from_row(conn, row)

    def get_reservation(self, reservation_id: str) -> BudgetReservation | None:
        reservation_id = _require_nonempty_str("reservation_id", reservation_id)
        return self._get_reservation_locked(self._store._conn, reservation_id)

    def _terminal_without_usage(
        self,
        reservation_id: str,
        *,
        target: ReservationState,
        event_type: str,
        event_key_prefix: str,
        actor: str,
        timestamp_utc: str | None,
    ) -> BudgetReservation:
        reservation_id = _require_nonempty_str("reservation_id", reservation_id)
        actor = _require_nonempty_str("actor", actor)
        ts = timestamp_utc if timestamp_utc is not None else _utc_now()
        with self._store.transaction(immediate=True) as conn:
            current = self._get_reservation_locked(conn, reservation_id)
            if current is None:
                raise BudgetError(f"unknown reservation {reservation_id!r}")
            if current.state is target:
                return current
            if current.state is not ReservationState.RESERVED:
                raise InvalidReservationTransition(
                    f"cannot move reservation {reservation_id!r} from "
                    f"{current.state.value} to {target.value}"
                )
            updated = _build_reservation(
                reservation_id=current.reservation_id,
                project_id=current.project_id,
                attempt_id=current.attempt_id,
                idempotency_key=current.idempotency_key,
                reserved_cost_microusd=current.reserved_cost_microusd,
                reserved_input_tokens=current.reserved_input_tokens,
                reserved_output_tokens=current.reserved_output_tokens,
                state=target,
                created_at=current.created_at,
                settled_at=ts,
            )
            self._update_reservation(conn, updated, consumed=0)
            self._append_budget_event(
                event_type=event_type,
                project_id=updated.project_id,
                idempotency_key=f"{event_key_prefix}:{updated.reservation_id}",
                payload=updated.to_dict(),
                actor=actor,
                timestamp_utc=ts,
                correlation_id=updated.attempt_id,
            )
            return updated

    def _append_budget_event(
        self,
        *,
        event_type: str,
        project_id: str,
        idempotency_key: str,
        payload: dict[str, object],
        actor: str,
        timestamp_utc: str,
        correlation_id: str | None = None,
    ) -> None:
        try:
            envelope = EventEnvelope.build(
                project_id=project_id,
                actor=actor,
                event_type=event_type,
                idempotency_key=idempotency_key,
                payload=payload,
                timestamp_utc=timestamp_utc,
                correlation_id=correlation_id,
            )
        except SchemaValidationError as exc:
            raise BudgetError(f"invalid budget event envelope: {exc}") from exc
        self._store.append(envelope)

    def _insert_reservation(
        self,
        conn: sqlite3.Connection,
        reservation: BudgetReservation,
        *,
        consumed: int,
    ) -> BudgetReservation:
        conn.execute("SAVEPOINT budget_insert")
        try:
            conn.execute(
                """
                INSERT INTO budget_reservations (
                    reservation_id, project_id, attempt_id, idempotency_key,
                    reserved_cost_microusd, reserved_input_tokens,
                    reserved_output_tokens, state, created_at, settled_at,
                    usage_json, consumed_cost_microusd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reservation.reservation_id,
                    reservation.project_id,
                    reservation.attempt_id,
                    reservation.idempotency_key,
                    reservation.reserved_cost_microusd,
                    reservation.reserved_input_tokens,
                    reservation.reserved_output_tokens,
                    reservation.state.value,
                    reservation.created_at,
                    reservation.settled_at,
                    _usage_json(reservation.actual_usage),
                    consumed,
                ),
            )
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK TO SAVEPOINT budget_insert")
            conn.execute("RELEASE SAVEPOINT budget_insert")
            existing = self._get_reservation_by_key(
                conn, reservation.project_id, reservation.idempotency_key
            )
            if existing is not None:
                if not _same_reserve_payload(
                    existing,
                    attempt_id=reservation.attempt_id,
                    reserved_cost_microusd=reservation.reserved_cost_microusd,
                    reserved_input_tokens=reservation.reserved_input_tokens,
                    reserved_output_tokens=reservation.reserved_output_tokens,
                ):
                    raise ReservationConflict(
                        "idempotency key reused with a different reservation "
                        f"payload for project {reservation.project_id!r}"
                    ) from exc
                return existing
            raise BudgetError(f"reservation insert conflict: {exc}") from exc
        conn.execute("RELEASE SAVEPOINT budget_insert")
        return reservation

    def _update_reservation(
        self,
        conn: sqlite3.Connection,
        reservation: BudgetReservation,
        *,
        consumed: int,
    ) -> None:
        conn.execute(
            """
            UPDATE budget_reservations
            SET state = ?, settled_at = ?, usage_json = ?, consumed_cost_microusd = ?
            WHERE reservation_id = ?
            """,
            (
                reservation.state.value,
                reservation.settled_at,
                _usage_json(reservation.actual_usage),
                consumed,
                reservation.reservation_id,
            ),
        )

    def _get_account_locked(
        self, conn: sqlite3.Connection, project_id: str
    ) -> BudgetAccount | None:
        row = conn.execute(
            "SELECT * FROM budget_accounts WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            return None
        return self._account_from_row(conn, row)

    def _account_from_row(
        self, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> BudgetAccount:
        try:
            limit = BudgetLimit(
                kind=BudgetLimitKind(row["limit_kind"]),
                value=row["limit_microusd"],
            )
        except (SchemaValidationError, ValueError) as exc:
            raise BudgetError(
                f"budget account {row['project_id']!r} failed to rehydrate: {exc}"
            ) from exc
        reserved, settled = _project_totals(conn, row["project_id"])
        return BudgetAccount(
            project_id=row["project_id"],
            limit=limit,
            reserved_cost_microusd=reserved,
            settled_cost_microusd=settled,
            consumed_cost_microusd=reserved + settled,
        )

    def _get_reservation_locked(
        self, conn: sqlite3.Connection, reservation_id: str
    ) -> BudgetReservation | None:
        row = conn.execute(
            "SELECT * FROM budget_reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        if row is None:
            return None
        return _reservation_from_row(row)

    def _get_reservation_by_key(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        idempotency_key: str,
    ) -> BudgetReservation | None:
        row = conn.execute(
            """
            SELECT * FROM budget_reservations
            WHERE project_id = ? AND idempotency_key = ?
            """,
            (project_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        return _reservation_from_row(row)


def _project_totals(conn: sqlite3.Connection, project_id: str) -> tuple[int, int]:
    row = conn.execute(
        """
        SELECT
            COALESCE(
                SUM(CASE WHEN state = 'RESERVED' THEN consumed_cost_microusd ELSE 0 END),
                0
            ),
            COALESCE(
                SUM(CASE WHEN state = 'SETTLED' THEN consumed_cost_microusd ELSE 0 END),
                0
            )
        FROM budget_reservations
        WHERE project_id = ?
        """,
        (project_id,),
    ).fetchone()
    return int(row[0]), int(row[1])


def _reservation_from_row(row: sqlite3.Row) -> BudgetReservation:
    raw_usage = row["usage_json"]
    if raw_usage is None:
        actual: dict[str, object] | None = None
    else:
        try:
            loaded = json.loads(raw_usage)
        except json.JSONDecodeError as exc:
            raise BudgetError(
                f"reservation {row['reservation_id']} usage is not valid JSON"
            ) from exc
        if not isinstance(loaded, dict):
            raise BudgetError(
                f"reservation {row['reservation_id']} usage is not a JSON object"
            )
        actual = loaded
    try:
        return BudgetReservation.from_dict(
            {
                "reservation_id": row["reservation_id"],
                "project_id": row["project_id"],
                "attempt_id": row["attempt_id"],
                "idempotency_key": row["idempotency_key"],
                "reserved_cost_microusd": row["reserved_cost_microusd"],
                "reserved_input_tokens": row["reserved_input_tokens"],
                "reserved_output_tokens": row["reserved_output_tokens"],
                "state": row["state"],
                "created_at": row["created_at"],
                "settled_at": row["settled_at"],
                "actual_usage": actual,
            }
        )
    except SchemaValidationError as exc:
        raise BudgetError(
            f"reservation {row['reservation_id']} failed to rehydrate: {exc}"
        ) from exc


def _build_reservation(**kwargs: object) -> BudgetReservation:
    try:
        return BudgetReservation(**kwargs)  # type: ignore[arg-type]
    except SchemaValidationError as exc:
        raise BudgetError(str(exc)) from exc


def _split_limit(limit: BudgetLimit) -> tuple[str, int | None]:
    if limit.kind is BudgetLimitKind.UNLIMITED:
        return BudgetLimitKind.UNLIMITED.value, None
    value = _require_nonneg_int("BudgetLimit.value", limit.value)
    return BudgetLimitKind.LIMITED.value, value


def _same_reserve_payload(
    existing: BudgetReservation,
    *,
    attempt_id: str,
    reserved_cost_microusd: int,
    reserved_input_tokens: int,
    reserved_output_tokens: int,
) -> bool:
    return (
        existing.attempt_id == attempt_id
        and existing.reserved_cost_microusd == reserved_cost_microusd
        and existing.reserved_input_tokens == reserved_input_tokens
        and existing.reserved_output_tokens == reserved_output_tokens
    )


def _usage_equal(left: Optional[UsageRecord], right: UsageRecord) -> bool:
    if left is None:
        return False
    return left.to_dict() == right.to_dict()


def _consumed_after_settle(reserved_cost_microusd: int, usage: UsageRecord) -> int:
    qty = usage.cost_microusd
    if qty.status is ObservationStatus.UNKNOWN:
        # unknown ≠ 0: keep the reserved amount as consumed.
        return reserved_cost_microusd
    if qty.status is ObservationStatus.OBSERVED:
        return _require_nonneg_int("cost_microusd.value", qty.value)
    raise BudgetError(f"unsupported observation status {qty.status!r}")


def _usage_json(usage: Optional[UsageRecord]) -> str | None:
    if usage is None:
        return None
    return canonical_json(usage.to_dict())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_project_id(project_id: object) -> str:
    text = _require_nonempty_str("project_id", project_id)
    if _SAFE_IDENTIFIER_RE.fullmatch(text) is None:
        raise BudgetError("project_id is not a safe identifier")
    return text


def _require_nonempty_str(name: str, value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise BudgetError(f"{name} must be a nonempty str")
    return value


def _require_nonneg_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BudgetError(f"{name} must be a nonnegative int, not {type(value).__name__}")
    if value < 0:
        raise BudgetError(f"{name} must be a nonnegative int")
    if value > _SQLITE_MAX_INT:
        raise BudgetError(f"{name} exceeds the sqlite INTEGER range")
    return value
