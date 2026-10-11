"""Offline tests for evaluation records, summaries, and deterministic exports.

Covers Phase 7 gates 10–14: failure denominators, unknown/undefined costs,
paired common-solved costs, replay descriptive labelling, family leakage,
config inequality, equal-compute omission, and censored shadow continuations.
Also covers roundtrips, input-order determinism, and AST isolation.

Runs standalone (``python3 -B -m euler.control.tests.test_evaluation``).
"""

from __future__ import annotations

import ast
from pathlib import Path

from euler.control.evaluation import (
    AblationFeature,
    ComparisonContract,
    DatasetSplit,
    EvaluationError,
    EvaluationRun,
    EvaluationSummary,
    MetricStatus,
    RationalMetric,
    RunMode,
    RunOutcome,
    ShadowContinuationRecord,
    ShadowStop,
    ShadowSummary,
    SystemVariant,
    TaskSpec,
    UsageBreakdown,
    export_evaluation_json,
    export_runs_csv,
    summarize_runs,
    summarize_shadow_continuations,
    validate_dataset,
)
from euler.control.ids import sha256_hex
from euler.control.schema import ObservationStatus, ObservedQuantity, UsageRecord

_ROOT = Path(__file__).resolve().parents[1]
_TS = "2026-01-01T00:00:00Z"
_TS2 = "2026-01-01T00:01:00Z"


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


def _qty(unit: str, value: int | None) -> ObservedQuantity:
    if value is None:
        return ObservedQuantity(unit=unit, status=ObservationStatus.UNKNOWN, value=None)
    return ObservedQuantity(unit=unit, status=ObservationStatus.OBSERVED, value=value)


def _usage(*, cost: int | None = 10, wall: int | None = 5, tokens: int | None = 1) -> UsageRecord:
    return UsageRecord(
        input_tokens=_qty("tokens", tokens),
        output_tokens=_qty("tokens", tokens),
        reasoning_tokens=_qty("tokens", 0),
        cached_tokens=_qty("tokens", 0),
        cost_microusd=_qty("microusd", cost),
        wall_ms=_qty("ms", wall),
        tool_cpu_ms=_qty("ms", 0),
        tool_gpu_ms=_qty("ms", 0),
    )


def _breakdown(
    *,
    cost: int | None = 10,
    wall: int | None = 5,
    worker_cost: int | None = None,
    coordinator_cost: int | None = 1,
) -> UsageBreakdown:
    worker = cost if worker_cost is None else worker_cost
    return UsageBreakdown(
        coordinator=_usage(cost=coordinator_cost, wall=1, tokens=0),
        worker=_usage(cost=worker, wall=wall, tokens=2),
        verifier=_usage(cost=1, wall=1, tokens=0),
        context=_usage(cost=0, wall=1, tokens=0),
        retry=_usage(cost=0, wall=0, tokens=0),
        compute=_usage(cost=0, wall=0, tokens=0),
    )


def _task(
    tag: str,
    *,
    family: str = "Goldbach",
    split: DatasetSplit = DatasetSplit.TEST,
    model: str = "model",
    future_derived: bool = False,
    audit: str | None = None,
) -> TaskSpec:
    kwargs = {
        "family": family,
        "split": split,
        "initial_state_hash": _hex("state", tag),
        "cutoff_hash": _hex("cutoff", tag),
        "model_config_hash": _hex(model),
        "tool_config_hash": _hex("tool"),
        "context_config_hash": _hex("ctx"),
        "verifier_config_hash": _hex("ver"),
        "budget_config_hash": _hex("budget", tag),
        "no_future_info_audit_hash": _hex("audit") if audit is None else audit,
        "future_derived": future_derived,
    }
    return TaskSpec.build(**kwargs)


def _comparison(
    *,
    compute_enabled: bool = False,
    variants: tuple[SystemVariant, ...] = (
        SystemVariant.EULER_V1,
        SystemVariant.STRONG_SINGLE_AGENT,
    ),
    model: str = "model",
    ablation: AblationFeature = AblationFeature.NONE,
) -> ComparisonContract:
    return ComparisonContract.build(
        variants=variants,
        model_config_hash=_hex(model),
        tool_config_hash=_hex("tool"),
        context_config_hash=_hex("ctx"),
        verifier_config_hash=_hex("ver"),
        baseline_commit="abc1234",
        baseline_config_hash=_hex("baseline-cfg"),
        compute_enabled=compute_enabled,
        ablation=ablation,
    )


