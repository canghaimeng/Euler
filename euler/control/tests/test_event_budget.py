"""Offline tests for the research-control event store and budget ledger.

No network, no runtime projects, no Fact Graph / gateway / verify imports.
Covers schema version fail-closed, crash/reopen recovery, idempotent
append/reserve/settle, over-budget behavior, terminal transitions, unknown
usage, and event tamper detection.

Runs standalone (``python -m euler.control.tests.test_event_budget``)
and under pytest.
"""

from __future__ import annotations

import ast
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from euler.control.budget import (
    BudgetAccount,
    BudgetError,
    BudgetExceeded,
    BudgetLedger,
    InvalidReservationTransition,
    ReservationConflict,
)
from euler.control.event_store import (
    BUSY_TIMEOUT_MS,
    STORE_SCHEMA_VERSION,
    AppendResult,
    EventIntegrityError,
    EventStore,
    EventStoreError,
    IdempotencyConflict,
    StoreSchemaError,
)
from euler.control.ids import canonical_json, sha256_hex
from euler.control.schema import (
    BudgetLimit,
    BudgetLimitKind,
    EventEnvelope,
    ObservationStatus,
    ObservedQuantity,
    ReservationState,
    UsageRecord,
)

_ROOT = Path(__file__).resolve().parents[1]


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__} from {getattr(fn, '__name__', fn)}")


def _hex(*parts: str) -> str:
    return sha256_hex("|".join(parts))


def _unknown(unit: str) -> ObservedQuantity:
    return ObservedQuantity(unit=unit, status=ObservationStatus.UNKNOWN, value=None)


def _observed(unit: str, value: int) -> ObservedQuantity:
    return ObservedQuantity(unit=unit, status=ObservationStatus.OBSERVED, value=value)


def _usage(**overrides: ObservedQuantity) -> UsageRecord:
    fields = {
        "input_tokens": _unknown("tokens"),
        "output_tokens": _unknown("tokens"),
        "reasoning_tokens": _unknown("tokens"),
        "cached_tokens": _unknown("tokens"),
        "cost_microusd": _unknown("microusd"),
        "wall_ms": _unknown("ms"),
        "tool_cpu_ms": _unknown("ms"),
        "tool_gpu_ms": _unknown("ms"),
    }
    fields.update(overrides)
    return UsageRecord(**fields)


def _envelope(**overrides) -> EventEnvelope:
    kwargs = dict(
        project_id="proj",
        actor="worker-1",
        event_type="attempt.started",
        idempotency_key="idem-1",
        payload={"n": 1, "note": "命题"},
        timestamp_utc="2026-01-01T00:00:00Z",
    )
    kwargs.update(overrides)
    return EventEnvelope.build(**kwargs)


def _limited(value: int) -> BudgetLimit:
    return BudgetLimit(kind=BudgetLimitKind.LIMITED, value=value)


def _unlimited() -> BudgetLimit:
    return BudgetLimit(kind=BudgetLimitKind.UNLIMITED, value=None)


def _event_types(store: EventStore, project_id: str = "proj") -> list[str]:
    return [env.event_type for _seq, env in store.iter_project(project_id)]


@contextmanager
def _tmpdir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@contextmanager
def _open_store():
    with _tmpdir() as d:
        path = d / "control.sqlite3"
        store = EventStore.open(path)
        try:
            yield store, path
        finally:
            store.close()


# --------------------------------------------------------------------------- #
# Event store: open, pragmas, schema version                                  #
# --------------------------------------------------------------------------- #


def test_open_enforces_wal_foreign_keys_synchronous_and_busy_timeout():
    with _open_store() as (store, path):
        conn = store._conn
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        assert int(conn.execute("PRAGMA synchronous").fetchone()[0]) == 2
        assert int(conn.execute("PRAGMA busy_timeout").fetchone()[0]) == BUSY_TIMEOUT_MS
        via_ctor = EventStore(path)
        try:
            assert via_ctor.get("0" * 64) is None
        finally:
            via_ctor.close()


