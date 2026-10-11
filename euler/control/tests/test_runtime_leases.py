"""Offline tests for runtime leases, issue/complete, replay, and recovery.

Covers the runtime audit surface: eight dimensions, atomic concurrent
slot/budget recheck, unknown infra/cancel/expire settlement, full snapshot
replay, typed progress reject/accept, verifier slot, and after_seq fail-closed.

Runs standalone (``python -m euler.control.tests.test_runtime_leases``).
"""

from __future__ import annotations

import ast
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from euler.control.artifacts import ArtifactStore
from euler.control.budget import BudgetLedger
from euler.control.cache import build_context_cache_key
from euler.control.context import ContextLimits
from euler.control.contracts import (
    MathematicalScope,
    QuantifierBinding,
    QuantifierKind,
)
from euler.control.event_store import EventStore, EventStoreError
from euler.control.ids import canonical_json, sha256_hex
from euler.control.policy import (
    BudgetClass,
    BudgetPolicy,
    DimensionLimit,
    ResourceSnapshot,
    ResourceVector,
    SlotKind,
)
from euler.control.progress import ProgressKind, ProgressReceipt
from euler.control.recurrence import RecurrenceRecommendation
from euler.control.runtime import (
    LeaseConflict,
    LeaseStatus,
    ResourceDisposition,
    RuntimeCoordinator,
    RuntimeError,
    RuntimeSnapshot,
)
from euler.control.scheduler import (
    ActionKind,
    CandidateAction,
    DependencyValidity,
    PriorityClass,
    build_action_context_pair,
    slot_kind_for_action,
)
from euler.control.schema import (
    BudgetLimit,
    BudgetLimitKind,
    EventEnvelope,
    InfrastructureStatus,
    MathematicalStatus,
    ObservationStatus,
    ObservedQuantity,
    ReservationState,
    UsageRecord,
)
from euler.control.views import ResearchView

_ROOT = Path(__file__).resolve().parents[1]
_TS = "2026-01-01T00:00:00Z"


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return
    raise AssertionError(
        f"expected {exc.__name__} from {getattr(fn, '__name__', fn)}"
    )


def _hex(*parts: str) -> str:
    return sha256_hex("|".join(parts))


def _scope(*, domain: str = "Z") -> MathematicalScope:
    return MathematicalScope.build(
        assumptions=("n is an integer",),
        quantifiers=(
            QuantifierBinding(variable="n", kind=QuantifierKind.FORALL, domain=domain),
        ),
        objects=(),
        parameter_domain=domain,
        definitions_hash=_hex("defs", domain),
    )


def _vector(**overrides: int) -> ResourceVector:
    payload = {
        "cost_microusd": 100,
        "input_tokens": 10,
        "output_tokens": 20,
        "reasoning_tokens": 11,
        "cached_tokens": 12,
        "wall_ms": 1000,
        "tool_cpu_ms": 5,
        "tool_gpu_ms": 0,
    }
    payload.update(overrides)
    return ResourceVector(**payload)


def _policy(**overrides: object) -> BudgetPolicy:
    unbound = DimensionLimit.unlimited()
    kwargs: dict[str, object] = {
        "cost_microusd": DimensionLimit.limited(10_000),
        "input_tokens": unbound,
        "output_tokens": unbound,
        "reasoning_tokens": unbound,
        "cached_tokens": unbound,
        "wall_ms": unbound,
        "tool_cpu_ms": unbound,
        "tool_gpu_ms": unbound,
        "max_reasoning_slots": 2,
        "max_compute_slots": 1,
        "max_retrieval_slots": 1,
        "max_verifier_slots": 1,
    }
    kwargs.update(overrides)
    return BudgetPolicy(**kwargs)  # type: ignore[arg-type]


def _pair(**overrides: object):
    kwargs: dict[str, object] = {
        "contract_id": _hex("contract"),
        "obligation_id": _hex("obligation"),
        "concrete_gap": "close the even-integer case",
        "output_contract": "return a KERNEL_CHECKED_PROOF",
        "tool_environment_hash": _hex("tool"),
        "acceptance_policy_hash": _hex("accept"),
        "reference_versions_hash": _hex("refs"),
        "limits": ContextLimits.unlimited(),
        "action_type": "prove",
        "mechanism": "direct",
        "model_id": "grok-4.6",
        "effort": "high",
        "task_mode": "worker",
        "parameters_json": canonical_json({"k": 1}),
        "dependency_hash": _hex("dep"),
        "definitions_hash": _hex("defs"),
        "expand_evidence_content": False,
    }
    kwargs.update(overrides)
    return build_action_context_pair(**kwargs)  # type: ignore[arg-type]