def _run(
    task: TaskSpec,
    variant: SystemVariant,
    *,
    outcome: RunOutcome = RunOutcome.SOLVED,
    seed: int = 0,
    mode: RunMode = RunMode.LIVE_BRANCH,
    cost: int | None = 10,
    verified_error: bool = False,
    acceptance: str | None = None,
) -> EvaluationRun:
    if outcome in {RunOutcome.SOLVED, RunOutcome.REFUTED}:
        acc = _hex("acc", task.task_id, variant.value) if acceptance is None else acceptance
    else:
        acc = None
    return EvaluationRun.build(
        task_id=task.task_id,
        variant=variant,
        seed=seed,
        outcome=outcome,
        run_mode=mode,
        usage=_breakdown(cost=cost),
        verified_error=verified_error,
        started_at=_TS,
        ended_at=_TS2,
        acceptance_id=acc,
    )


def _shadow(
    task: TaskSpec,
    *,
    seed: int = 0,
    stop: ShadowStop = ShadowStop.COMPLETED_SOLVED,
    added: int = 100,
    variant: SystemVariant = SystemVariant.EULER_V1,
    original: RunOutcome = RunOutcome.BUDGET_EXHAUSTED,
    checkpoint: str | None = None,
    usage: UsageBreakdown | None = None,
    acceptance: str | None = None,
    started: str = _TS,
    ended: str = _TS2,
    preselected: bool = True,
) -> ShadowContinuationRecord:
    kwargs = {
        "task_id": task.task_id,
        "variant": variant,
        "seed": seed,
        "checkpoint_id": _hex("ckpt", task.task_id) if checkpoint is None else checkpoint,
        "original_outcome": original,
        "added_budget_microusd": added,
        "added_usage": usage if usage is not None else _breakdown(cost=added),
        "started_at": started,
        "ended_at": ended,
        "stop": stop,
        "preselected": preselected,
    }
    if stop is ShadowStop.COMPLETED_SOLVED:
        kwargs["acceptance_id"] = (
            _hex("sh-acc", task.task_id, str(seed)) if acceptance is None else acceptance
        )
    elif acceptance is not None:
        kwargs["acceptance_id"] = acceptance
    return ShadowContinuationRecord.build(**kwargs)


def test_dataset_rejects_duplication_leakage_audit_and_future_hashes():
    a = _task("a", family="FamA", split=DatasetSplit.TRAIN)
    validate_dataset((a,))
    _raises(EvaluationError, validate_dataset, (a, a))
    leak = _task("b", family="FamA", split=DatasetSplit.TEST)
    _raises(EvaluationError, validate_dataset, (a, leak))
    _raises(EvaluationError, TaskSpec.build, family="", split=DatasetSplit.TEST,
            initial_state_hash=_hex("s"), cutoff_hash=_hex("c"),
            model_config_hash=_hex("m"), tool_config_hash=_hex("t"),
            context_config_hash=_hex("x"), verifier_config_hash=_hex("v"),
            budget_config_hash=_hex("b"), no_future_info_audit_hash=_hex("a"))
    _raises(
        EvaluationError,
        TaskSpec.build,
        family="1bad",
        split=DatasetSplit.TEST,
        initial_state_hash=_hex("s"),
        cutoff_hash=_hex("c"),
        model_config_hash=_hex("m"),
        tool_config_hash=_hex("t"),
        context_config_hash=_hex("x"),
        verifier_config_hash=_hex("v"),
        budget_config_hash=_hex("b"),
        no_future_info_audit_hash=_hex("a"),
    )
    future = _task("c", family="FamC", future_derived=True)
    _raises(EvaluationError, validate_dataset, (future,))