def test_unsupported_schema_version_fail_closed():
    with _tmpdir() as d:
        path = d / "control.sqlite3"
        EventStore.open(path).close()
        raw = sqlite3.connect(path)
        raw.execute("UPDATE schema_meta SET version = 99")
        raw.commit()
        raw.close()
        _raises(StoreSchemaError, EventStore.open, path)

        EventStore.open(d / "ok.sqlite3").close()
        raw = sqlite3.connect(d / "ok.sqlite3")
        raw.execute("INSERT INTO schema_meta (version) VALUES (1)")
        raw.commit()
        raw.close()
        _raises(StoreSchemaError, EventStore.open, d / "ok.sqlite3")

        EventStore.open(d / "missing.sqlite3").close()
        raw = sqlite3.connect(d / "missing.sqlite3")
        raw.execute("DROP TABLE schema_meta")
        raw.commit()
        raw.close()
        _raises(StoreSchemaError, EventStore.open, d / "missing.sqlite3")

        EventStore.open(d / "notables.sqlite3").close()
        raw = sqlite3.connect(d / "notables.sqlite3")
        raw.execute("DROP TABLE events")
        raw.commit()
        raw.close()
        _raises(StoreSchemaError, EventStore.open, d / "notables.sqlite3")


# --------------------------------------------------------------------------- #
# Event store: append idempotency and integrity                               #
# --------------------------------------------------------------------------- #


def test_duplicate_append_returns_original_and_does_not_add_row():
    with _open_store() as (store, _path):
        first = _envelope(timestamp_utc="2026-01-01T00:00:00Z")
        retry = _envelope(timestamp_utc="2026-01-01T00:00:01.500Z")
        created = store.append(first)
        assert isinstance(created, AppendResult)
        assert created.created is True
        assert created.event.event_id == first.event_id
        replay = store.append(retry)
        assert replay.created is False
        assert replay.event.event_id == first.event_id
        assert replay.event.timestamp_utc == first.timestamp_utc
        rows = list(store.iter_project("proj"))
        assert len(rows) == 1
        seq, env = rows[0]
        assert seq == 1
        assert env.event_id == first.event_id
        assert store.verify_integrity() == 1
        later = list(store.iter_project("proj", after_seq=seq))
        assert later == []
        assert store.get(first.event_id).payload["note"] == "命题"


def test_same_idempotency_key_changed_payload_conflicts():
    with _open_store() as (store, _path):
        store.append(_envelope(payload={"n": 1}))
        _raises(
            IdempotencyConflict,
            store.append,
            _envelope(payload={"n": 2}),
        )
        assert store.verify_integrity() == 1


def test_transaction_rollback_does_not_persist_events():
    with _open_store() as (store, _path):
        env = _envelope()
        try:
            with store.transaction(immediate=True):
                store.append(env)
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert store.get(env.event_id) is None
        assert list(store.iter_project("proj")) == []


def test_nested_caught_error_rolls_back_and_raises():
    with _open_store() as (store, _path):
        env = _envelope()

        def body():
            with store.transaction(immediate=True):
                store.append(env)
                try:
                    with store.transaction(immediate=True):
                        raise ValueError("inner")
                except ValueError:
                    pass

        _raises(EventStoreError, body)
        assert store.get(env.event_id) is None
        assert list(store.iter_project("proj")) == []


