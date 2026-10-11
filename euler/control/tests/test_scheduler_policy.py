"""Offline tests for Phase 5 resource policy and transparent scheduler.

Covers V1 Phase 5 required tests 1–10 plus zero mutation of view/index/history
and no provider or truth-store writes. Runtime lease tests are out of scope.

Runs standalone (``python -m euler.control.tests.test_scheduler_policy``).
"""

from __future__ import annotations

import ast
from pathlib import Path

from euler.control.cache import (
    ContextCacheKey,
    build_context_cache_key,
    compare_context_keys,
)
from euler.control.context import ContextLimits
from euler.control.contracts import (
    CheckerResult,
    EvidenceArtifact,
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
    QuantifierBinding,
    QuantifierKind,
    TargetKind,
    ValidityStatus,
)
from euler.control.dedupe import ActionAttemptIndex, DedupeDecision
from euler.control.ids import canonical_json, sha256_hex
from euler.control.obligations import Obligation, ObligationStatus, Route
from euler.control.policy import (
    BudgetClass,
    BudgetPolicy,
    DimensionLimit,
    OnDeadlinePolicy,
    PolicyError,
    RESOURCE_DIMENSIONS,
    ResourceLimitKind,
    ResourceSnapshot,
    ResourceUsage,
    ResourceVector,
    SlotKind,
)
from euler.control.recurrence import RecurrenceHistory, RecurrenceRecommendation
from euler.control.scheduler import (
    ActionKind,
    ActiveWork,
    CandidateAction,
    CandidateDecision,
    ContextSeed,
    DependencyValidity,
    DispatchPlan,
    PriorityClass,
    ScheduleDisposition,
    ScheduleReason,
    SchedulerError,
    TransparentScheduler,
    build_action_context_pair,
    slot_kind_for_action,
)
from euler.control.schema import (
    EventEnvelope,
    InfrastructureStatus,
    ObservationStatus,
)
from euler.control.views import ResearchView, RouteReadiness

_ROOT = Path(__file__).resolve().parents[1]
_TS = "2026-01-01T00:00:00Z"
_NOW = "2026-01-01T00:00:00Z"
_AFTER_DEADLINE = "2026-01-02T00:00:00Z"


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__} from {getattr(fn, '__name__', fn)}")


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
        "cost_microusd": 1,
        "input_tokens": 1,
        "output_tokens": 1,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "wall_ms": 1,
        "tool_cpu_ms": 0,
        "tool_gpu_ms": 0,
    }
    payload.update(overrides)
    return ResourceVector(**payload)


def _unlimited_policy(**overrides: object) -> BudgetPolicy:
    unbound = DimensionLimit.unlimited()
    kwargs: dict[str, object] = {
        "cost_microusd": unbound,
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
    created_sequence: int = 1,
    action_kind: ActionKind = ActionKind.PROVE,
    budget_class: BudgetClass = BudgetClass.NORMAL,
    priority_class: PriorityClass = PriorityClass.NORMAL,
    requested: ResourceVector | None = None,
    dependency_validity: DependencyValidity = DependencyValidity.VALID,
    recurrence_recommendation: RecurrenceRecommendation = RecurrenceRecommendation.CONTINUE,
    recurrence_audit_required: bool = False,
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
        priority_class=priority_class,
        dependency_validity=dependency_validity,
        recurrence_recommendation=recurrence_recommendation,
        recurrence_audit_required=recurrence_audit_required,
        created_sequence=created_sequence,
    )


def _cache(spec, request) -> ContextCacheKey:
    facts = tuple(
        {"fact_id": fact_id, "content_hash": _hex("fact", fact_id)}
        for fact_id in request.required_fact_ids
    )
    return build_context_cache_key(
        contract_id=request.contract_id,
        obligation_id=request.obligation_id,
        action_id=request.action_id,
        definitions=_scope(),
        tool_environment_hash=request.tool_environment_hash,
        acceptance_policy_hash=request.acceptance_policy_hash,
        reference_versions_hash=request.reference_versions_hash,
        output_contract=request.output_contract,
        limits={
            "max_chars": request.limits.max_chars,
            "max_bytes": request.limits.max_bytes,
            "max_failures": request.limits.max_optional_failures,
            "max_relations": request.limits.max_optional_relations,
        },
        concrete_gap=request.concrete_gap,
        expand_evidence_content=request.expand_evidence_content,
        packet_policy={"expand_evidence_content": request.expand_evidence_content},
        facts=facts,
        failure_ids=request.relevant_failure_ids,
        relation_ids=request.relevant_relation_ids,
        route_id=request.route_id,
    )


def _plan(candidates, *, policy=None, snapshot=None, now_utc=_NOW, **kwargs) -> DispatchPlan:
    scheduler = TransparentScheduler()
    return scheduler.select(
        candidates,
        policy=policy if policy is not None else _unlimited_policy(),
        snapshot=snapshot if snapshot is not None else ResourceSnapshot.empty(),
        now_utc=now_utc,
        **kwargs,
    )


def _reasons(plan: DispatchPlan) -> dict[str, tuple[ScheduleDisposition, ScheduleReason]]:
    return {
        item.candidate_id: (item.disposition, item.reason) for item in plan.decisions
    }


# --------------------------------------------------------------------------- #
# 1. seed / action / request construction                                     #
# --------------------------------------------------------------------------- #


def test_seed_action_request_construction_has_no_cache_feedback_cycle():
    spec, request, seed = _pair()
    assert spec.context_hash == seed.seed_hash
    assert request.action_id == spec.action_id
    assert "action_id" not in seed.to_dict()
    cache = _cache(spec, request)
    assert isinstance(cache, ContextCacheKey)
    assert cache.key_id != seed.seed_hash
    assert spec.context_hash != cache.key_id

    semantic = {
        "contract_id": _hex("contract-2"),
        "obligation_id": _hex("obligation-2"),
        "route_id": _hex("route-2"),
        "concrete_gap": "a different concrete gap",
        "output_contract": "return a counterexample",
        "tool_environment_hash": _hex("tool-2"),
        "acceptance_policy_hash": _hex("accept-2"),
        "reference_versions_hash": _hex("refs-2"),
        "expand_evidence_content": True,
        "expand_proof_content": True,
        "required_fact_ids": ("lemma-a",),
        "required_evidence_ids": (_hex("ev-1"),),
        "relevant_failure_ids": (_hex("fail-1"),),
        "relevant_relation_ids": (_hex("rel-1"),),
        "limits": ContextLimits.build(max_chars=128),
    }
    for key, value in semantic.items():
        other_spec, other_request, other_seed = _pair(**{key: value})
        assert other_seed.seed_hash != seed.seed_hash, key
        assert other_spec.action_id != spec.action_id, key
        assert other_request.action_id == other_spec.action_id, key
        assert other_spec.context_hash == other_seed.seed_hash, key
        other_cache = _cache(other_spec, other_request)
        assert other_cache.key_id != cache.key_id, key
        reasons = compare_context_keys(cache, other_cache)
        assert reasons, key

    dep_spec, dep_request, dep_seed = _pair(dependency_hash=_hex("dep-changed"))
    assert dep_seed.seed_hash == seed.seed_hash
    assert dep_spec.action_id != spec.action_id
    assert dep_request.action_id == dep_spec.action_id
    dep_cache = _cache(dep_spec, dep_request)
    assert dep_cache.key_id != cache.key_id
    assert dep_spec.context_hash == seed.seed_hash
    assert dep_spec.context_hash != dep_cache.key_id