def test_comparison_requires_equal_compute_and_typed_ablation():
    _raises(
        EvaluationError,
        ComparisonContract.build,
        variants=(SystemVariant.EULER_V1, SystemVariant.STRONG_SINGLE_AGENT),
        model_config_hash=_hex("m"),
        tool_config_hash=_hex("t"),
        context_config_hash=_hex("x"),
        verifier_config_hash=_hex("v"),
        baseline_commit="abc",
        baseline_config_hash=_hex("b"),
        compute_enabled=True,
    )
    ok = _comparison(
        compute_enabled=True,
        variants=(
            SystemVariant.EULER_V1,
            SystemVariant.BASELINE_EQUAL_COMPUTE,
            SystemVariant.STRONG_SINGLE_AGENT,
        ),
        ablation=AblationFeature.WITHOUT_STAGNATION_AUDIT,
    )
    assert ok.ablation is AblationFeature.WITHOUT_STAGNATION_AUDIT
    assert SystemVariant.BASELINE_EQUAL_COMPUTE in ok.variants
    for feature in (
        AblationFeature.WITHOUT_RECURRENCE,
        AblationFeature.WITHOUT_CONTEXT_COMPILER,
        AblationFeature.WITHOUT_COMPUTE,
        AblationFeature.FIXED_CONCURRENCY,
        AblationFeature.WITHOUT_STAGNATION_AUDIT,
        AblationFeature.NONE,
    ):
        _comparison(ablation=feature)
    _raises(EvaluationError, ComparisonContract.build,
            variants=(SystemVariant.EULER_V1, SystemVariant.STRONG_SINGLE_AGENT),
            model_config_hash=_hex("m"), tool_config_hash=_hex("t"),
            context_config_hash=_hex("x"), verifier_config_hash=_hex("v"),
            baseline_commit="abc", baseline_config_hash=_hex("b"),
            compute_enabled=False, ablation="WITHOUT_RETRY")
    _raises(EvaluationError, ComparisonContract.build, variants=("FREE_TEXT",),
            model_config_hash=_hex("m"), tool_config_hash=_hex("t"),
            context_config_hash=_hex("x"), verifier_config_hash=_hex("v"),
            baseline_commit="abc", baseline_config_hash=_hex("b"),
            compute_enabled=False)


def test_gate10_denominator_includes_failures_unknown_and_undefined_costs():
    t_solved = _task("s")
    t_failed = _task("f")
    comparison = _comparison()
    runs = (
        _run(t_solved, SystemVariant.EULER_V1, outcome=RunOutcome.SOLVED, cost=20),
        _run(t_failed, SystemVariant.EULER_V1, outcome=RunOutcome.REFUTED, cost=30),
        _run(t_solved, SystemVariant.STRONG_SINGLE_AGENT, outcome=RunOutcome.INFRA_BLOCKED, cost=11),
        _run(t_failed, SystemVariant.STRONG_SINGLE_AGENT, outcome=RunOutcome.FRONTIER_STALLED, cost=12),
    )
    summary = summarize_runs((t_solved, t_failed), runs, comparison)
    solve = dict(summary.solve_at_budget)
    assert solve[SystemVariant.EULER_V1.value].numerator == 2
    assert solve[SystemVariant.EULER_V1.value].denominator == 2
    assert solve[SystemVariant.STRONG_SINGLE_AGENT.value].numerator == 0
    assert solve[SystemVariant.STRONG_SINGLE_AGENT.value].denominator == 2
    cost = dict(summary.cost_per_verified_solve)
    assert cost[SystemVariant.EULER_V1.value].status is MetricStatus.OBSERVED
    assert cost[SystemVariant.EULER_V1.value].numerator == (
        runs[0].usage.dimension_total("cost_microusd").value
        + runs[1].usage.dimension_total("cost_microusd").value
    )
    assert cost[SystemVariant.EULER_V1.value].denominator == 2
    assert cost[SystemVariant.STRONG_SINGLE_AGENT.value].status is MetricStatus.UNDEFINED
    assert cost[SystemVariant.STRONG_SINGLE_AGENT.value].denominator == 0
    assert cost[SystemVariant.STRONG_SINGLE_AGENT.value].numerator is not None

    unknown_runs = (
        _run(t_solved, SystemVariant.EULER_V1, outcome=RunOutcome.SOLVED, cost=None),
        _run(t_failed, SystemVariant.EULER_V1, outcome=RunOutcome.BUDGET_EXHAUSTED, cost=30),
        _run(t_solved, SystemVariant.STRONG_SINGLE_AGENT, outcome=RunOutcome.SOLVED, cost=11),
        _run(t_failed, SystemVariant.STRONG_SINGLE_AGENT, outcome=RunOutcome.BUDGET_EXHAUSTED, cost=12),
    )
    unknown_summary = summarize_runs((t_solved, t_failed), unknown_runs, comparison)
    unknown_cost = dict(unknown_summary.cost_per_verified_solve)
    assert unknown_cost[SystemVariant.EULER_V1.value].status is MetricStatus.UNKNOWN
    assert unknown_cost[SystemVariant.EULER_V1.value].numerator is None
    csv_text = export_runs_csv(unknown_runs)
    assert "UNKNOWN,," in csv_text