def test_event_tamper_is_detected_on_read_and_integrity_scan():
    with _tmpdir() as d:
        path = d / "control.sqlite3"
        env = _envelope()
        with EventStore.open(path) as store:
            store.append(env)
            assert store.get(env.event_id) is not None

        raw = sqlite3.connect(path)
        raw.execute("DROP TRIGGER IF EXISTS events_no_update")
        raw.execute(
            "UPDATE events SET payload = ? WHERE event_id = ?",
            (canonical_json({"n": 2, "note": "命题"}), env.event_id),
        )
        raw.commit()
        raw.close()

        with EventStore.open(path) as store:
            _raises(EventIntegrityError, store.get, env.event_id)
            _raises(EventIntegrityError, store.verify_integrity)
            _raises(EventIntegrityError, lambda: list(store.iter_project("proj")))

        EventStore.open(d / "hash.sqlite3").close()
        with EventStore.open(d / "hash.sqlite3") as store:
            store.append(env)
        raw = sqlite3.connect(d / "hash.sqlite3")
        raw.execute("DROP TRIGGER IF EXISTS events_no_update")
        raw.execute(
            "UPDATE events SET payload_hash = ? WHERE event_id = ?",
            ("0" * 64, env.event_id),
        )
        raw.commit()
        raw.close()
        with EventStore.open(d / "hash.sqlite3") as store:
            _raises(EventIntegrityError, store.get, env.event_id)

        EventStore.open(d / "append_only.sqlite3").close()
        with EventStore.open(d / "append_only.sqlite3") as store:
            store.append(env)
            _raises(
                sqlite3.IntegrityError,
                store._conn.execute,
                "UPDATE events SET actor = 'x'",
            )


# --------------------------------------------------------------------------- #
# Crash / reopen                                                              #
# --------------------------------------------------------------------------- #


def test_crash_reopen_recovers_events_and_reservations():
    with _tmpdir() as d:
        path = d / "control.sqlite3"
        env = _envelope()
        with EventStore.open(path) as store:
            ledger = BudgetLedger(store)
            store.append(env)
            ledger.ensure_account("proj", _limited(5_000))
            reserved = ledger.reserve(
                project_id="proj",
                attempt_id=_hex("attempt"),
                idempotency_key="reserve-1",
                reserved_cost_microusd=1200,
                reserved_input_tokens=10,
                reserved_output_tokens=4,
                timestamp_utc="2026-01-01T00:00:00Z",
            )
            reservation_id = reserved.reservation_id
            event_id = env.event_id

        with EventStore.open(path) as store:
            recovered_event = store.get(event_id)
            assert recovered_event is not None
            assert recovered_event.payload == env.payload
            ledger = BudgetLedger(store)
            account = ledger.get_account("proj")
            assert account is not None
            assert account.limit.kind is BudgetLimitKind.LIMITED
            assert account.limit.value == 5_000
            assert account.reserved_cost_microusd == 1200
            got = ledger.get_reservation(reservation_id)
            assert got is not None
            assert got.state is ReservationState.RESERVED
            assert got.reserved_cost_microusd == 1200
            assert store.verify_integrity() == 3
            types = _event_types(store)
            assert types.count("attempt.started") == 1
            assert types.count("budget.account_created") == 1
            assert types.count("budget.reserved") == 1


# --------------------------------------------------------------------------- #
# Budget: reserve / settle / over-budget / terminal                           #
# --------------------------------------------------------------------------- #


def test_missing_account_is_not_unlimited():
    with _open_store() as (store, _path):
        ledger = BudgetLedger(store)
        assert ledger.get_account("proj") is None
        _raises(
            BudgetError,
            ledger.reserve,
            "proj",
            _hex("attempt"),
            "k",
            1,
        )
        ledger.ensure_account("proj", _unlimited())
        account = ledger.get_account("proj")
        assert isinstance(account, BudgetAccount)
        assert account.limit.kind is BudgetLimitKind.UNLIMITED
        assert account.remaining_cost_microusd is None
        huge = ledger.reserve(
            project_id="proj",
            attempt_id=_hex("attempt"),
            idempotency_key="k",
            reserved_cost_microusd=10**12,
        )
        assert huge.state is ReservationState.RESERVED