# --------------------------------------------------------------------------- #
# 2–4. resource dimensions, unknown settlement, class reserves                #
# --------------------------------------------------------------------------- #


def test_eight_resource_dimensions_and_explicit_unlimited():
    limited = DimensionLimit.limited(10)
    unbound = DimensionLimit.unlimited()
    assert limited.kind is ResourceLimitKind.LIMITED
    assert unbound.kind is ResourceLimitKind.UNLIMITED
    assert unbound.value is None
    assert RESOURCE_DIMENSIONS == (
        "cost_microusd",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "wall_ms",
        "tool_cpu_ms",
        "tool_gpu_ms",
    )
    _raises(PolicyError, DimensionLimit, kind=ResourceLimitKind.UNLIMITED, value=0)
    _raises(PolicyError, DimensionLimit, kind=ResourceLimitKind.LIMITED, value=None)
    _raises(PolicyError, ResourceVector.zero().add, True)  # type: ignore[arg-type]
    for name in RESOURCE_DIMENSIONS:
        _raises(PolicyError, ResourceVector, **{dim: (1.5 if dim == name else 0) for dim in RESOURCE_DIMENSIONS})
        _raises(PolicyError, ResourceVector, **{dim: (True if dim == name else 0) for dim in RESOURCE_DIMENSIONS})
    policy = BudgetPolicy(
        cost_microusd=DimensionLimit.limited(100),
        input_tokens=DimensionLimit.unlimited(),
        output_tokens=DimensionLimit.limited(8),
        reasoning_tokens=DimensionLimit.limited(16),
        cached_tokens=DimensionLimit.unlimited(),
        wall_ms=DimensionLimit.unlimited(),
        tool_cpu_ms=DimensionLimit.limited(0),
        tool_gpu_ms=DimensionLimit.unlimited(),
        max_reasoning_slots=2,
        max_compute_slots=1,
        max_retrieval_slots=1,
        max_verifier_slots=1,
    )
    assert policy.dimension("input_tokens").kind is ResourceLimitKind.UNLIMITED
    assert policy.dimension("reasoning_tokens").kind is ResourceLimitKind.LIMITED
    assert policy.dimension("cached_tokens").kind is ResourceLimitKind.UNLIMITED
    huge = _vector(input_tokens=10**12, cost_microusd=1, output_tokens=1, cached_tokens=10**9)
    snap = ResourceSnapshot.empty()
    assert snap.can_reserve(
        huge, budget_class=BudgetClass.NORMAL, slot_kind=SlotKind.REASONING, policy=policy
    )
    too_costly = _vector(cost_microusd=101)
    assert not snap.can_reserve(
        too_costly,
        budget_class=BudgetClass.NORMAL,
        slot_kind=SlotKind.REASONING,
        policy=policy,
    )
    too_reason = _vector(reasoning_tokens=17)
    assert not snap.can_reserve(
        too_reason,
        budget_class=BudgetClass.NORMAL,
        slot_kind=SlotKind.REASONING,
        policy=policy,
    )
    _raises(PolicyError, BudgetPolicy, **{**policy.to_dict(), "final_audit_reserve_bp": 6000, "exploration_reserve_bp": 5000})
    _raises(PolicyError, BudgetPolicy, **{**policy.to_dict(), "final_audit_reserve_bp": 0.5})
    _raises(PolicyError, BudgetPolicy, **{**policy.to_dict(), "max_reasoning_slots": True})
    _raises(PolicyError, BudgetPolicy, **{**policy.to_dict(), "deadline_utc": "2026-01-01T00:00:00+01:00"})
    restored = BudgetPolicy.from_dict(policy.to_dict())
    assert restored == policy
    missing_vector = ResourceVector.zero().to_dict()
    del missing_vector["reasoning_tokens"]
    _raises(PolicyError, ResourceVector.from_dict, missing_vector)
    missing_cached = ResourceVector.zero().to_dict()
    del missing_cached["cached_tokens"]
    _raises(PolicyError, ResourceVector.from_dict, missing_cached)
    missing_usage = ResourceUsage.unknown().to_dict()
    del missing_usage["reasoning_tokens"]
    _raises(PolicyError, ResourceUsage.from_dict, missing_usage)
    missing_policy = policy.to_dict()
    del missing_policy["cached_tokens"]
    _raises(PolicyError, BudgetPolicy.from_dict, missing_policy)
    missing_snap = ResourceSnapshot.empty().to_dict()
    del missing_snap["verifier_slots_in_use"]
    _raises(PolicyError, ResourceSnapshot.from_dict, missing_snap)
    original = ResourceSnapshot.empty()
    reserved = original.reserve(
        _vector(cost_microusd=4, reasoning_tokens=3, cached_tokens=2),
        budget_class=BudgetClass.NORMAL,
        slot_kind=SlotKind.REASONING,
        policy=policy,
    )
    assert original == ResourceSnapshot.empty()
    assert reserved.reserved.cost_microusd == 4
    assert reserved.reserved.reasoning_tokens == 3
    assert reserved.reserved.cached_tokens == 2
    assert reserved.reasoning_slots_in_use == 1
    assert reserved.verifier_slots_in_use == 0
    assert set(reserved.reserved.to_dict()) == set(RESOURCE_DIMENSIONS)