def _candidate(
    tag: str,
    *,
    action_kind: ActionKind = ActionKind.PROVE,
    budget_class: BudgetClass = BudgetClass.NORMAL,
    requested: ResourceVector | None = None,
    **pair_overrides: object,
) -> CandidateAction:
    if "action_type" not in pair_overrides:
        pair_overrides["action_type"] = action_kind.value.lower()
    if "mechanism" not in pair_overrides:
        pair_overrides["mechanism"] = tag
    spec, request, seed = _pair(**pair_overrides)
    return CandidateAction(
        candidate_id=tag,
        action_spec=spec,
        context_request=request,
        context_seed=seed,
        obligation_id=request.obligation_id,
        route_id=request.route_id,
        mechanism_family=spec.mechanism,
        action_kind=action_kind,
        slot_kind=slot_kind_for_action(action_kind),
        budget_class=budget_class,
        requested=requested if requested is not None else _vector(),
        priority_class=PriorityClass.NORMAL,
        dependency_validity=DependencyValidity.VALID,
        recurrence_recommendation=RecurrenceRecommendation.CONTINUE,
        recurrence_audit_required=False,
        created_sequence=1,
    )


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


@contextmanager
def _runtime(policy: BudgetPolicy | None = None, *, budget_limit: int = 10_000):
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        store = EventStore.open(root / "control.sqlite3")
        try:
            budget = BudgetLedger(store)
            budget.ensure_account(
                "proj",
                BudgetLimit(kind=BudgetLimitKind.LIMITED, value=budget_limit),
            )
            artifacts = ArtifactStore(root / "cas")
            coordinator = RuntimeCoordinator(
                policy if policy is not None else _policy(),
                store,
                budget,
                artifacts,
            )
            yield coordinator, store, budget, artifacts
        finally:
            store.close()


def _runtime_event_types(store: EventStore) -> list[str]:
    return [
        envelope.event_type
        for _seq, envelope in store.iter_project("proj")
        if envelope.event_type.startswith("runtime.")
    ]


def test_issue_requires_candidate_action_and_policy():
    with _runtime() as (coordinator, store, budget, artifacts):
        _raises(RuntimeError, RuntimeCoordinator, "not-policy", store, budget, artifacts)
        _raises(
            RuntimeError,
            coordinator.issue,
            "proj",
            object(),
            idempotency_key="x",
            timestamp_utc=_TS,
        )


def test_issue_transaction_rollback_on_event_failure():
    with _runtime() as (coordinator, store, budget, _artifacts):
        original = store.append

        def boom(envelope):
            if envelope.event_type == "runtime.lease_issued":
                raise EventStoreError("injected event failure")
            return original(envelope)

        store.append = boom  # type: ignore[method-assign]
        _raises(
            RuntimeError,
            coordinator.issue,
            "proj",
            _candidate("idem-1"),
            idempotency_key="idem-1",
            timestamp_utc=_TS,
        )
        store.append = original  # type: ignore[method-assign]
        assert _runtime_event_types(store) == []
        account = budget.get_account("proj")
        assert account is not None
        assert account.reserved_cost_microusd == 0
        snapshot = coordinator.replay("proj")
        assert snapshot.leases == ()
        assert snapshot.resources == ResourceSnapshot.empty()


def test_issue_transaction_rollback_on_budget_failure():
    with _runtime() as (coordinator, store, budget, _artifacts):
        _raises(
            RuntimeError,
            coordinator.issue,
            "proj",
            _candidate("too-big", requested=_vector(cost_microusd=50_000)),
            idempotency_key="too-big",
            timestamp_utc=_TS,
        )
        assert _runtime_event_types(store) == []
        account = budget.get_account("proj")
        assert account is not None
        assert account.reserved_cost_microusd == 0