def test_duplicate_reserve_and_over_budget():
    with _open_store() as (store, _path):
        ledger = BudgetLedger(store)
        ledger.ensure_account("proj", _limited(1000))
        first = ledger.reserve(
            project_id="proj",
            attempt_id=_hex("attempt"),
            idempotency_key="same",
            reserved_cost_microusd=600,
            timestamp_utc="2026-01-01T00:00:00Z",
        )
        replay = ledger.reserve(
            project_id="proj",
            attempt_id=_hex("attempt"),
            idempotency_key="same",
            reserved_cost_microusd=600,
            timestamp_utc="2026-01-01T00:00:05Z",
        )
        assert replay.reservation_id == first.reservation_id
        assert replay.created_at == first.created_at
        assert ledger.get_account("proj").consumed_cost_microusd == 600
        _raises(
            ReservationConflict,
            ledger.reserve,
            "proj",
            _hex("attempt-other"),
            "same",
            600,
        )
        _raises(
            BudgetExceeded,
            ledger.reserve,
            "proj",
            _hex("attempt-2"),
            "other",
            500,
        )
        second = ledger.reserve(
            project_id="proj",
            attempt_id=_hex("attempt-2"),
            idempotency_key="other",
            reserved_cost_microusd=400,
        )
        assert ledger.get_account("proj").consumed_cost_microusd == 1000
        _raises(
            BudgetExceeded,
            ledger.reserve,
            "proj",
            _hex("attempt-3"),
            "overflow",
            1,
        )
        assert second.state is ReservationState.RESERVED
        _raises(BudgetError, ledger.reserve, "proj", _hex("a"), "float", 1.5)
        _raises(BudgetError, ledger.reserve, "proj", _hex("a"), "bool", True)


def test_duplicate_settle_does_not_double_charge():
    with _open_store() as (store, _path):
        ledger = BudgetLedger(store)
        ledger.ensure_account("proj", _limited(1000))
        reserved = ledger.reserve(
            project_id="proj",
            attempt_id=_hex("attempt"),
            idempotency_key="s1",
            reserved_cost_microusd=800,
            timestamp_utc="2026-01-01T00:00:00Z",
        )
        usage = _usage(cost_microusd=_observed("microusd", 250))
        settled = ledger.settle(
            reserved.reservation_id,
            usage,
            timestamp_utc="2026-01-01T00:00:05Z",
        )
        assert settled.state is ReservationState.SETTLED
        replay = ledger.settle(reserved.reservation_id, usage)
        assert replay.reservation_id == settled.reservation_id
        assert replay.settled_at == settled.settled_at
        account = ledger.get_account("proj")
        assert account.settled_cost_microusd == 250
        assert account.reserved_cost_microusd == 0
        assert account.consumed_cost_microusd == 250
        other = _usage(cost_microusd=_observed("microusd", 251))
        _raises(ReservationConflict, ledger.settle, reserved.reservation_id, other)
        assert ledger.get_account("proj").consumed_cost_microusd == 250


def test_unknown_usage_is_not_rewritten_to_zero():
    with _open_store() as (store, _path):
        ledger = BudgetLedger(store)
        ledger.ensure_account("proj", _limited(1000))
        reserved = ledger.reserve(
            project_id="proj",
            attempt_id=_hex("attempt"),
            idempotency_key="unknown",
            reserved_cost_microusd=400,
            timestamp_utc="2026-01-01T00:00:00Z",
        )
        unknown = _usage()
        assert unknown.cost_microusd.status is ObservationStatus.UNKNOWN
        assert unknown.cost_microusd.value is None
        settled = ledger.settle(
            reserved.reservation_id,
            unknown,
            timestamp_utc="2026-01-01T00:00:05Z",
        )
        assert settled.actual_usage is not None
        assert settled.actual_usage.cost_microusd.status is ObservationStatus.UNKNOWN
        assert settled.actual_usage.cost_microusd.value is None
        account = ledger.get_account("proj")
        assert account.consumed_cost_microusd == 400
        assert account.remaining_cost_microusd == 600

        zero_res = ledger.reserve(
            project_id="proj",
            attempt_id=_hex("attempt-zero"),
            idempotency_key="zero",
            reserved_cost_microusd=100,
            timestamp_utc="2026-01-01T00:00:06Z",
        )
        zero_usage = _usage(cost_microusd=_observed("microusd", 0))
        zero_settled = ledger.settle(
            zero_res.reservation_id,
            zero_usage,
            timestamp_utc="2026-01-01T00:00:07Z",
        )
        assert zero_settled.actual_usage.cost_microusd.value == 0
        account = ledger.get_account("proj")
        assert account.consumed_cost_microusd == 400
        assert account.remaining_cost_microusd == 600