def test_unknown_settlement_is_conservative():
    policy = _unlimited_policy()
    requested = _vector(
        cost_microusd=40,
        input_tokens=7,
        output_tokens=9,
        reasoning_tokens=13,
        cached_tokens=5,
        wall_ms=11,
    )
    original = ResourceSnapshot.empty()
    reserved = original.reserve(
        requested,
        budget_class=BudgetClass.EXPLORATION,
        slot_kind=SlotKind.COMPUTE,
        policy=policy,
    )
    assert original == ResourceSnapshot.empty()
    unknown = reserved.settle(
        requested,
        ResourceUsage.unknown(),
        budget_class=BudgetClass.EXPLORATION,
        slot_kind=SlotKind.COMPUTE,
    )
    assert reserved.reserved == requested
    assert unknown.reserved == ResourceVector.zero()
    assert unknown.consumed == requested
    assert unknown.consumed_exploration == requested
    assert unknown.compute_slots_in_use == 0
    observed = reserved.settle(
        requested,
        ResourceUsage.observed(
            cost_microusd=3,
            input_tokens=1,
            output_tokens=0,
            reasoning_tokens=4,
            cached_tokens=1,
            wall_ms=2,
            tool_cpu_ms=0,
            tool_gpu_ms=0,
        ),
        budget_class=BudgetClass.EXPLORATION,
        slot_kind=SlotKind.COMPUTE,
    )
    assert observed.consumed.cost_microusd == 3
    assert observed.consumed.input_tokens == 1
    assert observed.consumed.reasoning_tokens == 4
    assert observed.consumed.cached_tokens == 1
    assert reserved.consumed == ResourceVector.zero()
    unknown_usage = ResourceUsage.unknown()
    for name in RESOURCE_DIMENSIONS:
        qty = getattr(unknown_usage, name)
        assert qty.status is ObservationStatus.UNKNOWN
        assert qty.value is None
    restored_unknown = ResourceUsage.from_dict(unknown_usage.to_dict())
    assert restored_unknown.reasoning_tokens.status is ObservationStatus.UNKNOWN
    assert restored_unknown.cached_tokens.status is ObservationStatus.UNKNOWN
    assert unknown.consumed.reasoning_tokens == requested.reasoning_tokens
    assert unknown.consumed.cached_tokens == requested.cached_tokens


def test_class_reserves_prevent_normal_from_consuming_exclusive_quota():
    policy = _unlimited_policy(
        cost_microusd=DimensionLimit.limited(10000),
        final_audit_reserve_bp=2000,
        exploration_reserve_bp=2000,
        max_reasoning_slots=4,
    )
    snap = ResourceSnapshot.empty()
    over = _vector(cost_microusd=6001)
    assert snap.reservation_block(
        over,
        budget_class=BudgetClass.NORMAL,
        slot_kind=SlotKind.REASONING,
        policy=policy,
    ) == "reserve"
    assert snap.can_reserve(
        _vector(cost_microusd=6000),
        budget_class=BudgetClass.NORMAL,
        slot_kind=SlotKind.REASONING,
        policy=policy,
    )
    assert snap.can_reserve(
        _vector(cost_microusd=2000),
        budget_class=BudgetClass.FINAL_AUDIT,
        slot_kind=SlotKind.REASONING,
        policy=policy,
    )
    audit = _candidate(
        "audit-reserve",
        created_sequence=1,
        budget_class=BudgetClass.FINAL_AUDIT,
        priority_class=PriorityClass.LOAD_BEARING,
        requested=_vector(cost_microusd=2000),
        obligation_id=_hex("ob-audit"),
    )
    normal_ok = _candidate(
        "normal-ok",
        created_sequence=2,
        priority_class=PriorityClass.LOAD_BEARING,
        requested=_vector(cost_microusd=6000),
        obligation_id=_hex("ob-normal"),
    )
    explore = _candidate(
        "explore-ok",
        created_sequence=3,
        budget_class=BudgetClass.EXPLORATION,
        priority_class=PriorityClass.EXPLORATION,
        requested=_vector(cost_microusd=2000),
        obligation_id=_hex("ob-exp"),
    )
    normal_over = _candidate(
        "normal-over",
        created_sequence=4,
        requested=_vector(cost_microusd=1),
        obligation_id=_hex("ob-extra"),
    )
    plan = _plan(
        [normal_over, explore, normal_ok, audit],
        policy=policy,
    )
    reasons = _reasons(plan)
    assert reasons["audit-reserve"] == (
        ScheduleDisposition.SELECTED,
        ScheduleReason.SELECTED,
    )
    assert reasons["normal-ok"] == (
        ScheduleDisposition.SELECTED,
        ScheduleReason.SELECTED,
    )
    assert reasons["explore-ok"] == (
        ScheduleDisposition.SELECTED,
        ScheduleReason.SELECTED,
    )
    assert reasons["normal-over"] == (
        ScheduleDisposition.DEFERRED,
        ScheduleReason.CLASS_RESERVE,
    )
    assert set(plan.selected_candidate_ids) == {
        "audit-reserve",
        "normal-ok",
        "explore-ok",
    }


# --------------------------------------------------------------------------- #
# 5–8. deadline, dedupe, mechanism diversity, slots                           #
# --------------------------------------------------------------------------- #


def test_deadline_rejects_normal_work_and_preserves_final_audit():
    policy = _unlimited_policy(
        deadline_utc="2026-01-01T12:00:00Z",
        on_deadline=OnDeadlinePolicy.REJECT_NORMAL,
        max_reasoning_slots=3,
        max_verifier_slots=1,
        final_audit_reserve_bp=1000,
    )
    normal = _candidate(
        "dead-normal",
        created_sequence=2,
        obligation_id=_hex("ob-n"),
    )
    load = _candidate(
        "dead-load",
        created_sequence=3,
        priority_class=PriorityClass.LOAD_BEARING,
        obligation_id=_hex("ob-l"),
    )
    explore = _candidate(
        "dead-exp",
        created_sequence=4,
        budget_class=BudgetClass.EXPLORATION,
        priority_class=PriorityClass.EXPLORATION,
        obligation_id=_hex("ob-e"),
    )
    audit = _candidate(
        "dead-audit",
        created_sequence=1,
        action_kind=ActionKind.VERIFY,
        budget_class=BudgetClass.FINAL_AUDIT,
        priority_class=PriorityClass.FINAL_ACCEPTANCE,
        obligation_id=_hex("ob-a"),
    )
    plan = _plan(
        [explore, normal, audit, load],
        policy=policy,
        now_utc=_AFTER_DEADLINE,
    )
    reasons = _reasons(plan)
    assert reasons["dead-audit"][0] is ScheduleDisposition.SELECTED
    assert reasons["dead-normal"] == (
        ScheduleDisposition.DEFERRED,
        ScheduleReason.DEADLINE,
    )
    assert reasons["dead-load"] == (
        ScheduleDisposition.DEFERRED,
        ScheduleReason.DEADLINE,
    )
    assert reasons["dead-exp"] == (
        ScheduleDisposition.DEFERRED,
        ScheduleReason.DEADLINE,
    )
    before = _plan([normal, audit], policy=policy, now_utc="2026-01-01T11:59:59Z")
    assert set(before.selected_candidate_ids) == {"dead-normal", "dead-audit"}