def test_idempotent_and_conflicting_issue():
    with _runtime() as (coordinator, _store, budget, _artifacts):
        cand = _candidate("same")
        first = coordinator.issue(
            "proj", cand, idempotency_key="same", timestamp_utc=_TS
        )
        again = coordinator.issue(
            "proj",
            cand,
            idempotency_key="same",
            timestamp_utc="2026-01-01T00:00:01Z",
        )
        assert again.lease_id == first.lease_id
        assert again.slot_kind is SlotKind.REASONING
        assert again.budget_class is BudgetClass.NORMAL
        assert again.contract_id == cand.context_seed.contract_id
        assert again.context_seed_hash == cand.context_seed.seed_hash
        assert again.context_cache_key is None
        account = budget.get_account("proj")
        assert account is not None
        assert account.reserved_cost_microusd == 100
        _raises(
            LeaseConflict,
            coordinator.issue,
            "proj",
            _candidate("other"),
            idempotency_key="same",
            timestamp_utc="2026-01-01T00:00:02Z",
        )


def test_same_idempotency_rejects_changed_requested_resources():
    with _runtime() as (coordinator, _store, budget, _artifacts):
        first = coordinator.issue(
            "proj",
            _candidate("res", requested=_vector(cost_microusd=100)),
            idempotency_key="res",
            timestamp_utc=_TS,
        )
        _raises(
            LeaseConflict,
            coordinator.issue,
            "proj",
            _candidate("res", requested=_vector(cost_microusd=200)),
            idempotency_key="res",
            timestamp_utc="2026-01-01T00:00:01Z",
        )
        account = budget.get_account("proj")
        assert account is not None
        assert account.reserved_cost_microusd == 100
        snapshot = coordinator.replay("proj")
        assert len(snapshot.leases) == 1
        assert snapshot.leases[0].requested.cost_microusd == first.requested.cost_microusd


def test_same_idempotency_rejects_changed_cache_key():
    with _runtime() as (coordinator, _store, budget, _artifacts):
        key_a = _hex("cache-a")
        key_b = _hex("cache-b")
        cand = _candidate("cache")
        first = coordinator.issue(
            "proj",
            cand,
            idempotency_key="cache",
            timestamp_utc=_TS,
            context_cache_key=key_a,
        )
        assert first.context_cache_key == key_a
        _raises(
            LeaseConflict,
            coordinator.issue,
            "proj",
            cand,
            idempotency_key="cache",
            timestamp_utc="2026-01-01T00:00:01Z",
            context_cache_key=key_b,
        )
        replayed = coordinator.issue(
            "proj",
            cand,
            idempotency_key="cache",
            timestamp_utc="2026-01-01T00:00:02Z",
            context_cache_key=key_a,
        )
        assert replayed.lease_id == first.lease_id
        assert replayed.context_cache_key == key_a
        account = budget.get_account("proj")
        assert account is not None
        assert account.reserved_cost_microusd == 100


def test_inconsistent_snapshot_verifier_count_fails_closed():
    _raises(
        RuntimeError,
        RuntimeSnapshot,
        project_id="proj",
        leases=(),
        active_lease_ids=(),
        resources=ResourceSnapshot.empty(),
        verifier_slots_in_use=1,
    )
    ok = RuntimeSnapshot(
        project_id="proj",
        leases=(),
        active_lease_ids=(),
        resources=ResourceSnapshot.empty(),
        verifier_slots_in_use=0,
    )
    assert ok.verifier_slots_in_use == ok.resources.verifier_slots_in_use


def test_eight_dimensions_requested_and_consumed():
    with _runtime() as (coordinator, _store, _budget, _artifacts):
        lease = coordinator.issue(
            "proj", _candidate("dims"), idempotency_key="dims", timestamp_utc=_TS
        )
        requested = lease.requested_eight()
        assert set(requested) == {
            "cost_microusd",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "wall_ms",
            "tool_cpu_ms",
            "tool_gpu_ms",
        }
        assert requested["cost_microusd"] == 100
        assert requested["reasoning_tokens"] == 11
        assert requested["cached_tokens"] == 12
        usage = _usage(
            cost_microusd=_observed("microusd", 40),
            input_tokens=_observed("tokens", 3),
            output_tokens=_observed("tokens", 4),
            reasoning_tokens=_observed("tokens", 7),
            cached_tokens=_observed("tokens", 8),
            wall_ms=_observed("ms", 9),
            tool_cpu_ms=_observed("ms", 1),
            tool_gpu_ms=_observed("ms", 2),
        )
        done = coordinator.complete(
            "proj",
            lease.lease_id,
            usage=usage,
            timestamp_utc="2026-01-01T00:00:02Z",
        )
        consumed = done.consumed_eight()
        assert consumed["cost_microusd"] == 40
        assert consumed["input_tokens"] == 3
        assert consumed["output_tokens"] == 4
        assert consumed["reasoning_tokens"] == 7
        assert consumed["cached_tokens"] == 8
        assert consumed["wall_ms"] == 9
        assert consumed["tool_cpu_ms"] == 1
        assert consumed["tool_gpu_ms"] == 2
        assert done.resource_disposition is ResourceDisposition.SETTLED