def test_terminal_transitions_reject_non_identical_replay():
    with _open_store() as (store, _path):
        ledger = BudgetLedger(store)
        ledger.ensure_account("proj", _limited(5000))

        released = ledger.reserve(
            project_id="proj",
            attempt_id=_hex("rel"),
            idempotency_key="rel",
            reserved_cost_microusd=100,
            timestamp_utc="2026-01-01T00:00:00Z",
        )
        released_row = ledger.release(
            released.reservation_id,
            actor="ops",
            timestamp_utc="2026-01-01T00:00:01Z",
        )
        assert released_row.settled_at == "2026-01-01T00:00:01Z"
        assert released_row.actual_usage is None
        again = ledger.release(released.reservation_id)
        assert again.state is ReservationState.RELEASED
        assert again.settled_at == "2026-01-01T00:00:01Z"
        _raises(
            InvalidReservationTransition,
            ledger.settle,
            released.reservation_id,
            _usage(cost_microusd=_observed("microusd", 1)),
        )
        _raises(InvalidReservationTransition, ledger.expire, released.reservation_id)
        assert ledger.get_account("proj").consumed_cost_microusd == 0

        expired = ledger.reserve(
            project_id="proj",
            attempt_id=_hex("exp"),
            idempotency_key="exp",
            reserved_cost_microusd=100,
            timestamp_utc="2026-01-01T00:00:02Z",
        )
        expired_row = ledger.expire(
            expired.reservation_id,
            actor="ops",
            timestamp_utc="2026-01-01T00:00:03Z",
        )
        assert expired_row.settled_at == "2026-01-01T00:00:03Z"
        assert expired_row.actual_usage is None
        assert ledger.expire(expired.reservation_id).state is ReservationState.EXPIRED
        _raises(
            InvalidReservationTransition,
            ledger.settle,
            expired.reservation_id,
            _usage(cost_microusd=_observed("microusd", 1)),
        )
        _raises(InvalidReservationTransition, ledger.release, expired.reservation_id)

        settled = ledger.reserve(
            project_id="proj",
            attempt_id=_hex("set"),
            idempotency_key="set",
            reserved_cost_microusd=100,
            timestamp_utc="2026-01-01T00:00:04Z",
        )
        ledger.settle(
            settled.reservation_id,
            _usage(cost_microusd=_observed("microusd", 40)),
            timestamp_utc="2026-01-01T00:00:05Z",
        )
        _raises(InvalidReservationTransition, ledger.release, settled.reservation_id)
        _raises(InvalidReservationTransition, ledger.expire, settled.reservation_id)
        assert ledger.get_account("proj").consumed_cost_microusd == 40