def test_exact_dedupe_wait_recover_retry_behavior():
    wait_c = _candidate("dup-wait", task_mode="waitmode", obligation_id=_hex("ob-w"))
    recover_c = _candidate(
        "dup-recover", task_mode="recovermode", obligation_id=_hex("ob-r")
    )
    retry_c = _candidate("dup-retry", task_mode="retrymode", obligation_id=_hex("ob-t"))
    index = ActionAttemptIndex()
    wait_rec = index.start(wait_c.action_spec, _hex("att-wait"))
    rec_rec = index.start(recover_c.action_spec, _hex("att-rec"))
    index.finish(
        rec_rec.attempt_id,
        infrastructure_status=InfrastructureStatus.SUCCEEDED,
        artifact_refs=(_hex("art"),),
        result="ok",
    )
    retry_rec = index.start(retry_c.action_spec, _hex("att-retry"))
    index.finish(
        retry_rec.attempt_id, infrastructure_status=InfrastructureStatus.FAILED
    )
    assert index.decide(wait_c.action_spec) is DedupeDecision.WAIT_FOR_EXISTING
    assert index.decide(recover_c.action_spec) is DedupeDecision.RECOVER_OR_REUSE
    assert index.decide(retry_c.action_spec) is DedupeDecision.RETRY_TRANSPORT
    before_attempts = dict(index._by_attempt)
    before_keys = {key: list(value) for key, value in index._by_key.items()}
    plan = _plan(
        [retry_c, recover_c, wait_c],
        policy=_unlimited_policy(max_reasoning_slots=3),
        dedupe_index=index,
    )
    reasons = _reasons(plan)
    assert reasons["dup-wait"] == (
        ScheduleDisposition.DEFERRED,
        ScheduleReason.DEDUPE_WAIT,
    )
    assert reasons["dup-recover"] == (
        ScheduleDisposition.DEFERRED,
        ScheduleReason.DEDUPE_RECOVER,
    )
    assert reasons["dup-retry"] == (
        ScheduleDisposition.SELECTED,
        ScheduleReason.SELECTED,
    )
    assert index.decide(wait_c.action_spec) is DedupeDecision.WAIT_FOR_EXISTING
    assert dict(index._by_attempt) == before_attempts
    assert {key: list(value) for key, value in index._by_key.items()} == before_keys
    assert wait_rec.infrastructure_status is InfrastructureStatus.PENDING


def test_different_mechanisms_may_parallel_identical_may_not():
    obligation = _hex("shared-ob")
    direct = _candidate(
        "mech-direct",
        created_sequence=1,
        mechanism="direct",
        obligation_id=obligation,
        priority_class=PriorityClass.LOAD_BEARING,
    )
    literature = _candidate(
        "mech-lit",
        created_sequence=2,
        action_kind=ActionKind.LITERATURE,
        mechanism="literature",
        obligation_id=obligation,
        priority_class=PriorityClass.DISCRIMINATING_CHEAP,
    )
    direct_again = _candidate(
        "mech-direct-2",
        created_sequence=3,
        mechanism="direct",
        obligation_id=obligation,
        dependency_hash=_hex("dep-other"),
    )
    other_ob = _candidate(
        "mech-other-ob",
        created_sequence=4,
        mechanism="direct",
        obligation_id=_hex("other-ob"),
    )
    policy = _unlimited_policy(max_reasoning_slots=3, max_retrieval_slots=1)
    plan = _plan([direct_again, other_ob, literature, direct], policy=policy)
    reasons = _reasons(plan)
    assert reasons["mech-direct"][0] is ScheduleDisposition.SELECTED
    assert reasons["mech-lit"][0] is ScheduleDisposition.SELECTED
    assert reasons["mech-direct-2"] == (
        ScheduleDisposition.DEFERRED,
        ScheduleReason.MECHANISM_CONFLICT,
    )
    assert reasons["mech-other-ob"][0] is ScheduleDisposition.SELECTED
    running = ActiveWork(
        obligation_id=obligation,
        mechanism_family="direct",
        action_id=direct.action_spec.action_id,
        slot_kind=SlotKind.REASONING,
    )
    occupied = ResourceSnapshot.empty().reserve(
        _vector(),
        budget_class=BudgetClass.NORMAL,
        slot_kind=SlotKind.REASONING,
        policy=policy,
    )
    blocked = _plan(
        [literature, direct_again],
        policy=policy,
        snapshot=occupied,
        active=(running,),
    )
    blocked_reasons = _reasons(blocked)
    assert blocked_reasons["mech-direct-2"][1] is ScheduleReason.MECHANISM_CONFLICT
    assert blocked_reasons["mech-lit"][0] is ScheduleDisposition.SELECTED


def test_two_reasoning_and_configured_compute_retrieval_slots():
    policy = _unlimited_policy(
        max_reasoning_slots=2,
        max_compute_slots=1,
        max_retrieval_slots=1,
    )
    r1 = _candidate("slot-r1", created_sequence=1, obligation_id=_hex("r1"))
    r2 = _candidate("slot-r2", created_sequence=2, obligation_id=_hex("r2"))
    r3 = _candidate("slot-r3", created_sequence=3, obligation_id=_hex("r3"))
    c1 = _candidate(
        "slot-c1",
        created_sequence=4,
        action_kind=ActionKind.COMPUTE,
        obligation_id=_hex("c1"),
    )
    c2 = _candidate(
        "slot-c2",
        created_sequence=5,
        action_kind=ActionKind.COMPUTE,
        obligation_id=_hex("c2"),
    )
    l1 = _candidate(
        "slot-l1",
        created_sequence=6,
        action_kind=ActionKind.LITERATURE,
        obligation_id=_hex("l1"),
    )
    l2 = _candidate(
        "slot-l2",
        created_sequence=7,
        action_kind=ActionKind.LITERATURE,
        obligation_id=_hex("l2"),
    )
    plan = _plan([l2, c2, r3, l1, c1, r2, r1], policy=policy)
    reasons = _reasons(plan)
    assert reasons["slot-r1"][0] is ScheduleDisposition.SELECTED
    assert reasons["slot-r2"][0] is ScheduleDisposition.SELECTED
    assert reasons["slot-r3"] == (
        ScheduleDisposition.DEFERRED,
        ScheduleReason.SLOT_EXHAUSTED,
    )
    assert reasons["slot-c1"][0] is ScheduleDisposition.SELECTED
    assert reasons["slot-c2"] == (
        ScheduleDisposition.DEFERRED,
        ScheduleReason.SLOT_EXHAUSTED,
    )
    assert reasons["slot-l1"][0] is ScheduleDisposition.SELECTED
    assert reasons["slot-l2"] == (
        ScheduleDisposition.DEFERRED,
        ScheduleReason.SLOT_EXHAUSTED,
    )
    idle = _plan([], policy=policy)
    assert idle.idle is True
    assert idle.selected_candidate_ids == ()
    one = _plan([r1], policy=policy)
    assert one.selected_candidate_ids == ("slot-r1",)
    assert one.idle is False