def test_gate11_paired_costs_use_common_solved_set():
    t1 = _task("p1")
    t2 = _task("p2")
    comparison = _comparison()
    runs = (
        _run(t1, SystemVariant.EULER_V1, outcome=RunOutcome.SOLVED, cost=40),
        _run(t2, SystemVariant.EULER_V1, outcome=RunOutcome.REFUTED, cost=50),
        _run(t1, SystemVariant.STRONG_SINGLE_AGENT, outcome=RunOutcome.SOLVED, cost=70),
        _run(t2, SystemVariant.STRONG_SINGLE_AGENT, outcome=RunOutcome.REFUTED, cost=80),
    )
    summary = summarize_runs((t1, t2), runs, comparison)
    assert len(summary.paired_costs) == 2
    by_task = {pair.task_id: pair for pair in summary.paired_costs}
    assert t1.task_id in by_task and t2.task_id in by_task
    pair = by_task[t1.task_id]
    assert pair.variant_a is SystemVariant.EULER_V1
    assert pair.variant_b is SystemVariant.STRONG_SINGLE_AGENT
    assert pair.cost_a.value == runs[0].usage.dimension_total("cost_microusd").value
    assert pair.cost_b.value == runs[2].usage.dimension_total("cost_microusd").value


def test_gate12_replay_cannot_claim_causal_savings():
    task = _task("r")
    comparison = _comparison()
    runs = (
        _run(task, SystemVariant.EULER_V1, mode=RunMode.OFFLINE_REPLAY),
        _run(task, SystemVariant.STRONG_SINGLE_AGENT, mode=RunMode.OFFLINE_REPLAY),
    )
    summary = summarize_runs((task,), runs, comparison)
    assert summary.causal_eligible is False
    assert summary.metrics_label == "descriptive"
    payload = export_evaluation_json(summary, runs)
    assert '"metrics_label":"descriptive"' in payload
    assert '"replay_metrics":"descriptive"' in payload
    assert '"causal_eligible":false' in payload
    live = (
        _run(task, SystemVariant.EULER_V1, mode=RunMode.LIVE_BRANCH),
        _run(task, SystemVariant.STRONG_SINGLE_AGENT, mode=RunMode.LIVE_BRANCH),
    )
    live_summary = summarize_runs((task,), live, comparison)
    assert live_summary.causal_eligible is True
    assert live_summary.metrics_label == "causal"
    live_json = export_evaluation_json(live_summary, live)
    assert '"replay_metrics":"not_applicable"' in live_json


def test_gate13_family_leakage_config_inequality_equal_compute():
    train = _task("l1", family="SameFam", split=DatasetSplit.TRAIN)
    test = _task("l2", family="SameFam", split=DatasetSplit.TEST)
    _raises(EvaluationError, validate_dataset, (train, test))
    a = _task("c1", model="model-a")
    b = _task("c2", model="model-b")
    comparison = _comparison(model="model-a")
    runs = (
        _run(a, SystemVariant.EULER_V1),
        _run(b, SystemVariant.EULER_V1),
        _run(a, SystemVariant.STRONG_SINGLE_AGENT),
        _run(b, SystemVariant.STRONG_SINGLE_AGENT),
    )
    _raises(EvaluationError, summarize_runs, (a, b), runs, comparison)
    _raises(
        EvaluationError,
        ComparisonContract.build,
        variants=(SystemVariant.EULER_V1, SystemVariant.STRONG_SINGLE_AGENT),
        model_config_hash=_hex("m"),
        tool_config_hash=_hex("t"),
        context_config_hash=_hex("x"),
        verifier_config_hash=_hex("v"),
        baseline_commit="abc",
        baseline_config_hash=_hex("b"),
        compute_enabled=True,
    )