def test_concurrent_reserve_does_not_exceed_limited_account():
    with _tmpdir() as d:
        path = d / "control.sqlite3"
        with EventStore.open(path) as store:
            BudgetLedger(store).ensure_account("proj", _limited(1000))

        barrier = threading.Barrier(2)
        results: list[object] = []
        exceeded = []
        unexpected = []
        lock = threading.Lock()

        def worker(n: int) -> None:
            try:
                with EventStore.open(path) as store:
                    ledger = BudgetLedger(store)
                    barrier.wait(timeout=5)
                    try:
                        reserved = ledger.reserve(
                            project_id="proj",
                            attempt_id=_hex(f"attempt-{n}"),
                            idempotency_key=f"k-{n}",
                            reserved_cost_microusd=700,
                        )
                        with lock:
                            results.append(reserved)
                    except BudgetExceeded:
                        with lock:
                            exceeded.append(n)
            except Exception as exc:  # pragma: no cover - diagnostic
                with lock:
                    unexpected.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
            assert not thread.is_alive()
        assert unexpected == []
        assert len(results) == 1
        assert len(exceeded) == 1
        with EventStore.open(path) as store:
            account = BudgetLedger(store).get_account("proj")
            assert account is not None
            assert account.consumed_cost_microusd == 700
            assert account.consumed_cost_microusd <= 1000


def test_concurrent_duplicate_reserve_is_idempotent():
    with _tmpdir() as d:
        path = d / "control.sqlite3"
        with EventStore.open(path) as store:
            BudgetLedger(store).ensure_account("proj", _limited(1000))

        barrier = threading.Barrier(2)
        results: list[str] = []
        unexpected = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                with EventStore.open(path) as store:
                    ledger = BudgetLedger(store)
                    barrier.wait(timeout=5)
                    reserved = ledger.reserve(
                        project_id="proj",
                        attempt_id=_hex("shared"),
                        idempotency_key="shared",
                        reserved_cost_microusd=1000,
                    )
                    with lock:
                        results.append(reserved.reservation_id)
            except Exception as exc:  # pragma: no cover - diagnostic
                with lock:
                    unexpected.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
            assert not thread.is_alive()
        assert unexpected == []
        assert len(results) == 2
        assert results[0] == results[1]
        with EventStore.open(path) as store:
            account = BudgetLedger(store).get_account("proj")
            assert account is not None
            assert account.consumed_cost_microusd == 1000
            assert _event_types(store).count("budget.reserved") == 1


def test_ensure_account_is_create_or_assert_same():
    with _open_store() as (store, _path):
        ledger = BudgetLedger(store)
        first = ledger.ensure_account(
            "proj",
            _limited(1000),
            actor="acct",
            timestamp_utc="2026-01-01T00:00:00Z",
        )
        replay = ledger.ensure_account("proj", _limited(1000))
        assert replay.limit.value == first.limit.value
        assert _event_types(store).count("budget.account_created") == 1
        events = [env for _seq, env in store.iter_project("proj")]
        assert events[0].actor == "acct"
        assert events[0].timestamp_utc == "2026-01-01T00:00:00Z"
        assert events[0].payload["limit"] == {"kind": "LIMITED", "value": 1000}
        _raises(BudgetError, ledger.ensure_account, "proj", _limited(2000))
        _raises(BudgetError, ledger.ensure_account, "proj", _unlimited())
        assert ledger.get_account("proj").limit.value == 1000
        assert _event_types(store).count("budget.account_created") == 1