# --------------------------------------------------------------------------- #
# 9–10. audit-required recurrence, determinism                                #
# --------------------------------------------------------------------------- #


def test_audit_required_recurrence_never_auto_parks():
    parked = _candidate(
        "park-prove",
        created_sequence=2,
        recurrence_recommendation=RecurrenceRecommendation.PARK_ROUTE_CANDIDATE,
        recurrence_audit_required=True,
        obligation_id=_hex("ob-park"),
    )
    bounded = _candidate(
        "audit-prove",
        created_sequence=3,
        recurrence_recommendation=RecurrenceRecommendation.BOUNDED_AUDIT,
        recurrence_audit_required=True,
        obligation_id=_hex("ob-aud"),
    )
    restructure = _candidate(
        "restructure-prove",
        created_sequence=4,
        recurrence_recommendation=RecurrenceRecommendation.RESTRUCTURE,
        recurrence_audit_required=True,
        obligation_id=_hex("ob-re"),
    )
    explicit = _candidate(
        "explicit-audit",
        created_sequence=1,
        action_kind=ActionKind.AUDIT,
        budget_class=BudgetClass.FINAL_AUDIT,
        priority_class=PriorityClass.LOAD_BEARING,
        recurrence_recommendation=RecurrenceRecommendation.PARK_ROUTE_CANDIDATE,
        recurrence_audit_required=True,
        obligation_id=_hex("ob-park"),
        task_mode="audit",
    )
    plan = _plan(
        [parked, restructure, explicit, bounded],
        policy=_unlimited_policy(max_reasoning_slots=3),
    )
    reasons = _reasons(plan)
    assert reasons["park-prove"] == (
        ScheduleDisposition.DEFERRED,
        ScheduleReason.RECURRENCE_AUDIT,
    )
    assert reasons["audit-prove"] == (
        ScheduleDisposition.DEFERRED,
        ScheduleReason.RECURRENCE_AUDIT,
    )
    assert reasons["restructure-prove"] == (
        ScheduleDisposition.DEFERRED,
        ScheduleReason.RECURRENCE_AUDIT,
    )
    assert reasons["explicit-audit"] == (
        ScheduleDisposition.SELECTED,
        ScheduleReason.SELECTED,
    )
    for item in plan.decisions:
        assert "park" not in item.reason.value.lower()
        assert item.disposition is not ScheduleDisposition.SELECTED or item.candidate_id == "explicit-audit"
    assert "PARK" not in {reason.value for reason in ScheduleReason}
    _raises(
        SchedulerError,
        CandidateAction,
        candidate_id="bad-audit-flag",
        action_spec=parked.action_spec,
        context_request=parked.context_request,
        context_seed=parked.context_seed,
        obligation_id=parked.obligation_id,
        mechanism_family=parked.mechanism_family,
        action_kind=ActionKind.PROVE,
        slot_kind=SlotKind.REASONING,
        budget_class=BudgetClass.NORMAL,
        requested=_vector(),
        priority_class=PriorityClass.NORMAL,
        dependency_validity=DependencyValidity.VALID,
        recurrence_recommendation=RecurrenceRecommendation.PARK_ROUTE_CANDIDATE,
        recurrence_audit_required=False,
        created_sequence=9,
    )


def test_deterministic_decisions_independent_of_input_ordering():
    stale = _candidate(
        "ord-stale",
        created_sequence=5,
        dependency_validity=DependencyValidity.STALE,
        obligation_id=_hex("ob-s"),
    )
    missing = _candidate(
        "ord-missing",
        created_sequence=6,
        dependency_validity=DependencyValidity.MISSING,
        obligation_id=_hex("ob-m"),
    )
    first = _candidate(
        "ord-first",
        created_sequence=1,
        priority_class=PriorityClass.LOAD_BEARING,
        obligation_id=_hex("ob-1"),
    )
    second = _candidate(
        "ord-second",
        created_sequence=2,
        priority_class=PriorityClass.LOAD_BEARING,
        obligation_id=_hex("ob-2"),
    )
    cheap = _candidate(
        "ord-cheap",
        created_sequence=3,
        action_kind=ActionKind.LITERATURE,
        priority_class=PriorityClass.DISCRIMINATING_CHEAP,
        obligation_id=_hex("ob-3"),
    )
    explore = _candidate(
        "ord-exp",
        created_sequence=4,
        budget_class=BudgetClass.EXPLORATION,
        priority_class=PriorityClass.EXPLORATION,
        obligation_id=_hex("ob-4"),
    )
    policy = _unlimited_policy(max_reasoning_slots=2, max_retrieval_slots=1)
    forward = [stale, missing, first, second, cheap, explore]
    reverse = list(reversed(forward))
    shuffled = [explore, stale, cheap, second, missing, first]
    plans = [_plan(forward, policy=policy), _plan(reverse, policy=policy), _plan(shuffled, policy=policy)]
    reasons = [_reasons(plan) for plan in plans]
    selected = [plan.selected_candidate_ids for plan in plans]
    assert reasons[0] == reasons[1] == reasons[2]
    assert selected[0] == selected[1] == selected[2]
    assert selected[0][0] == "ord-first"
    assert "ord-second" in selected[0]
    assert reasons[0]["ord-stale"] == (
        ScheduleDisposition.REJECTED,
        ScheduleReason.STALE_DEPENDENCY,
    )
    assert reasons[0]["ord-missing"] == (
        ScheduleDisposition.REJECTED,
        ScheduleReason.MISSING_DEPENDENCY,
    )
    assert reasons[0]["ord-exp"] == (
        ScheduleDisposition.DEFERRED,
        ScheduleReason.SLOT_EXHAUSTED,
    )
    for plan, original in zip(plans, (forward, reverse, shuffled)):
        assert {item.candidate_id for item in plan.decisions} == {
            item.candidate_id for item in original
        }
        assert len(plan.decisions) == len(original)


# --------------------------------------------------------------------------- #
# Audit cases: verifier slots, deadline config, mislabel                     #
# --------------------------------------------------------------------------- #