def test_gate14_censored_shadow_is_not_a_successful_stop():
    task = _task("sh")
    records = (
        _shadow(task, seed=0, stop=ShadowStop.COMPLETED_SOLVED),
        _shadow(task, seed=1, stop=ShadowStop.COMPLETED_NOT_SOLVED),
        _shadow(task, seed=2, stop=ShadowStop.CENSORED),
    )
    summary = summarize_shadow_continuations(records)
    assert summary.n_solved == 1
    assert summary.n_not_solved == 1
    assert summary.n_censored == 1
    assert summary.censored_counted_as_success is False
    assert summary.solve_lower.numerator == 1
    assert summary.solve_lower.denominator == 3
    assert summary.solve_upper.numerator == 2
    assert summary.solve_upper.denominator == 3
    _raises(
        EvaluationError,
        _shadow,
        task,
        seed=3,
        stop=ShadowStop.CENSORED,
        preselected=False,
    )
    mixed = records + (
        _shadow(
            task,
            variant=SystemVariant.STRONG_SINGLE_AGENT,
            seed=0,
            added=50,
            stop=ShadowStop.CENSORED,
        ),
    )
    _raises(EvaluationError, summarize_shadow_continuations, mixed)


def test_roundtrips_and_input_order_determinism():
    t1 = _task("d1")
    t2 = _task("d2")
    comparison = _comparison()
    runs = (
        _run(t1, SystemVariant.EULER_V1, outcome=RunOutcome.SOLVED, cost=4),
        _run(t2, SystemVariant.EULER_V1, outcome=RunOutcome.REFUTED, cost=5),
        _run(t1, SystemVariant.STRONG_SINGLE_AGENT, outcome=RunOutcome.SOLVED, cost=9),
        _run(t2, SystemVariant.STRONG_SINGLE_AGENT, outcome=RunOutcome.NEEDS_EXTERNAL_INPUT, cost=8),
    )
    summary = summarize_runs((t1, t2), runs, comparison)
    assert EvaluationSummary.from_dict(summary.to_dict()) == summary
    assert ComparisonContract.from_dict(comparison.to_dict()) == comparison
    assert TaskSpec.from_dict(t1.to_dict()) == t1
    for run in runs:
        assert EvaluationRun.from_dict(run.to_dict()) == run
    reversed_summary = summarize_runs((t2, t1), tuple(reversed(runs)), comparison)
    assert export_evaluation_json(summary, runs) == export_evaluation_json(
        reversed_summary, tuple(reversed(runs))
    )
    assert export_runs_csv(runs) == export_runs_csv(tuple(reversed(runs)))
    csv_text = export_runs_csv(runs)
    header = csv_text.split("\n", 1)[0]
    assert header.startswith("run_id,task_id,variant,seed,outcome")
    shadow = summarize_shadow_continuations((_shadow(t1, added=7),))
    assert ShadowSummary.from_dict(shadow.to_dict()) == shadow
    exported = export_evaluation_json(summary, runs, shadow)
    assert '"kind":"evaluation_summary"' in exported
    unknown = RationalMetric.unknown()
    assert RationalMetric.from_dict(unknown.to_dict()) == unknown


def test_usage_breakdown_unknown_dominates_sum():
    mixed = UsageBreakdown(
        coordinator=_usage(cost=1, wall=1),
        worker=_usage(cost=None, wall=2),
        verifier=_usage(cost=1, wall=1),
        context=_usage(cost=0, wall=1),
        retry=_usage(cost=0, wall=0),
        compute=_usage(cost=0, wall=0),
    )
    total = mixed.dimension_total("cost_microusd")
    assert total.status is ObservationStatus.UNKNOWN
    assert total.value is None
    wall = mixed.dimension_total("wall_ms")
    assert wall.status is ObservationStatus.OBSERVED
    assert wall.value == 1 + 2 + 1 + 1 + 0 + 0


def test_accepted_terminal_requires_acceptance_id():
    task = _task("acc")
    _raises(
        EvaluationError,
        EvaluationRun.build,
        task_id=task.task_id,
        variant=SystemVariant.EULER_V1,
        seed=0,
        outcome=RunOutcome.SOLVED,
        run_mode=RunMode.LIVE_BRANCH,
        usage=_breakdown(),
        verified_error=False,
        started_at=_TS,
        ended_at=_TS2,
        acceptance_id=None,
    )
    paused = EvaluationRun.build(
        task_id=task.task_id,
        variant=SystemVariant.EULER_V1,
        seed=0,
        outcome=RunOutcome.FRONTIER_STALLED,
        run_mode=RunMode.LIVE_BRANCH,
        usage=_breakdown(),
        verified_error=False,
        started_at=_TS,
        ended_at=_TS2,
        acceptance_id=None,
    )
    assert paused.acceptance_id is None