def test_budget_events_are_written_once_per_transition():
    with _open_store() as (store, _path):
        ledger = BudgetLedger(store)
        ledger.ensure_account(
            "proj",
            _limited(5000),
            actor="acct",
            timestamp_utc="2026-01-01T00:00:00Z",
        )
        reserved = ledger.reserve(
            project_id="proj",
            attempt_id=_hex("attempt"),
            idempotency_key="once",
            reserved_cost_microusd=100,
            actor="worker",
            timestamp_utc="2026-01-01T00:00:01Z",
        )
        ledger.reserve(
            project_id="proj",
            attempt_id=_hex("attempt"),
            idempotency_key="once",
            reserved_cost_microusd=100,
            timestamp_utc="2026-01-01T00:00:02Z",
        )
        usage = _usage(cost_microusd=_observed("microusd", 40))
        settled = ledger.settle(
            reserved.reservation_id,
            usage,
            actor="worker",
            timestamp_utc="2026-01-01T00:00:03Z",
        )
        ledger.settle(reserved.reservation_id, usage)
        types = _event_types(store)
        assert types.count("budget.account_created") == 1
        assert types.count("budget.reserved") == 1
        assert types.count("budget.settled") == 1
        reserved_events = [
            env for _seq, env in store.iter_project("proj") if env.event_type == "budget.reserved"
        ]
        assert reserved_events[0].actor == "worker"
        assert reserved_events[0].timestamp_utc == "2026-01-01T00:00:01Z"
        assert reserved_events[0].payload["reservation_id"] == reserved.reservation_id
        assert reserved_events[0].idempotency_key == f"reserve:{reserved.idempotency_key}"
        settled_events = [
            env for _seq, env in store.iter_project("proj") if env.event_type == "budget.settled"
        ]
        assert settled_events[0].payload["state"] == "SETTLED"
        assert settled_events[0].payload["actual_usage"] == usage.to_dict()
        assert settled.settled_at == "2026-01-01T00:00:03Z"

        other = ledger.reserve(
            project_id="proj",
            attempt_id=_hex("rel"),
            idempotency_key="rel-once",
            reserved_cost_microusd=50,
            timestamp_utc="2026-01-01T00:00:04Z",
        )
        ledger.release(
            other.reservation_id,
            actor="ops",
            timestamp_utc="2026-01-01T00:00:05Z",
        )
        ledger.release(other.reservation_id)
        assert _event_types(store).count("budget.released") == 1

        exp = ledger.reserve(
            project_id="proj",
            attempt_id=_hex("exp"),
            idempotency_key="exp-once",
            reserved_cost_microusd=50,
            timestamp_utc="2026-01-01T00:00:06Z",
        )
        ledger.expire(
            exp.reservation_id,
            actor="ops",
            timestamp_utc="2026-01-01T00:00:07Z",
        )
        ledger.expire(exp.reservation_id)
        assert _event_types(store).count("budget.expired") == 1


def test_injected_event_failure_rolls_back_budget_state():
    with _open_store() as (store, _path):
        ledger = BudgetLedger(store)
        ledger.ensure_account("proj", _limited(1000))
        original = store.append

        def boom(_envelope):
            raise EventStoreError("injected event failure")

        store.append = boom  # type: ignore[method-assign]
        try:
            def reserve():
                ledger.reserve(
                    project_id="proj",
                    attempt_id=_hex("attempt"),
                    idempotency_key="fail-event",
                    reserved_cost_microusd=100,
                    timestamp_utc="2026-01-01T00:00:00Z",
                )

            _raises(EventStoreError, reserve)
        finally:
            store.append = original
        assert ledger.get_account("proj").consumed_cost_microusd == 0
        assert "budget.reserved" not in _event_types(store)

        reserved = ledger.reserve(
            project_id="proj",
            attempt_id=_hex("attempt"),
            idempotency_key="fail-settle",
            reserved_cost_microusd=100,
            timestamp_utc="2026-01-01T00:00:01Z",
        )
        store.append = boom  # type: ignore[method-assign]
        try:
            def settle():
                ledger.settle(
                    reserved.reservation_id,
                    _usage(cost_microusd=_observed("microusd", 10)),
                    timestamp_utc="2026-01-01T00:00:02Z",
                )

            _raises(EventStoreError, settle)
        finally:
            store.append = original
        got = ledger.get_reservation(reserved.reservation_id)
        assert got is not None
        assert got.state is ReservationState.RESERVED
        assert got.actual_usage is None
        assert got.settled_at is None
        assert "budget.settled" not in _event_types(store)