def test_reasoning_and_cached_unknown_settlement():
    policy = _unlimited_policy()
    requested = _vector(reasoning_tokens=21, cached_tokens=8, cost_microusd=2)
    reserved = ResourceSnapshot.empty().reserve(
        requested,
        budget_class=BudgetClass.NORMAL,
        slot_kind=SlotKind.REASONING,
        policy=policy,
    )
    settled = reserved.settle(
        requested,
        ResourceUsage.unknown(),
        budget_class=BudgetClass.NORMAL,
        slot_kind=SlotKind.REASONING,
    )
    assert settled.consumed.reasoning_tokens == 21
    assert settled.consumed.cached_tokens == 8
    missing = ResourceUsage.unknown().to_dict()
    del missing["cached_tokens"]
    _raises(PolicyError, ResourceUsage.from_dict, missing)
    _raises(
        PolicyError,
        ResourceUsage.observed,
        **{name: 0 for name in RESOURCE_DIMENSIONS if name != "reasoning_tokens"},
    )


def test_verifier_independent_while_reasoning_full():
    policy = _unlimited_policy(max_reasoning_slots=2, max_verifier_slots=1)
    r1 = _candidate("ver-r1", created_sequence=1, obligation_id=_hex("vr1"))
    r2 = _candidate("ver-r2", created_sequence=2, obligation_id=_hex("vr2"))
    r3 = _candidate("ver-r3", created_sequence=3, obligation_id=_hex("vr3"))
    verify = _candidate(
        "ver-verify",
        created_sequence=4,
        action_kind=ActionKind.VERIFY,
        budget_class=BudgetClass.FINAL_AUDIT,
        priority_class=PriorityClass.FINAL_ACCEPTANCE,
        obligation_id=_hex("vv"),
    )
    plan = _plan([r3, verify, r2, r1], policy=policy)
    reasons = _reasons(plan)
    assert reasons["ver-r1"][0] is ScheduleDisposition.SELECTED
    assert reasons["ver-r2"][0] is ScheduleDisposition.SELECTED
    assert reasons["ver-r3"] == (
        ScheduleDisposition.DEFERRED,
        ScheduleReason.SLOT_EXHAUSTED,
    )
    assert reasons["ver-verify"] == (
        ScheduleDisposition.SELECTED,
        ScheduleReason.SELECTED,
    )
    occupied = ResourceSnapshot.empty()
    occupied = occupied.reserve(
        _vector(),
        budget_class=BudgetClass.NORMAL,
        slot_kind=SlotKind.REASONING,
        policy=policy,
    )
    occupied = occupied.reserve(
        _vector(),
        budget_class=BudgetClass.NORMAL,
        slot_kind=SlotKind.REASONING,
        policy=policy,
    )
    assert occupied.reasoning_slots_in_use == 2
    assert occupied.verifier_slots_in_use == 0
    still = _plan([verify, r3], policy=policy, snapshot=occupied)
    still_reasons = _reasons(still)
    assert still_reasons["ver-verify"][0] is ScheduleDisposition.SELECTED
    assert still_reasons["ver-r3"][1] is ScheduleReason.SLOT_EXHAUSTED
    assert slot_kind_for_action(ActionKind.VERIFY) is SlotKind.VERIFIER
    assert slot_kind_for_action(ActionKind.AUDIT) is SlotKind.REASONING


def test_invalid_deadline_config():
    _raises(
        PolicyError,
        _unlimited_policy,
        deadline_utc="2026-01-01T12:00:00Z",
        on_deadline=OnDeadlinePolicy.REJECT_NORMAL,
        final_audit_reserve_bp=0,
        max_verifier_slots=1,
    )
    _raises(
        PolicyError,
        _unlimited_policy,
        deadline_utc="2026-01-01T12:00:00Z",
        on_deadline=OnDeadlinePolicy.REJECT_ALL,
        final_audit_reserve_bp=500,
        max_verifier_slots=0,
    )
    allowed = _unlimited_policy(
        deadline_utc="2026-01-01T12:00:00Z",
        on_deadline=OnDeadlinePolicy.ALLOW,
        final_audit_reserve_bp=0,
        max_verifier_slots=0,
    )
    assert allowed.deadline_utc is not None
    ok = _unlimited_policy(
        deadline_utc="2026-01-01T12:00:00Z",
        on_deadline=OnDeadlinePolicy.REJECT_NORMAL,
        final_audit_reserve_bp=1,
        max_verifier_slots=1,
    )
    assert ok.final_audit_reserve_bp == 1
    assert ok.max_verifier_slots == 1


def test_reject_all_final_audit_verify_survives():
    policy = _unlimited_policy(
        deadline_utc="2026-01-01T12:00:00Z",
        on_deadline=OnDeadlinePolicy.REJECT_ALL,
        final_audit_reserve_bp=1000,
        max_verifier_slots=2,
        max_reasoning_slots=2,
    )
    accept = _candidate(
        "all-accept",
        created_sequence=1,
        action_kind=ActionKind.VERIFY,
        budget_class=BudgetClass.FINAL_AUDIT,
        priority_class=PriorityClass.FINAL_ACCEPTANCE,
        obligation_id=_hex("all-a"),
    )
    fa_verify = _candidate(
        "all-fa-verify",
        created_sequence=2,
        action_kind=ActionKind.VERIFY,
        budget_class=BudgetClass.FINAL_AUDIT,
        priority_class=PriorityClass.LOAD_BEARING,
        obligation_id=_hex("all-v"),
        task_mode="verify2",
    )
    fa_audit = _candidate(
        "all-fa-audit",
        created_sequence=3,
        action_kind=ActionKind.AUDIT,
        budget_class=BudgetClass.FINAL_AUDIT,
        priority_class=PriorityClass.LOAD_BEARING,
        obligation_id=_hex("all-aud"),
    )
    normal = _candidate(
        "all-normal",
        created_sequence=4,
        obligation_id=_hex("all-n"),
    )
    plan = _plan(
        [normal, fa_audit, fa_verify, accept],
        policy=policy,
        now_utc=_AFTER_DEADLINE,
    )
    reasons = _reasons(plan)
    assert reasons["all-accept"][0] is ScheduleDisposition.SELECTED
    assert reasons["all-fa-verify"][0] is ScheduleDisposition.SELECTED
    assert reasons["all-fa-audit"] == (
        ScheduleDisposition.DEFERRED,
        ScheduleReason.DEADLINE,
    )
    assert reasons["all-normal"] == (
        ScheduleDisposition.DEFERRED,
        ScheduleReason.DEADLINE,
    )