def test_atomic_concurrent_slot_and_budget_recheck():
    policy = _policy(max_reasoning_slots=1, cost_microusd=DimensionLimit.limited(150))
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        path = root / "control.sqlite3"
        cas = root / "cas"
        with EventStore.open(path) as store:
            BudgetLedger(store).ensure_account(
                "proj",
                BudgetLimit(kind=BudgetLimitKind.LIMITED, value=10_000),
            )
        barrier = threading.Barrier(2)
        ok: list[str] = []
        blocked: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(tag: str) -> None:
            try:
                with EventStore.open(path) as store:
                    coordinator = RuntimeCoordinator(
                        policy, store, BudgetLedger(store), ArtifactStore(cas)
                    )
                    barrier.wait(timeout=5)
                    try:
                        lease = coordinator.issue(
                            "proj",
                            _candidate(tag, requested=_vector(cost_microusd=100)),
                            idempotency_key=tag,
                            timestamp_utc=_TS,
                        )
                        with lock:
                            ok.append(lease.lease_id)
                    except RuntimeError:
                        with lock:
                            blocked.append(tag)
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=("a",)),
            threading.Thread(target=worker, args=("b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
            assert not thread.is_alive()
        assert errors == []
        assert len(ok) == 1
        assert len(blocked) == 1
        with EventStore.open(path) as store:
            snapshot = RuntimeCoordinator(
                policy, store, BudgetLedger(store), ArtifactStore(cas)
            ).replay("proj")
            assert len(snapshot.active_lease_ids) == 1
            assert snapshot.resources.reasoning_slots_in_use == 1
            assert snapshot.resources.reserved.cost_microusd == 100


def test_unknown_infra_cancel_and_expire_settle():
    with _runtime() as (coordinator, _store, budget, artifacts):
        infra = coordinator.issue(
            "proj", _candidate("infra"), idempotency_key="infra", timestamp_utc=_TS
        )
        coordinator.mark_running(
            "proj", infra.lease_id, timestamp_utc="2026-01-01T00:00:01Z"
        )
        ref = artifacts.put_bytes(b"partial")
        failed = coordinator.fail_infrastructure(
            "proj",
            infra.lease_id,
            artifact_refs=(ref.content_hash,),
            timestamp_utc="2026-01-01T00:00:02Z",
        )
        assert failed.status is LeaseStatus.INFRA_FAILED
        assert failed.mathematical_status is MathematicalStatus.NO_CLAIM
        assert failed.resource_disposition is ResourceDisposition.SETTLED
        assert failed.consumed.cost_microusd == 100
        assert failed.consumed.reasoning_tokens == 11
        assert failed.consumed.cached_tokens == 12
        reservation = budget.get_reservation(failed.reservation_id)
        assert reservation is not None
        assert reservation.state is ReservationState.SETTLED

        pre = coordinator.issue(
            "proj", _candidate("pre"), idempotency_key="pre", timestamp_utc=_TS
        )
        cancelled = coordinator.cancel(
            "proj", pre.lease_id, timestamp_utc="2026-01-01T00:00:03Z"
        )
        assert cancelled.status is LeaseStatus.CANCELLED
        assert cancelled.resource_disposition is ResourceDisposition.RELEASED
        assert cancelled.consumed == ResourceVector.zero()
        assert cancelled.mathematical_status is MathematicalStatus.NO_CLAIM
        released = budget.get_reservation(cancelled.reservation_id)
        assert released is not None
        assert released.state is ReservationState.RELEASED

        run = coordinator.issue(
            "proj", _candidate("run"), idempotency_key="run", timestamp_utc=_TS
        )
        coordinator.mark_running(
            "proj", run.lease_id, timestamp_utc="2026-01-01T00:00:04Z"
        )
        stopped = coordinator.cancel(
            "proj", run.lease_id, timestamp_utc="2026-01-01T00:00:05Z"
        )
        assert stopped.resource_disposition is ResourceDisposition.SETTLED
        assert stopped.consumed.cost_microusd == 100
        assert stopped.mathematical_status is MathematicalStatus.NO_CLAIM
        settled = budget.get_reservation(stopped.reservation_id)
        assert settled is not None
        assert settled.state is ReservationState.SETTLED

        exp = coordinator.issue(
            "proj", _candidate("exp"), idempotency_key="exp", timestamp_utc=_TS
        )
        expired = coordinator.expire(
            "proj", exp.lease_id, timestamp_utc="2026-01-01T00:05:00Z"
        )
        assert expired.status is LeaseStatus.EXPIRED
        assert expired.mathematical_status is MathematicalStatus.NO_CLAIM
        assert expired.resource_disposition is ResourceDisposition.SETTLED
        assert expired.consumed.cost_microusd == 100
        expired_res = budget.get_reservation(expired.reservation_id)
        assert expired_res is not None
        assert expired_res.state is ReservationState.SETTLED


def test_completion_idempotency_conflict_and_progress():
    with _runtime() as (coordinator, _store, budget, artifacts):
        cand = _candidate("done")
        lease = coordinator.issue(
            "proj", cand, idempotency_key="done", timestamp_utc=_TS
        )
        coordinator.mark_running(
            "proj", lease.lease_id, timestamp_utc="2026-01-01T00:00:01Z"
        )
        ref = artifacts.put_bytes(b"proof-bytes")
        usage = _usage(cost_microusd=_observed("microusd", 40))
        view = ResearchView()
        progress = ProgressReceipt.build(
            action_id=lease.action_id,
            attempt_id=lease.attempt_id,
            contract_id=lease.contract_id,
            kind=ProgressKind.NO_VERIFIABLE_PROGRESS,
            claim="no new math",
            scope=_scope(),
            old_frontier_hash=_hex("old"),
            new_frontier_hash=_hex("old"),
            created_at="2026-01-01T00:00:02Z",
        )
        first = coordinator.complete(
            "proj",
            lease.lease_id,
            usage=usage,
            artifact_refs=(ref.content_hash,),
            mathematical_status=MathematicalStatus.CANDIDATE,
            progress=progress,
            view=view,
            timestamp_utc="2026-01-01T00:00:02Z",
        )
        assert first.status is LeaseStatus.COMPLETED
        assert first.consumed.cost_microusd == 40
        replayed = coordinator.complete(
            "proj",
            lease.lease_id,
            usage=usage,
            artifact_refs=(ref.content_hash,),
            mathematical_status=MathematicalStatus.CANDIDATE,
            timestamp_utc="2026-01-01T00:00:09Z",
        )
        assert replayed.ended_at == first.ended_at
        other = artifacts.put_bytes(b"other-proof")
        _raises(
            LeaseConflict,
            coordinator.complete,
            "proj",
            lease.lease_id,
            usage=usage,
            artifact_refs=(other.content_hash,),
            mathematical_status=MathematicalStatus.CANDIDATE,
            timestamp_utc="2026-01-01T00:00:10Z",
        )
        reservation = budget.get_reservation(first.reservation_id)
        assert reservation is not None
        assert reservation.state is ReservationState.SETTLED


def test_typed_progress_reject_and_accept():
    with _runtime() as (coordinator, _store, _budget, _artifacts):
        lease = coordinator.issue(
            "proj", _candidate("prog"), idempotency_key="prog", timestamp_utc=_TS
        )
        usage = _usage()
        _raises(
            RuntimeError,
            coordinator.complete,
            "proj",
            lease.lease_id,
            usage=usage,
            progress={"kind": "NO_VERIFIABLE_PROGRESS"},
            timestamp_utc="2026-01-01T00:00:02Z",
        )
        receipt = ProgressReceipt.build(
            action_id=lease.action_id,
            attempt_id=lease.attempt_id,
            contract_id=lease.contract_id,
            kind=ProgressKind.NO_VERIFIABLE_PROGRESS,
            claim="no new math",
            scope=_scope(),
            old_frontier_hash=_hex("old"),
            new_frontier_hash=_hex("old"),
            created_at="2026-01-01T00:00:02Z",
        )
        _raises(
            RuntimeError,
            coordinator.complete,
            "proj",
            lease.lease_id,
            usage=usage,
            progress=receipt,
            timestamp_utc="2026-01-01T00:00:02Z",
        )
        mismatch = ProgressReceipt.build(
            action_id=lease.action_id,
            attempt_id=_hex("other-attempt"),
            contract_id=lease.contract_id,
            kind=ProgressKind.NO_VERIFIABLE_PROGRESS,
            claim="no new math",
            scope=_scope(),
            old_frontier_hash=_hex("old"),
            new_frontier_hash=_hex("old"),
            created_at="2026-01-01T00:00:02Z",
        )
        _raises(
            RuntimeError,
            coordinator.complete,
            "proj",
            lease.lease_id,
            usage=usage,
            progress=mismatch,
            view=ResearchView(),
            timestamp_utc="2026-01-01T00:00:02Z",
        )
        done = coordinator.complete(
            "proj",
            lease.lease_id,
            usage=usage,
            progress=receipt,
            view=ResearchView(),
            timestamp_utc="2026-01-01T00:00:02Z",
        )
        assert done.status is LeaseStatus.COMPLETED
        assert coordinator.replay("proj").leases[0].status is LeaseStatus.COMPLETED


def test_full_snapshot_replay_and_verifier_slot():
    with _runtime() as (coordinator, store, _budget, artifacts):
        keep = coordinator.issue(
            "proj",
            _candidate("keep", action_kind=ActionKind.VERIFY),
            idempotency_key="keep",
            timestamp_utc=_TS,
        )
        assert keep.slot_kind is SlotKind.VERIFIER
        assert keep.action_kind is ActionKind.VERIFY
        coordinator.mark_running(
            "proj", keep.lease_id, timestamp_utc="2026-01-01T00:00:01Z"
        )
        done = coordinator.issue(
            "proj",
            _candidate("done", action_kind=ActionKind.COMPUTE),
            idempotency_key="done",
            timestamp_utc="2026-01-01T00:00:02Z",
        )
        ref = artifacts.put_bytes(b"done")
        coordinator.complete(
            "proj",
            done.lease_id,
            usage=_usage(),
            artifact_refs=(ref.content_hash,),
            timestamp_utc="2026-01-01T00:00:03Z",
        )
        snapshot = coordinator.replay("proj")
        assert isinstance(snapshot.resources, ResourceSnapshot)
        assert snapshot.active_lease_ids == (keep.lease_id,)
        assert snapshot.resources.reasoning_slots_in_use == 0
        assert snapshot.resources.compute_slots_in_use == 0
        assert snapshot.resources.verifier_slots_in_use == 1
        assert snapshot.verifier_slots_in_use == 1
        assert snapshot.resources.reserved.cost_microusd == 100
        assert snapshot.resources.consumed.cost_microusd == 100
        cache_key = build_context_cache_key(
            contract_id=keep.contract_id,
            obligation_id=keep.obligation_id or _hex("obligation"),
            action_id=keep.action_id,
            definitions=_scope(),
            tool_environment_hash=_hex("tool"),
            acceptance_policy_hash=_hex("accept"),
            reference_versions_hash=_hex("refs"),
            output_contract="return a KERNEL_CHECKED_PROOF",
            limits={
                "max_chars": {"kind": "UNLIMITED", "value": None},
                "max_bytes": {"kind": "UNLIMITED", "value": None},
            },
            concrete_gap="close the even-integer case",
            expand_evidence_content=False,
        )
        keyed = coordinator.issue(
            "proj",
            _candidate("keyed", obligation_id=_hex("obligation-2")),
            idempotency_key="keyed",
            timestamp_utc="2026-01-01T00:00:04Z",
            context_cache_key=cache_key.key_id,
        )
        assert keyed.context_cache_key == cache_key.key_id
        recovered = RuntimeCoordinator(
            _policy(), store, BudgetLedger(store), artifacts
        ).replay("proj")
        by_id = {item.lease_id: item for item in recovered.leases}
        assert by_id[keep.lease_id].status is LeaseStatus.RUNNING
        assert by_id[done.lease_id].resource_disposition is ResourceDisposition.SETTLED
        assert recovered.verifier_slots_in_use == 1
        assert recovered.resources.verifier_slots_in_use == 1
        assert recovered.resources.reasoning_slots_in_use == 1


def test_after_seq_replay_fails_closed():
    with _runtime() as (coordinator, _store, _budget, _artifacts):
        coordinator.issue(
            "proj", _candidate("seq"), idempotency_key="seq", timestamp_utc=_TS
        )
        _raises(RuntimeError, coordinator.replay, "proj", 1)
        snapshot = coordinator.replay("proj", 0)
        assert len(snapshot.leases) == 1


def test_artifact_recovery_and_missing_artifact():
    with _runtime() as (coordinator, store, budget, artifacts):
        lease = coordinator.issue(
            "proj", _candidate("art"), idempotency_key="art", timestamp_utc=_TS
        )
        _raises(
            RuntimeError,
            coordinator.complete,
            "proj",
            lease.lease_id,
            usage=_usage(),
            artifact_refs=(_hex("missing-blob"),),
            timestamp_utc="2026-01-01T00:00:02Z",
        )
        reservation = budget.get_reservation(lease.reservation_id)
        assert reservation is not None
        assert reservation.state is ReservationState.RESERVED
        ref = artifacts.put_bytes(b"recovered-receipt")
        recovered = coordinator.complete(
            "proj",
            lease.lease_id,
            usage=_usage(),
            artifact_refs=(ref.content_hash,),
            timestamp_utc="2026-01-01T00:00:05Z",
        )
        assert recovered.status is LeaseStatus.COMPLETED
        assert "runtime.lease_completed" in _runtime_event_types(store)


def test_replay_detects_malformed_history():
    with _runtime() as (coordinator, store, _budget, _artifacts):
        store.append(
            EventEnvelope.build(
                project_id="proj",
                actor="runtime",
                event_type="runtime.not_a_real_event",
                idempotency_key="bad",
                payload={"lease_id": "nope"},
                timestamp_utc=_TS,
            )
        )
        _raises(RuntimeError, coordinator.replay, "proj")


def test_no_provider_or_truth_store_side_effects():
    forbidden_modules = {
        "euler.core.factgraph",
        "euler.core.global_memory",
        "euler.gateway",
        "euler.gateway.server",
        "euler.verify",
        "euler.verify.service",
        "euler.execution.launch_receipt",
        "euler.execution.memory_contract_audit",
        "euler.execution.sol_guidance",
    }
    forbidden_names = {
        "FactGraph",
        "fact_submit",
        "GlobalMemory",
        "subprocess",
        "openai",
        "httpx",
        "requests",
    }
    path = _ROOT / "runtime.py"
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
    assert imported.isdisjoint(forbidden_modules)
    assert named.isdisjoint(forbidden_names)
    assert "FactGraph.add" not in source
    assert "fact_submit" not in source
    assert "Popen" not in source
    assert "urlopen" not in source


def main() -> None:
    tests = [
        test_issue_requires_candidate_action_and_policy,
        test_issue_transaction_rollback_on_event_failure,
        test_issue_transaction_rollback_on_budget_failure,
        test_idempotent_and_conflicting_issue,
        test_same_idempotency_rejects_changed_requested_resources,
        test_same_idempotency_rejects_changed_cache_key,
        test_inconsistent_snapshot_verifier_count_fails_closed,
        test_eight_dimensions_requested_and_consumed,
        test_atomic_concurrent_slot_and_budget_recheck,
        test_unknown_infra_cancel_and_expire_settle,
        test_completion_idempotency_conflict_and_progress,
        test_typed_progress_reject_and_accept,
        test_full_snapshot_replay_and_verifier_slot,
        test_after_seq_replay_fails_closed,
        test_artifact_recovery_and_missing_artifact,
        test_replay_detects_malformed_history,
        test_no_provider_or_truth_store_side_effects,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL RUNTIME LEASE TESTS PASSED")


if __name__ == "__main__":
    main()