def test_release_and_expire_honor_actor_and_terminal_timestamp():
    with _open_store() as (store, _path):
        ledger = BudgetLedger(store)
        ledger.ensure_account("proj", _limited(1000))
        reserved = ledger.reserve(
            project_id="proj",
            attempt_id=_hex("rel"),
            idempotency_key="term-rel",
            reserved_cost_microusd=10,
            timestamp_utc="2026-01-01T00:00:00Z",
        )
        released = ledger.release(
            reserved.reservation_id,
            actor="ops",
            timestamp_utc="2026-01-01T00:00:09Z",
        )
        assert released.settled_at == "2026-01-01T00:00:09Z"
        assert released.actual_usage is None
        released_events = [
            env for _seq, env in store.iter_project("proj") if env.event_type == "budget.released"
        ]
        assert len(released_events) == 1
        assert released_events[0].actor == "ops"
        assert released_events[0].timestamp_utc == "2026-01-01T00:00:09Z"
        assert released_events[0].payload["settled_at"] == "2026-01-01T00:00:09Z"
        assert released_events[0].payload["actual_usage"] is None
        assert released_events[0].idempotency_key == f"release:{reserved.reservation_id}"

        exp = ledger.reserve(
            project_id="proj",
            attempt_id=_hex("exp"),
            idempotency_key="term-exp",
            reserved_cost_microusd=10,
            timestamp_utc="2026-01-01T00:00:10Z",
        )
        expired = ledger.expire(
            exp.reservation_id,
            actor="janitor",
            timestamp_utc="2026-01-01T00:00:11Z",
        )
        assert expired.settled_at == "2026-01-01T00:00:11Z"
        expired_events = [
            env for _seq, env in store.iter_project("proj") if env.event_type == "budget.expired"
        ]
        assert len(expired_events) == 1
        assert expired_events[0].actor == "janitor"
        assert expired_events[0].timestamp_utc == "2026-01-01T00:00:11Z"
        assert expired_events[0].payload["settled_at"] == "2026-01-01T00:00:11Z"
        assert expired_events[0].payload.get("actual_usage") is None


def test_production_modules_do_not_import_truth_stores():
    forbidden_modules = {
        "euler.core.factgraph",
        "euler.core.FactGraph",
        "euler.core.global_memory",
        "euler.gateway",
        "euler.gateway.server",
        "euler.verify",
        "euler.verify.service",
        "euler.execution.launch_receipt",
        "euler.execution.memory_contract_audit",
        "euler.execution.sol_guidance",
    }
    forbidden_names = {"FactGraph", "fact_submit", "GlobalMemory"}
    for filename in ("event_store.py", "budget.py"):
        path = _ROOT / filename
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        imported: set[str] = set()
        named: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
                    named.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                for alias in node.names:
                    named.add(alias.name)
        assert imported.isdisjoint(forbidden_modules), filename
        assert named.isdisjoint(forbidden_names), filename
        assert "fact_graph/" not in source
        assert "global_memory/" not in source
        assert "FactGraph.add" not in source
        assert "fact_submit" not in source


def main() -> None:
    tests = [
        test_open_enforces_wal_foreign_keys_synchronous_and_busy_timeout,
        test_unsupported_schema_version_fail_closed,
        test_duplicate_append_returns_original_and_does_not_add_row,
        test_same_idempotency_key_changed_payload_conflicts,
        test_transaction_rollback_does_not_persist_events,
        test_nested_caught_error_rolls_back_and_raises,
        test_event_tamper_is_detected_on_read_and_integrity_scan,
        test_crash_reopen_recovers_events_and_reservations,
        test_missing_account_is_not_unlimited,
        test_duplicate_reserve_and_over_budget,
        test_duplicate_settle_does_not_double_charge,
        test_unknown_usage_is_not_rewritten_to_zero,
        test_terminal_transitions_reject_non_identical_replay,
        test_concurrent_reserve_does_not_exceed_limited_account,
        test_concurrent_duplicate_reserve_is_idempotent,
        test_ensure_account_is_create_or_assert_same,
        test_budget_events_are_written_once_per_transition,
        test_injected_event_failure_rolls_back_budget_state,
        test_release_and_expire_honor_actor_and_terminal_timestamp,
        test_production_modules_do_not_import_truth_stores,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL EVENT/BUDGET TESTS PASSED")


if __name__ == "__main__":
    main()