def test_misclassified_candidates_fail_closed():
    _raises(
        SchedulerError,
        _candidate,
        "mis-fa-class",
        action_kind=ActionKind.VERIFY,
        budget_class=BudgetClass.NORMAL,
        priority_class=PriorityClass.FINAL_ACCEPTANCE,
    )
    _raises(
        SchedulerError,
        _candidate,
        "mis-fa-prove",
        action_kind=ActionKind.PROVE,
        budget_class=BudgetClass.FINAL_AUDIT,
        priority_class=PriorityClass.FINAL_ACCEPTANCE,
    )
    _raises(
        SchedulerError,
        _candidate,
        "mis-fa-audit",
        action_kind=ActionKind.AUDIT,
        budget_class=BudgetClass.FINAL_AUDIT,
        priority_class=PriorityClass.FINAL_ACCEPTANCE,
    )
    _raises(
        SchedulerError,
        _candidate,
        "mis-exp-class",
        budget_class=BudgetClass.NORMAL,
        priority_class=PriorityClass.EXPLORATION,
    )
    ok = _candidate(
        "ok-fa",
        action_kind=ActionKind.VERIFY,
        budget_class=BudgetClass.FINAL_AUDIT,
        priority_class=PriorityClass.FINAL_ACCEPTANCE,
    )
    assert ok.slot_kind is SlotKind.VERIFIER
    assert ok.budget_class is BudgetClass.FINAL_AUDIT


# --------------------------------------------------------------------------- #
# 16. no mutation / no provider / no truth-store writes                       #
# --------------------------------------------------------------------------- #


def _event(event_type: str, payload: dict, key: str) -> EventEnvelope:
    return EventEnvelope.build(
        project_id="proj",
        actor="tester",
        event_type=event_type,
        idempotency_key=key,
        payload=payload,
        timestamp_utc=_TS,
    )


def test_stale_route_requirement_is_rejected_while_direct_route_stays_valid():
    contract = ProblemContract.build(
        problem_id="odd_sum",
        version=1,
        exact_statement="every even integer greater than 2 is a sum of two primes",
        scope=_scope(),
        permitted_tools=("lean",),
        external_help_policy="none",
        target_kind=TargetKind.PROOF,
        required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
        completion_checks=("proof checked",),
        budget_policy_ref="default-budget",
    )
    target = Obligation.build(
        contract_id=contract.contract_id,
        exact_statement=contract.exact_statement,
        scope=contract.scope,
        desired_output="proof",
        created_by="researcher",
        version=1,
    )
    lemma = Obligation.build(
        contract_id=contract.contract_id,
        exact_statement="lemma A",
        scope=contract.scope,
        desired_output="proof",
        created_by="researcher",
        version=1,
    )
    waiting_lemma = Obligation.build(
        contract_id=contract.contract_id,
        exact_statement="lemma B still open",
        scope=contract.scope,
        desired_output="proof",
        created_by="researcher",
        version=2,
    )
    waiting_target = Obligation.build(
        contract_id=contract.contract_id,
        exact_statement="waiting target",
        scope=contract.scope,
        desired_output="proof",
        created_by="researcher",
        version=2,
    )
    evidence = EvidenceArtifact.build(
        content_hash=_hex("blob", "lemma-a"),
        producer="worker-1",
        contract_id=contract.contract_id,
        evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
        exact_claim="lemma A",
        scope=contract.scope,
        environment_hash=_hex("env"),
        created_at=_TS,
        checker_id="checker",
        checker_version="1",
        checker_result=CheckerResult.PASS,
    )
    stale_route = Route.build(
        target_obligation_id=target.obligation_id,
        mechanism="direct",
        required_obligation_ids=(lemma.obligation_id,),
    )
    waiting_route = Route.build(
        target_obligation_id=waiting_target.obligation_id,
        mechanism="direct",
        required_obligation_ids=(waiting_lemma.obligation_id,),
    )
    view = ResearchView()
    view.apply(1, _event("problem.contract.created", {"contract": contract.to_dict()}, "c"))
    view.apply(2, _event("evidence.registered", {"evidence": evidence.to_dict()}, "e"))
    view.apply(3, _event("obligation.created", {"obligation": target.to_dict()}, "o-t"))
    view.apply(4, _event("obligation.created", {"obligation": lemma.to_dict()}, "o-l"))
    view.apply(5, _event("obligation.created", {"obligation": waiting_target.to_dict()}, "o-wt"))
    view.apply(6, _event("obligation.created", {"obligation": waiting_lemma.to_dict()}, "o-wl"))
    view.apply(7, _event("route.created", {"route": stale_route.to_dict()}, "r-s"))
    view.apply(8, _event("route.created", {"route": waiting_route.to_dict()}, "r-w"))
    view.apply(
        9,
        _event(
            "obligation.status_changed",
            {
                "obligation_id": lemma.obligation_id,
                "status": ObligationStatus.DISCHARGED.value,
                "evidence_refs": [evidence.artifact_id],
            },
            "d-l",
        ),
    )
    view.apply(
        10,
        _event("evidence.invalidated", {"artifact_id": evidence.artifact_id}, "inv"),
    )
    stored_route = view.get_route(stale_route.route_id)
    assert stored_route is not None
    assert stored_route.validity is ValidityStatus.VALID
    assert stale_route.route_id not in view.stale_dependencies().route_ids
    assert lemma.obligation_id in view.stale_dependencies().obligation_ids
    assert view.route_readiness(stale_route.route_id) is RouteReadiness.STALE
    assert view.route_readiness(waiting_route.route_id) is RouteReadiness.WAITING

    stale_candidate = _candidate(
        "stale-req",
        created_sequence=1,
        contract_id=contract.contract_id,
        obligation_id=target.obligation_id,
        route_id=stale_route.route_id,
        dependency_validity=DependencyValidity.VALID,
    )
    waiting_candidate = _candidate(
        "waiting-req",
        created_sequence=2,
        contract_id=contract.contract_id,
        obligation_id=waiting_target.obligation_id,
        route_id=waiting_route.route_id,
        dependency_validity=DependencyValidity.VALID,
        mechanism="waiting",
    )
    plan = _plan(
        [waiting_candidate, stale_candidate],
        view=view,
        policy=_unlimited_policy(max_reasoning_slots=2),
    )
    reasons = _reasons(plan)
    assert reasons["stale-req"] == (
        ScheduleDisposition.REJECTED,
        ScheduleReason.STALE_DEPENDENCY,
    )
    assert reasons["waiting-req"][1] is not ScheduleReason.STALE_DEPENDENCY
    assert reasons["waiting-req"][1] is not ScheduleReason.MISSING_DEPENDENCY
    assert view.get_route(stale_route.route_id).validity is ValidityStatus.VALID