def test_run_id_binds_usage_and_time_order():
    task = _task("idbind")
    first = _run(task, SystemVariant.EULER_V1, cost=10)
    cheaper = _run(task, SystemVariant.EULER_V1, cost=11)
    later = EvaluationRun.build(
        task_id=task.task_id,
        variant=SystemVariant.EULER_V1,
        seed=0,
        outcome=RunOutcome.SOLVED,
        run_mode=RunMode.LIVE_BRANCH,
        usage=_breakdown(cost=10),
        verified_error=False,
        started_at=_TS,
        ended_at="2026-01-01T00:02:00Z",
        acceptance_id=first.acceptance_id,
    )
    assert first.run_id != cheaper.run_id
    assert first.run_id != later.run_id
    _raises(
        EvaluationError,
        EvaluationRun.build,
        task_id=task.task_id,
        variant=SystemVariant.EULER_V1,
        seed=1,
        outcome=RunOutcome.BUDGET_EXHAUSTED,
        run_mode=RunMode.LIVE_BRANCH,
        usage=_breakdown(),
        verified_error=False,
        started_at=_TS2,
        ended_at=_TS,
        acceptance_id=None,
    )


def test_shadow_checkpoint_usage_and_acceptance_sensitivity():
    task = _task("sh2")
    base = _shadow(task, seed=0, stop=ShadowStop.COMPLETED_SOLVED)
    other_ckpt = _shadow(
        task, seed=0, stop=ShadowStop.COMPLETED_SOLVED, checkpoint=_hex("ckpt-other")
    )
    other_usage = _shadow(
        task,
        seed=0,
        stop=ShadowStop.COMPLETED_SOLVED,
        usage=_breakdown(cost=101),
    )
    assert base.record_id != other_ckpt.record_id
    assert base.record_id != other_usage.record_id
    _raises(
        EvaluationError,
        _shadow,
        task,
        stop=ShadowStop.CENSORED,
        acceptance=_hex("bad-acc"),
    )
    _raises(
        EvaluationError,
        _shadow,
        task,
        stop=ShadowStop.COMPLETED_NOT_SOLVED,
        acceptance=_hex("bad-acc"),
    )
    _raises(
        EvaluationError,
        _shadow,
        task,
        original=RunOutcome.SOLVED,
    )
    _raises(
        EvaluationError,
        _shadow,
        task,
        started=_TS2,
        ended=_TS,
    )
    restored = ShadowContinuationRecord.from_dict(base.to_dict())
    assert restored == base


def test_source_isolation():
    forbidden_modules = {
        "subprocess",
        "socket",
        "requests",
        "httpx",
        "pandas",
        "numpy",
        "euler.core.factgraph",
        "euler.gateway",
        "euler.verify",
        "euler.control.views",
        "euler.control.runtime",
        "euler.control.acceptance",
    }
    forbidden_names = {"eval", "exec", "compile"}
    forbidden_attrs = {"Popen", "urlopen"}
    path = _ROOT / "evaluation.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    imported: set[str] = set()
    named: set[str] = set()
    name_calls: set[str] = set()
    attr_calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
                named.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            for alias in node.names:
                named.add(alias.name)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                name_calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                attr_calls.add(node.func.attr)
    assert imported.isdisjoint(forbidden_modules)
    assert "FactGraph" not in named
    assert "EvidenceArtifact" not in named
    assert name_calls.isdisjoint(forbidden_names)
    assert attr_calls.isdisjoint(forbidden_attrs)
    assert "open(" not in source
    assert "Path(" not in source
    assert "float(" not in source


def main() -> None:
    tests = [
        test_dataset_rejects_duplication_leakage_audit_and_future_hashes,
        test_comparison_requires_equal_compute_and_typed_ablation,
        test_gate10_denominator_includes_failures_unknown_and_undefined_costs,
        test_gate11_paired_costs_use_common_solved_set,
        test_gate12_replay_cannot_claim_causal_savings,
        test_gate13_family_leakage_config_inequality_equal_compute,
        test_gate14_censored_shadow_is_not_a_successful_stop,
        test_roundtrips_and_input_order_determinism,
        test_usage_breakdown_unknown_dominates_sum,
        test_accepted_terminal_requires_acceptance_id,
        test_run_id_binds_usage_and_time_order,
        test_shadow_checkpoint_usage_and_acceptance_sensitivity,
        test_source_isolation,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL EVALUATION TESTS PASSED")


if __name__ == "__main__":
    main()