def test_no_input_mutation_and_production_modules_are_read_only():
    contract = ProblemContract.build(
        problem_id="odd_sum",
        version=1,
        exact_statement="every even integer greater than 2 is a sum of two primes",
        scope=_scope(),
        permitted_tools=("lean",),
        external_help_policy="none",
        target_kind=TargetKind.PROOF,
        required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
        completion_checks=("proof checked",),
        budget_policy_ref="default-budget",
    )
    obligation = Obligation.build(
        contract_id=contract.contract_id,
        exact_statement=contract.exact_statement,
        scope=contract.scope,
        desired_output="proof",
        created_by="researcher",
        version=1,
    )
    view = ResearchView()
    view.apply(
        1, _event("problem.contract.created", {"contract": contract.to_dict()}, "c")
    )
    view.apply(
        2, _event("obligation.created", {"obligation": obligation.to_dict()}, "o")
    )
    history = RecurrenceHistory()
    index = ActionAttemptIndex()
    snapshot = ResourceSnapshot.empty()
    candidate = _candidate(
        "mut-1",
        obligation_id=obligation.obligation_id,
        contract_id=contract.contract_id,
    )
    other = _candidate(
        "mut-2",
        obligation_id=obligation.obligation_id,
        contract_id=contract.contract_id,
        mechanism="other",
        created_sequence=2,
    )
    candidates = [candidate, other]
    before_view = (view.frontier(), view.obligations(), view.stale_dependencies())
    before_history = (
        dict(history._strikes),
        dict(history._fingerprints),
        {key: set(value) for key, value in history._exhausted.items()},
        history.threshold,
    )
    before_index = (dict(index._by_attempt), {k: list(v) for k, v in index._by_key.items()})
    before_snapshot = snapshot.to_dict()
    before_ids = [item.candidate_id for item in candidates]
    plan = _plan(
        candidates,
        snapshot=snapshot,
        view=view,
        history=history,
        dedupe_index=index,
        policy=_unlimited_policy(max_reasoning_slots=2),
    )
    assert len(plan.decisions) == 2
    assert [item.candidate_id for item in candidates] == before_ids
    assert candidates[0] is candidate
    assert (view.frontier(), view.obligations(), view.stale_dependencies()) == before_view
    assert (
        dict(history._strikes),
        dict(history._fingerprints),
        {key: set(value) for key, value in history._exhausted.items()},
        history.threshold,
    ) == before_history
    assert (
        dict(index._by_attempt),
        {k: list(v) for k, v in index._by_key.items()},
    ) == before_index
    assert snapshot.to_dict() == before_snapshot
    assert snapshot == ResourceSnapshot.empty()

    forbidden_modules = {
        "euler.gateway",
        "euler.verify",
        "euler.execution",
        "euler.core.factgraph",
        "euler.core.global_memory",
        "subprocess",
        "openai",
        "http.client",
        "urllib.request",
    }
    forbidden_names = {
        "FactGraph",
        "fact_submit",
        "GlobalMemory",
        "put_bytes",
        "Popen",
        "urlopen",
    }
    for filename in ("policy.py", "scheduler.py"):
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
            elif isinstance(node, ast.Attribute):
                named.add(node.attr)
        assert imported.isdisjoint(forbidden_modules), (filename, imported)
        assert named.isdisjoint(forbidden_names), (filename, named & forbidden_names)
        assert "write_text" not in named


def test_context_seed_candidate_and_plan_roundtrip():
    candidate = _candidate("roundtrip", created_sequence=3)
    seed = candidate.context_seed
    loaded_seed = ContextSeed.from_dict(seed.to_dict())
    assert loaded_seed == seed
    loaded_candidate = CandidateAction.from_dict(candidate.to_dict())
    assert loaded_candidate == candidate
    plan = _plan((candidate, _candidate("other", created_sequence=4)))
    loaded_plan = DispatchPlan.from_dict(plan.to_dict())
    assert loaded_plan == plan
    decision = plan.decision_for(candidate.candidate_id)
    assert CandidateDecision.from_dict(decision.to_dict()) == decision


def test_serialization_rejects_tamper_extra_fields_and_keeps_invariants():
    candidate = _candidate("tamper")
    payload = candidate.to_dict()
    payload["candidate_id"] = "tamper"
    payload["context_seed"] = dict(payload["context_seed"])
    payload["context_seed"]["seed_hash"] = _hex("wrong-seed")
    _raises(SchedulerError, CandidateAction.from_dict, payload)
    extra = candidate.to_dict()
    extra["unexpected"] = True
    _raises(SchedulerError, CandidateAction.from_dict, extra)
    seed_payload = candidate.context_seed.to_dict()
    seed_payload["extra"] = 1
    _raises(SchedulerError, ContextSeed.from_dict, seed_payload)
    plan = DispatchPlan(
        decisions=(
            CandidateDecision(
                candidate_id="a",
                disposition=ScheduleDisposition.SELECTED,
                reason=ScheduleReason.SELECTED,
                detail="ok",
            ),
        ),
        selected_candidate_ids=("a",),
        idle=False,
    )
    broken = plan.to_dict()
    broken["idle"] = True
    _raises(SchedulerError, DispatchPlan.from_dict, broken)
    selected_mismatch = plan.to_dict()
    selected_mismatch["selected_candidate_ids"] = ()
    _raises(SchedulerError, DispatchPlan.from_dict, selected_mismatch)


def main() -> None:
    tests = [
        test_seed_action_request_construction_has_no_cache_feedback_cycle,
        test_eight_resource_dimensions_and_explicit_unlimited,
        test_unknown_settlement_is_conservative,
        test_reasoning_and_cached_unknown_settlement,
        test_class_reserves_prevent_normal_from_consuming_exclusive_quota,
        test_deadline_rejects_normal_work_and_preserves_final_audit,
        test_invalid_deadline_config,
        test_reject_all_final_audit_verify_survives,
        test_exact_dedupe_wait_recover_retry_behavior,
        test_different_mechanisms_may_parallel_identical_may_not,
        test_two_reasoning_and_configured_compute_retrieval_slots,
        test_verifier_independent_while_reasoning_full,
        test_misclassified_candidates_fail_closed,
        test_audit_required_recurrence_never_auto_parks,
        test_deterministic_decisions_independent_of_input_ordering,
        test_stale_route_requirement_is_rejected_while_direct_route_stays_valid,
        test_no_input_mutation_and_production_modules_are_read_only,
        test_context_seed_candidate_and_plan_roundtrip,
        test_serialization_rejects_tamper_extra_fields_and_keeps_invariants,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL SCHEDULER/POLICY TESTS PASSED")


if __name__ == "__main__":
    main()
