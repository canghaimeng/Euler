"""Reproducible evaluation records, summaries, and deterministic exports.

Stdlib only. Evaluation consumes immutable records and never writes a store,
calls a provider, or admits a Fact. Unknown usage is never converted to zero.
Replay metrics are labelled descriptive and are not causal.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Mapping

from euler.control.ids import canonical_json, stable_id
from euler.control.schema import ObservationStatus, ObservedQuantity, UsageRecord

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_FAMILY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
_UTC = timezone.utc
_ZERO = timedelta(0)
_ID_VERSION = 1
_USAGE_DIMENSIONS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "cost_microusd",
    "wall_ms",
    "tool_cpu_ms",
    "tool_gpu_ms",
)
_USAGE_UNITS = {
    "input_tokens": "tokens",
    "output_tokens": "tokens",
    "reasoning_tokens": "tokens",
    "cached_tokens": "tokens",
    "cost_microusd": "microusd",
    "wall_ms": "ms",
    "tool_cpu_ms": "ms",
    "tool_gpu_ms": "ms",
}
_USAGE_CATEGORIES = (
    "coordinator",
    "worker",
    "verifier",
    "context",
    "retry",
    "compute",
)
_OVERHEAD_CATEGORIES = ("coordinator", "context", "retry")
_TERMINAL_ACCEPTED = frozenset({"SOLVED", "REFUTED"})
_PAUSE_OUTCOMES = frozenset(
    {
        "BUDGET_EXHAUSTED",
        "FRONTIER_STALLED",
        "NEEDS_EXTERNAL_INPUT",
        "INFRA_BLOCKED",
    }
)
_CSV_COLUMNS = (
    "run_id",
    "task_id",
    "variant",
    "seed",
    "outcome",
    "acceptance_id",
    "run_mode",
    "verified_error",
    "started_at",
    "ended_at",
    "cost_microusd_status",
    "cost_microusd",
    "wall_ms_status",
    "wall_ms",
    "input_tokens_status",
    "input_tokens",
    "output_tokens_status",
    "output_tokens",
    "tool_cpu_ms_status",
    "tool_cpu_ms",
    "tool_gpu_ms_status",
    "tool_gpu_ms",
    "overhead_wall_ms_status",
    "overhead_wall_ms",
)

__all__ = [
    "EvaluationError",
    "DatasetSplit",
    "RunMode",
    "SystemVariant",
    "AblationFeature",
    "MetricStatus",
    "RunOutcome",
    "ShadowStop",
    "TaskSpec",
    "ComparisonContract",
    "UsageBreakdown",
    "EvaluationRun",
    "RationalMetric",
    "PairedCostRecord",
    "EvaluationSummary",
    "ShadowContinuationRecord",
    "ShadowSummary",
    "validate_dataset",
    "summarize_runs",
    "summarize_shadow_continuations",
    "export_evaluation_json",
    "export_runs_csv",
]


class EvaluationError(ValueError):
    """Malformed evaluation record, dataset, or an illegal summary."""


class DatasetSplit(str, Enum):
    TRAIN = "TRAIN"
    DEV = "DEV"
    TEST = "TEST"


class RunMode(str, Enum):
    LIVE_BRANCH = "LIVE_BRANCH"
    OFFLINE_REPLAY = "OFFLINE_REPLAY"


class SystemVariant(str, Enum):
    STRONG_SINGLE_AGENT = "STRONG_SINGLE_AGENT"
    BASELINE_PINNED = "BASELINE_PINNED"
    BASELINE_HARD_BUDGET = "BASELINE_HARD_BUDGET"
    BASELINE_PROMPT_CONTROL = "BASELINE_PROMPT_CONTROL"
    BASELINE_EQUAL_COMPUTE = "BASELINE_EQUAL_COMPUTE"
    EULER_V1 = "EULER_V1"


class AblationFeature(str, Enum):
    NONE = "NONE"
    WITHOUT_RECURRENCE = "WITHOUT_RECURRENCE"
    WITHOUT_CONTEXT_COMPILER = "WITHOUT_CONTEXT_COMPILER"
    WITHOUT_COMPUTE = "WITHOUT_COMPUTE"
    FIXED_CONCURRENCY = "FIXED_CONCURRENCY"
    WITHOUT_STAGNATION_AUDIT = "WITHOUT_STAGNATION_AUDIT"


class MetricStatus(str, Enum):
    OBSERVED = "OBSERVED"
    UNKNOWN = "UNKNOWN"
    UNDEFINED = "UNDEFINED"


class RunOutcome(str, Enum):
    SOLVED = "SOLVED"
    REFUTED = "REFUTED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    FRONTIER_STALLED = "FRONTIER_STALLED"
    NEEDS_EXTERNAL_INPUT = "NEEDS_EXTERNAL_INPUT"
    INFRA_BLOCKED = "INFRA_BLOCKED"


class ShadowStop(str, Enum):
    COMPLETED_SOLVED = "COMPLETED_SOLVED"
    COMPLETED_NOT_SOLVED = "COMPLETED_NOT_SOLVED"
    CENSORED = "CENSORED"


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskSpec:
    """One evaluation task: family/split, initial state, cutoff, and config hashes."""

    task_id: str
    family: str
    split: DatasetSplit
    initial_state_hash: str
    cutoff_hash: str
    model_config_hash: str
    tool_config_hash: str
    context_config_hash: str
    verifier_config_hash: str
    budget_config_hash: str
    no_future_info_audit_hash: str
    future_derived: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "family", _require_family(self.family))
        object.__setattr__(self, "split", _require_enum(DatasetSplit, self.split, "split"))
        for name in (
            "initial_state_hash",
            "cutoff_hash",
            "model_config_hash",
            "tool_config_hash",
            "context_config_hash",
            "verifier_config_hash",
            "budget_config_hash",
            "no_future_info_audit_hash",
        ):
            object.__setattr__(self, name, _require_hex64(name, getattr(self, name)))
        if not isinstance(self.future_derived, bool):
            raise EvaluationError("future_derived must be a bool")
        expected = _task_id(
            family=self.family,
            split=self.split,
            initial_state_hash=self.initial_state_hash,
            cutoff_hash=self.cutoff_hash,
            model_config_hash=self.model_config_hash,
            tool_config_hash=self.tool_config_hash,
            context_config_hash=self.context_config_hash,
            verifier_config_hash=self.verifier_config_hash,
            budget_config_hash=self.budget_config_hash,
            no_future_info_audit_hash=self.no_future_info_audit_hash,
            future_derived=self.future_derived,
        )
        object.__setattr__(self, "task_id", _require_hex64("task_id", self.task_id))
        if self.task_id != expected:
            raise EvaluationError("task_id does not match task semantics")

    @classmethod
    def build(cls, **kwargs: Any) -> "TaskSpec":
        payload = dict(kwargs)
        split = _require_enum(DatasetSplit, payload.get("split"), "split")
        family = _require_family(payload.get("family"))
        future_derived = payload.get("future_derived", False)
        if not isinstance(future_derived, bool):
            raise EvaluationError("future_derived must be a bool")
        task_id = _task_id(
            family=family,
            split=split,
            initial_state_hash=_require_hex64(
                "initial_state_hash", payload.get("initial_state_hash")
            ),
            cutoff_hash=_require_hex64("cutoff_hash", payload.get("cutoff_hash")),
            model_config_hash=_require_hex64(
                "model_config_hash", payload.get("model_config_hash")
            ),
            tool_config_hash=_require_hex64(
                "tool_config_hash", payload.get("tool_config_hash")
            ),
            context_config_hash=_require_hex64(
                "context_config_hash", payload.get("context_config_hash")
            ),
            verifier_config_hash=_require_hex64(
                "verifier_config_hash", payload.get("verifier_config_hash")
            ),
            budget_config_hash=_require_hex64(
                "budget_config_hash", payload.get("budget_config_hash")
            ),
            no_future_info_audit_hash=_require_hex64(
                "no_future_info_audit_hash", payload.get("no_future_info_audit_hash")
            ),
            future_derived=future_derived,
        )
        payload["task_id"] = task_id
        payload["family"] = family
        payload["split"] = split
        payload["future_derived"] = future_derived
        return cls(**payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget_config_hash": self.budget_config_hash,
            "context_config_hash": self.context_config_hash,
            "cutoff_hash": self.cutoff_hash,
            "family": self.family,
            "future_derived": self.future_derived,
            "initial_state_hash": self.initial_state_hash,
            "model_config_hash": self.model_config_hash,
            "no_future_info_audit_hash": self.no_future_info_audit_hash,
            "split": self.split.value,
            "task_id": self.task_id,
            "tool_config_hash": self.tool_config_hash,
            "verifier_config_hash": self.verifier_config_hash,
        }

    @classmethod
    def from_dict(cls, data: object) -> "TaskSpec":
        payload = _mapping("TaskSpec", data)
        allowed = {
            "budget_config_hash",
            "context_config_hash",
            "cutoff_hash",
            "family",
            "future_derived",
            "initial_state_hash",
            "model_config_hash",
            "no_future_info_audit_hash",
            "split",
            "task_id",
            "tool_config_hash",
            "verifier_config_hash",
        }
        _reject_extra(payload, allowed, "TaskSpec")
        _require_keys(payload, allowed, "TaskSpec")
        return cls(
            task_id=payload["task_id"],
            family=payload["family"],
            split=payload["split"],
            initial_state_hash=payload["initial_state_hash"],
            cutoff_hash=payload["cutoff_hash"],
            model_config_hash=payload["model_config_hash"],
            tool_config_hash=payload["tool_config_hash"],
            context_config_hash=payload["context_config_hash"],
            verifier_config_hash=payload["verifier_config_hash"],
            budget_config_hash=payload["budget_config_hash"],
            no_future_info_audit_hash=payload["no_future_info_audit_hash"],
            future_derived=payload["future_derived"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ComparisonContract:
    """Equal model/tool/context/final-verifier comparison with explicit baseline."""

    comparison_id: str
    variants: tuple[SystemVariant, ...]
    model_config_hash: str
    tool_config_hash: str
    context_config_hash: str
    verifier_config_hash: str
    baseline_commit: str
    baseline_config_hash: str
    compute_enabled: bool
    ablation: AblationFeature = AblationFeature.NONE

    def __post_init__(self) -> None:
        variants = _require_variants(self.variants)
        object.__setattr__(self, "variants", variants)
        object.__setattr__(
            self, "ablation", _require_enum(AblationFeature, self.ablation, "ablation")
        )
        if not isinstance(self.compute_enabled, bool):
            raise EvaluationError("compute_enabled must be a bool")
        if self.compute_enabled and SystemVariant.BASELINE_EQUAL_COMPUTE not in variants:
            raise EvaluationError(
                "compute-enabled comparisons require BASELINE_EQUAL_COMPUTE"
            )
        for name in (
            "model_config_hash",
            "tool_config_hash",
            "context_config_hash",
            "verifier_config_hash",
            "baseline_config_hash",
        ):
            object.__setattr__(self, name, _require_hex64(name, getattr(self, name)))
        object.__setattr__(
            self, "baseline_commit", _require_nonempty("baseline_commit", self.baseline_commit)
        )
        expected = _comparison_id(
            variants=variants,
            model_config_hash=self.model_config_hash,
            tool_config_hash=self.tool_config_hash,
            context_config_hash=self.context_config_hash,
            verifier_config_hash=self.verifier_config_hash,
            baseline_commit=self.baseline_commit,
            baseline_config_hash=self.baseline_config_hash,
            compute_enabled=self.compute_enabled,
            ablation=self.ablation,
        )
        object.__setattr__(
            self, "comparison_id", _require_hex64("comparison_id", self.comparison_id)
        )
        if self.comparison_id != expected:
            raise EvaluationError("comparison_id does not match comparison semantics")

    @classmethod
    def build(cls, **kwargs: Any) -> "ComparisonContract":
        payload = dict(kwargs)
        variants = _require_variants(payload.get("variants"))
        ablation = _require_enum(
            AblationFeature, payload.get("ablation", AblationFeature.NONE), "ablation"
        )
        compute_enabled = payload.get("compute_enabled")
        if not isinstance(compute_enabled, bool):
            raise EvaluationError("compute_enabled must be a bool")
        comparison_id = _comparison_id(
            variants=variants,
            model_config_hash=_require_hex64(
                "model_config_hash", payload.get("model_config_hash")
            ),
            tool_config_hash=_require_hex64(
                "tool_config_hash", payload.get("tool_config_hash")
            ),
            context_config_hash=_require_hex64(
                "context_config_hash", payload.get("context_config_hash")
            ),
            verifier_config_hash=_require_hex64(
                "verifier_config_hash", payload.get("verifier_config_hash")
            ),
            baseline_commit=_require_nonempty(
                "baseline_commit", payload.get("baseline_commit")
            ),
            baseline_config_hash=_require_hex64(
                "baseline_config_hash", payload.get("baseline_config_hash")
            ),
            compute_enabled=compute_enabled,
            ablation=ablation,
        )
        payload["comparison_id"] = comparison_id
        payload["variants"] = variants
        payload["ablation"] = ablation
        return cls(**payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ablation": self.ablation.value,
            "baseline_commit": self.baseline_commit,
            "baseline_config_hash": self.baseline_config_hash,
            "comparison_id": self.comparison_id,
            "compute_enabled": self.compute_enabled,
            "context_config_hash": self.context_config_hash,
            "model_config_hash": self.model_config_hash,
            "tool_config_hash": self.tool_config_hash,
            "variants": [item.value for item in self.variants],
            "verifier_config_hash": self.verifier_config_hash,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ComparisonContract":
        payload = _mapping("ComparisonContract", data)
        allowed = {
            "ablation",
            "baseline_commit",
            "baseline_config_hash",
            "comparison_id",
            "compute_enabled",
            "context_config_hash",
            "model_config_hash",
            "tool_config_hash",
            "variants",
            "verifier_config_hash",
        }
        _reject_extra(payload, allowed, "ComparisonContract")
        _require_keys(payload, allowed, "ComparisonContract")
        raw_variants = payload["variants"]
        if isinstance(raw_variants, str) or not isinstance(raw_variants, (list, tuple)):
            raise EvaluationError("variants must be a list")
        return cls(
            comparison_id=payload["comparison_id"],
            variants=tuple(raw_variants),
            model_config_hash=payload["model_config_hash"],
            tool_config_hash=payload["tool_config_hash"],
            context_config_hash=payload["context_config_hash"],
            verifier_config_hash=payload["verifier_config_hash"],
            baseline_commit=payload["baseline_commit"],
            baseline_config_hash=payload["baseline_config_hash"],
            compute_enabled=payload["compute_enabled"],
            ablation=payload["ablation"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageBreakdown:
    """Per-category usage. Dimension totals are UNKNOWN if any contributor is."""

    coordinator: UsageRecord
    worker: UsageRecord
    verifier: UsageRecord
    context: UsageRecord
    retry: UsageRecord
    compute: UsageRecord

    def __post_init__(self) -> None:
        for name in _USAGE_CATEGORIES:
            value = getattr(self, name)
            if not isinstance(value, UsageRecord):
                raise EvaluationError(f"{name} must be a UsageRecord")

    def records(self) -> tuple[UsageRecord, ...]:
        return tuple(getattr(self, name) for name in _USAGE_CATEGORIES)

    def dimension_total(self, name: str) -> ObservedQuantity:
        if name not in _USAGE_UNITS:
            raise EvaluationError(f"unknown usage dimension {name!r}")
        return _sum_quantities(
            [getattr(record, name) for record in self.records()],
            _USAGE_UNITS[name],
        )

    def overhead_wall_ms(self) -> ObservedQuantity:
        return _sum_quantities(
            [getattr(self, name).wall_ms for name in _OVERHEAD_CATEGORIES],
            "ms",
        )

    def overhead_cost_microusd(self) -> ObservedQuantity:
        return _sum_quantities(
            [getattr(self, name).cost_microusd for name in _OVERHEAD_CATEGORIES],
            "microusd",
        )

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name).to_dict() for name in _USAGE_CATEGORIES}

    @classmethod
    def from_dict(cls, data: object) -> "UsageBreakdown":
        payload = _mapping("UsageBreakdown", data)
        _reject_extra(payload, set(_USAGE_CATEGORIES), "UsageBreakdown")
        _require_keys(payload, set(_USAGE_CATEGORIES), "UsageBreakdown")
        return cls(
            **{
                name: UsageRecord.from_dict(payload[name])
                for name in _USAGE_CATEGORIES
            }
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationRun:
    """One task/variant/seed execution, including failed and paused tasks."""

    run_id: str
    task_id: str
    variant: SystemVariant
    seed: int
    outcome: RunOutcome
    run_mode: RunMode
    usage: UsageBreakdown
    verified_error: bool
    started_at: str
    ended_at: str
    acceptance_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _require_hex64("task_id", self.task_id))
        object.__setattr__(
            self, "variant", _require_enum(SystemVariant, self.variant, "variant")
        )
        object.__setattr__(self, "seed", _require_nonneg_int("seed", self.seed))
        object.__setattr__(
            self, "outcome", _require_enum(RunOutcome, self.outcome, "outcome")
        )
        object.__setattr__(
            self, "run_mode", _require_enum(RunMode, self.run_mode, "run_mode")
        )
        if not isinstance(self.usage, UsageBreakdown):
            raise EvaluationError("usage must be a UsageBreakdown")
        if not isinstance(self.verified_error, bool):
            raise EvaluationError("verified_error must be a bool")
        started = _parse_utc("started_at", self.started_at)
        ended = _parse_utc("ended_at", self.ended_at)
        if ended < started:
            raise EvaluationError("ended_at must be >= started_at")
        if self.outcome.value in _TERMINAL_ACCEPTED:
            object.__setattr__(
                self,
                "acceptance_id",
                _require_hex64("acceptance_id", self.acceptance_id),
            )
        elif self.acceptance_id is not None:
            raise EvaluationError("acceptance_id is only allowed for accepted terminals")
        expected = _run_id(
            task_id=self.task_id,
            variant=self.variant,
            seed=self.seed,
            outcome=self.outcome,
            run_mode=self.run_mode,
            verified_error=self.verified_error,
            acceptance_id=self.acceptance_id,
            usage=self.usage,
            started_at=self.started_at,
            ended_at=self.ended_at,
        )
        object.__setattr__(self, "run_id", _require_hex64("run_id", self.run_id))
        if self.run_id != expected:
            raise EvaluationError("run_id does not match run semantics")

    @classmethod
    def build(cls, **kwargs: Any) -> "EvaluationRun":
        payload = dict(kwargs)
        variant = _require_enum(SystemVariant, payload.get("variant"), "variant")
        outcome = _require_enum(RunOutcome, payload.get("outcome"), "outcome")
        run_mode = _require_enum(RunMode, payload.get("run_mode"), "run_mode")
        seed = _require_nonneg_int("seed", payload.get("seed"))
        verified_error = payload.get("verified_error")
        if not isinstance(verified_error, bool):
            raise EvaluationError("verified_error must be a bool")
        task_id = _require_hex64("task_id", payload.get("task_id"))
        acceptance_id = payload.get("acceptance_id")
        if outcome.value in _TERMINAL_ACCEPTED:
            acceptance_id = _require_hex64("acceptance_id", acceptance_id)
        elif acceptance_id is not None:
            raise EvaluationError("acceptance_id is only allowed for accepted terminals")
        usage = payload.get("usage")
        if not isinstance(usage, UsageBreakdown):
            raise EvaluationError("usage must be a UsageBreakdown")
        started_at = _require_nonempty("started_at", payload.get("started_at"))
        ended_at = _require_nonempty("ended_at", payload.get("ended_at"))
        payload["run_id"] = _run_id(
            task_id=task_id,
            variant=variant,
            seed=seed,
            outcome=outcome,
            run_mode=run_mode,
            verified_error=verified_error,
            acceptance_id=acceptance_id,
            usage=usage,
            started_at=started_at,
            ended_at=ended_at,
        )
        payload["variant"] = variant
        payload["outcome"] = outcome
        payload["run_mode"] = run_mode
        payload["seed"] = seed
        payload["acceptance_id"] = acceptance_id
        return cls(**payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "acceptance_id": self.acceptance_id,
            "ended_at": self.ended_at,
            "outcome": self.outcome.value,
            "run_id": self.run_id,
            "run_mode": self.run_mode.value,
            "seed": self.seed,
            "started_at": self.started_at,
            "task_id": self.task_id,
            "usage": self.usage.to_dict(),
            "variant": self.variant.value,
            "verified_error": self.verified_error,
        }

    @classmethod
    def from_dict(cls, data: object) -> "EvaluationRun":
        payload = _mapping("EvaluationRun", data)
        allowed = {
            "acceptance_id",
            "ended_at",
            "outcome",
            "run_id",
            "run_mode",
            "seed",
            "started_at",
            "task_id",
            "usage",
            "variant",
            "verified_error",
        }
        _reject_extra(payload, allowed, "EvaluationRun")
        _require_keys(payload, allowed, "EvaluationRun")
        return cls(
            run_id=payload["run_id"],
            task_id=payload["task_id"],
            variant=payload["variant"],
            seed=payload["seed"],
            outcome=payload["outcome"],
            run_mode=payload["run_mode"],
            usage=UsageBreakdown.from_dict(payload["usage"]),
            verified_error=payload["verified_error"],
            started_at=payload["started_at"],
            ended_at=payload["ended_at"],
            acceptance_id=payload["acceptance_id"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RationalMetric:
    """Integer ratio. UNKNOWN/UNDEFINED never store a fabricated zero cost."""

    status: MetricStatus
    numerator: int | None = None
    denominator: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", _require_enum(MetricStatus, self.status, "status")
        )
        if self.status is MetricStatus.OBSERVED:
            num = _require_nonneg_int("numerator", self.numerator)
            den = _require_positive_int("denominator", self.denominator)
            object.__setattr__(self, "numerator", num)
            object.__setattr__(self, "denominator", den)
            return
        if self.status is MetricStatus.UNKNOWN:
            if self.numerator is not None or self.denominator is not None:
                raise EvaluationError("UNKNOWN RationalMetric requires null ratio")
            return
        if self.denominator is not None:
            object.__setattr__(
                self, "denominator", _require_nonneg_int("denominator", self.denominator)
            )
        if self.numerator is not None:
            object.__setattr__(
                self, "numerator", _require_nonneg_int("numerator", self.numerator)
            )

    @classmethod
    def observed(cls, numerator: int, denominator: int) -> "RationalMetric":
        return cls(
            status=MetricStatus.OBSERVED,
            numerator=numerator,
            denominator=denominator,
        )

    @classmethod
    def unknown(cls) -> "RationalMetric":
        return cls(status=MetricStatus.UNKNOWN)

    @classmethod
    def undefined(
        cls, *, numerator: int | None = None, denominator: int | None = 0
    ) -> "RationalMetric":
        return cls(
            status=MetricStatus.UNDEFINED,
            numerator=numerator,
            denominator=denominator,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "denominator": self.denominator,
            "numerator": self.numerator,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, data: object) -> "RationalMetric":
        payload = _mapping("RationalMetric", data)
        _reject_extra(payload, {"denominator", "numerator", "status"}, "RationalMetric")
        _require_keys(payload, {"denominator", "numerator", "status"}, "RationalMetric")
        return cls(
            status=payload["status"],
            numerator=payload["numerator"],
            denominator=payload["denominator"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PairedCostRecord:
    """Costs on a (task, seed) solved by both variants. Unknown stays unknown."""

    task_id: str
    seed: int
    variant_a: SystemVariant
    variant_b: SystemVariant
    cost_a: ObservedQuantity
    cost_b: ObservedQuantity

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _require_hex64("task_id", self.task_id))
        object.__setattr__(self, "seed", _require_nonneg_int("seed", self.seed))
        object.__setattr__(
            self, "variant_a", _require_enum(SystemVariant, self.variant_a, "variant_a")
        )
        object.__setattr__(
            self, "variant_b", _require_enum(SystemVariant, self.variant_b, "variant_b")
        )
        if self.variant_a.value >= self.variant_b.value:
            raise EvaluationError("paired variants must be strictly ordered")
        if not isinstance(self.cost_a, ObservedQuantity) or self.cost_a.unit != "microusd":
            raise EvaluationError("cost_a must be a microusd ObservedQuantity")
        if not isinstance(self.cost_b, ObservedQuantity) or self.cost_b.unit != "microusd":
            raise EvaluationError("cost_b must be a microusd ObservedQuantity")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cost_a": self.cost_a.to_dict(),
            "cost_b": self.cost_b.to_dict(),
            "seed": self.seed,
            "task_id": self.task_id,
            "variant_a": self.variant_a.value,
            "variant_b": self.variant_b.value,
        }

    @classmethod
    def from_dict(cls, data: object) -> "PairedCostRecord":
        payload = _mapping("PairedCostRecord", data)
        allowed = {
            "cost_a",
            "cost_b",
            "seed",
            "task_id",
            "variant_a",
            "variant_b",
        }
        _reject_extra(payload, allowed, "PairedCostRecord")
        _require_keys(payload, allowed, "PairedCostRecord")
        return cls(
            task_id=payload["task_id"],
            seed=payload["seed"],
            variant_a=payload["variant_a"],
            variant_b=payload["variant_b"],
            cost_a=ObservedQuantity.from_dict(payload["cost_a"]),
            cost_b=ObservedQuantity.from_dict(payload["cost_b"]),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationSummary:
    """Deterministic comparison summary. Replay is labelled descriptive."""

    comparison_id: str
    task_ids: tuple[str, ...]
    variants: tuple[SystemVariant, ...]
    n_tasks: int
    n_runs: int
    causal_eligible: bool
    metrics_label: str
    solve_at_budget: tuple[tuple[str, RationalMetric], ...]
    cost_per_verified_solve: tuple[tuple[str, RationalMetric], ...]
    verified_error_count: tuple[tuple[str, int], ...]
    verified_error_rate: tuple[tuple[str, RationalMetric], ...]
    total_cost_microusd: tuple[tuple[str, dict[str, Any]], ...]
    total_wall_ms: tuple[tuple[str, dict[str, Any]], ...]
    total_input_tokens: tuple[tuple[str, dict[str, Any]], ...]
    total_output_tokens: tuple[tuple[str, dict[str, Any]], ...]
    total_tool_cpu_ms: tuple[tuple[str, dict[str, Any]], ...]
    total_tool_gpu_ms: tuple[tuple[str, dict[str, Any]], ...]
    overhead_wall_ms: tuple[tuple[str, dict[str, Any]], ...]
    paired_costs: tuple[PairedCostRecord, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "comparison_id", _require_hex64("comparison_id", self.comparison_id)
        )
        object.__setattr__(self, "n_tasks", _require_nonneg_int("n_tasks", self.n_tasks))
        object.__setattr__(self, "n_runs", _require_nonneg_int("n_runs", self.n_runs))
        if not isinstance(self.causal_eligible, bool):
            raise EvaluationError("causal_eligible must be a bool")
        if self.metrics_label not in {"causal", "descriptive"}:
            raise EvaluationError("metrics_label must be causal or descriptive")
        if self.causal_eligible and self.metrics_label != "causal":
            raise EvaluationError("live causal summaries must be labelled causal")
        if not self.causal_eligible and self.metrics_label != "descriptive":
            raise EvaluationError("non-causal summaries must be labelled descriptive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "causal_eligible": self.causal_eligible,
            "comparison_id": self.comparison_id,
            "cost_per_verified_solve": _metrics_to_dict(self.cost_per_verified_solve),
            "metrics_label": self.metrics_label,
            "n_runs": self.n_runs,
            "n_tasks": self.n_tasks,
            "overhead_wall_ms": _qty_map_to_dict(self.overhead_wall_ms),
            "paired_costs": [item.to_dict() for item in self.paired_costs],
            "solve_at_budget": _metrics_to_dict(self.solve_at_budget),
            "task_ids": list(self.task_ids),
            "total_cost_microusd": _qty_map_to_dict(self.total_cost_microusd),
            "total_input_tokens": _qty_map_to_dict(self.total_input_tokens),
            "total_output_tokens": _qty_map_to_dict(self.total_output_tokens),
            "total_tool_cpu_ms": _qty_map_to_dict(self.total_tool_cpu_ms),
            "total_tool_gpu_ms": _qty_map_to_dict(self.total_tool_gpu_ms),
            "total_wall_ms": _qty_map_to_dict(self.total_wall_ms),
            "variants": [item.value for item in self.variants],
            "verified_error_count": [
                {"count": count, "variant": variant}
                for variant, count in self.verified_error_count
            ],
            "verified_error_rate": _metrics_to_dict(self.verified_error_rate),
        }

    @classmethod
    def from_dict(cls, data: object) -> "EvaluationSummary":
        payload = _mapping("EvaluationSummary", data)
        allowed = {
            "causal_eligible",
            "comparison_id",
            "cost_per_verified_solve",
            "metrics_label",
            "n_runs",
            "n_tasks",
            "overhead_wall_ms",
            "paired_costs",
            "solve_at_budget",
            "task_ids",
            "total_cost_microusd",
            "total_input_tokens",
            "total_output_tokens",
            "total_tool_cpu_ms",
            "total_tool_gpu_ms",
            "total_wall_ms",
            "variants",
            "verified_error_count",
            "verified_error_rate",
        }
        _reject_extra(payload, allowed, "EvaluationSummary")
        _require_keys(payload, allowed, "EvaluationSummary")
        variants = tuple(
            _require_enum(SystemVariant, item, "variants")
            for item in _require_list(payload["variants"], "variants")
        )
        return cls(
            comparison_id=payload["comparison_id"],
            task_ids=tuple(
                _require_hex64("task_ids", item)
                for item in _require_list(payload["task_ids"], "task_ids")
            ),
            variants=variants,
            n_tasks=payload["n_tasks"],
            n_runs=payload["n_runs"],
            causal_eligible=payload["causal_eligible"],
            metrics_label=payload["metrics_label"],
            solve_at_budget=_metrics_from_dict(payload["solve_at_budget"]),
            cost_per_verified_solve=_metrics_from_dict(payload["cost_per_verified_solve"]),
            verified_error_count=_count_from_dict(payload["verified_error_count"]),
            verified_error_rate=_metrics_from_dict(payload["verified_error_rate"]),
            total_cost_microusd=_qty_map_from_dict(payload["total_cost_microusd"]),
            total_wall_ms=_qty_map_from_dict(payload["total_wall_ms"]),
            total_input_tokens=_qty_map_from_dict(payload["total_input_tokens"]),
            total_output_tokens=_qty_map_from_dict(payload["total_output_tokens"]),
            total_tool_cpu_ms=_qty_map_from_dict(payload["total_tool_cpu_ms"]),
            total_tool_gpu_ms=_qty_map_from_dict(payload["total_tool_gpu_ms"]),
            overhead_wall_ms=_qty_map_from_dict(payload["overhead_wall_ms"]),
            paired_costs=tuple(
                PairedCostRecord.from_dict(item)
                for item in _require_list(payload["paired_costs"], "paired_costs")
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ShadowContinuationRecord:
    """Fixed-budget continuation of a preselected pause. Censored is not a solve."""

    record_id: str
    task_id: str
    variant: SystemVariant
    seed: int
    checkpoint_id: str
    original_outcome: RunOutcome
    added_budget_microusd: int
    added_usage: UsageBreakdown
    started_at: str
    ended_at: str
    stop: ShadowStop
    preselected: bool = True
    acceptance_id: str | None = None

    def __post_init__(self) -> None:
        if self.preselected is not True:
            raise EvaluationError("shadow continuation requires preselection")
        object.__setattr__(self, "task_id", _require_hex64("task_id", self.task_id))
        object.__setattr__(
            self, "variant", _require_enum(SystemVariant, self.variant, "variant")
        )
        object.__setattr__(self, "seed", _require_nonneg_int("seed", self.seed))
        object.__setattr__(
            self, "checkpoint_id", _require_hex64("checkpoint_id", self.checkpoint_id)
        )
        object.__setattr__(
            self,
            "original_outcome",
            _require_enum(RunOutcome, self.original_outcome, "original_outcome"),
        )
        if self.original_outcome.value not in _PAUSE_OUTCOMES:
            raise EvaluationError("original_outcome must be a pause outcome")
        object.__setattr__(
            self,
            "added_budget_microusd",
            _require_positive_int("added_budget_microusd", self.added_budget_microusd),
        )
        if not isinstance(self.added_usage, UsageBreakdown):
            raise EvaluationError("added_usage must be a UsageBreakdown")
        started = _parse_utc("started_at", self.started_at)
        ended = _parse_utc("ended_at", self.ended_at)
        if ended < started:
            raise EvaluationError("ended_at must be >= started_at")
        object.__setattr__(self, "stop", _require_enum(ShadowStop, self.stop, "stop"))
        if self.stop is ShadowStop.COMPLETED_SOLVED:
            object.__setattr__(
                self,
                "acceptance_id",
                _require_hex64("acceptance_id", self.acceptance_id),
            )
        elif self.acceptance_id is not None:
            raise EvaluationError(
                "acceptance_id is only allowed for COMPLETED_SOLVED shadow stops"
            )
        expected = _shadow_id(
            task_id=self.task_id,
            variant=self.variant,
            seed=self.seed,
            checkpoint_id=self.checkpoint_id,
            original_outcome=self.original_outcome,
            added_budget_microusd=self.added_budget_microusd,
            added_usage=self.added_usage,
            started_at=self.started_at,
            ended_at=self.ended_at,
            stop=self.stop,
            acceptance_id=self.acceptance_id,
        )
        object.__setattr__(self, "record_id", _require_hex64("record_id", self.record_id))
        if self.record_id != expected:
            raise EvaluationError("record_id does not match shadow semantics")

    @classmethod
    def build(cls, **kwargs: Any) -> "ShadowContinuationRecord":
        payload = dict(kwargs)
        payload["preselected"] = payload.get("preselected", True)
        variant = _require_enum(SystemVariant, payload.get("variant"), "variant")
        stop = _require_enum(ShadowStop, payload.get("stop"), "stop")
        seed = _require_nonneg_int("seed", payload.get("seed"))
        added = _require_positive_int(
            "added_budget_microusd", payload.get("added_budget_microusd")
        )
        task_id = _require_hex64("task_id", payload.get("task_id"))
        checkpoint_id = _require_hex64("checkpoint_id", payload.get("checkpoint_id"))
        original_outcome = _require_enum(
            RunOutcome, payload.get("original_outcome"), "original_outcome"
        )
        added_usage = payload.get("added_usage")
        if not isinstance(added_usage, UsageBreakdown):
            raise EvaluationError("added_usage must be a UsageBreakdown")
        started_at = _require_nonempty("started_at", payload.get("started_at"))
        ended_at = _require_nonempty("ended_at", payload.get("ended_at"))
        acceptance_id = payload.get("acceptance_id")
        if stop is ShadowStop.COMPLETED_SOLVED:
            acceptance_id = _require_hex64("acceptance_id", acceptance_id)
        elif acceptance_id is not None:
            raise EvaluationError(
                "acceptance_id is only allowed for COMPLETED_SOLVED shadow stops"
            )
        payload["record_id"] = _shadow_id(
            task_id=task_id,
            variant=variant,
            seed=seed,
            checkpoint_id=checkpoint_id,
            original_outcome=original_outcome,
            added_budget_microusd=added,
            added_usage=added_usage,
            started_at=started_at,
            ended_at=ended_at,
            stop=stop,
            acceptance_id=acceptance_id,
        )
        payload["variant"] = variant
        payload["stop"] = stop
        payload["seed"] = seed
        payload["added_budget_microusd"] = added
        payload["checkpoint_id"] = checkpoint_id
        payload["original_outcome"] = original_outcome
        payload["acceptance_id"] = acceptance_id
        return cls(**payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "acceptance_id": self.acceptance_id,
            "added_budget_microusd": self.added_budget_microusd,
            "added_usage": self.added_usage.to_dict(),
            "checkpoint_id": self.checkpoint_id,
            "ended_at": self.ended_at,
            "original_outcome": self.original_outcome.value,
            "preselected": self.preselected,
            "record_id": self.record_id,
            "seed": self.seed,
            "started_at": self.started_at,
            "stop": self.stop.value,
            "task_id": self.task_id,
            "variant": self.variant.value,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ShadowContinuationRecord":
        payload = _mapping("ShadowContinuationRecord", data)
        allowed = {
            "acceptance_id",
            "added_budget_microusd",
            "added_usage",
            "checkpoint_id",
            "ended_at",
            "original_outcome",
            "preselected",
            "record_id",
            "seed",
            "started_at",
            "stop",
            "task_id",
            "variant",
        }
        _reject_extra(payload, allowed, "ShadowContinuationRecord")
        _require_keys(payload, allowed, "ShadowContinuationRecord")
        return cls(
            record_id=payload["record_id"],
            task_id=payload["task_id"],
            variant=payload["variant"],
            seed=payload["seed"],
            checkpoint_id=payload["checkpoint_id"],
            original_outcome=payload["original_outcome"],
            added_budget_microusd=payload["added_budget_microusd"],
            added_usage=UsageBreakdown.from_dict(payload["added_usage"]),
            started_at=payload["started_at"],
            ended_at=payload["ended_at"],
            stop=payload["stop"],
            preselected=payload["preselected"],
            acceptance_id=payload["acceptance_id"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ShadowSummary:
    """Conservative identification interval. Censored is not a successful stop."""

    n_records: int
    n_solved: int
    n_not_solved: int
    n_censored: int
    added_budget_microusd: int
    solve_lower: RationalMetric
    solve_upper: RationalMetric
    censored_counted_as_success: bool = False

    def __post_init__(self) -> None:
        for name in ("n_records", "n_solved", "n_not_solved", "n_censored"):
            object.__setattr__(self, name, _require_nonneg_int(name, getattr(self, name)))
        object.__setattr__(
            self,
            "added_budget_microusd",
            _require_positive_int("added_budget_microusd", self.added_budget_microusd),
        )
        if self.n_solved + self.n_not_solved + self.n_censored != self.n_records:
            raise EvaluationError("shadow counts must sum to n_records")
        if self.censored_counted_as_success is not False:
            raise EvaluationError("censored must not be counted as a successful stop")
        if not isinstance(self.solve_lower, RationalMetric):
            raise EvaluationError("solve_lower must be a RationalMetric")
        if not isinstance(self.solve_upper, RationalMetric):
            raise EvaluationError("solve_upper must be a RationalMetric")

    def to_dict(self) -> dict[str, Any]:
        return {
            "added_budget_microusd": self.added_budget_microusd,
            "censored_counted_as_success": self.censored_counted_as_success,
            "n_censored": self.n_censored,
            "n_not_solved": self.n_not_solved,
            "n_records": self.n_records,
            "n_solved": self.n_solved,
            "solve_lower": self.solve_lower.to_dict(),
            "solve_upper": self.solve_upper.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: object) -> "ShadowSummary":
        payload = _mapping("ShadowSummary", data)
        allowed = {
            "added_budget_microusd",
            "censored_counted_as_success",
            "n_censored",
            "n_not_solved",
            "n_records",
            "n_solved",
            "solve_lower",
            "solve_upper",
        }
        _reject_extra(payload, allowed, "ShadowSummary")
        _require_keys(payload, allowed, "ShadowSummary")
        return cls(
            n_records=payload["n_records"],
            n_solved=payload["n_solved"],
            n_not_solved=payload["n_not_solved"],
            n_censored=payload["n_censored"],
            added_budget_microusd=payload["added_budget_microusd"],
            solve_lower=RationalMetric.from_dict(payload["solve_lower"]),
            solve_upper=RationalMetric.from_dict(payload["solve_upper"]),
            censored_counted_as_success=payload["censored_counted_as_success"],
        )


def validate_dataset(tasks: object) -> None:
    """Reject duplication, family/split leakage, missing audit, and future hashes."""
    items = _require_task_seq(tasks)
    seen_ids: set[str] = set()
    seen_identity: dict[tuple[str, str, str], str] = {}
    family_split: dict[str, DatasetSplit] = {}
    for task in items:
        if task.future_derived:
            raise EvaluationError(f"task {task.task_id} has future-derived hashes")
        if not task.no_future_info_audit_hash:
            raise EvaluationError(f"task {task.task_id} is missing no-future-info audit")
        if task.task_id in seen_ids:
            raise EvaluationError(f"duplicate task_id {task.task_id}")
        seen_ids.add(task.task_id)
        identity = (task.family, task.initial_state_hash, task.cutoff_hash)
        previous = seen_identity.get(identity)
        if previous is not None:
            raise EvaluationError(
                f"duplicate family/initial-state/cutoff for {task.family}"
            )
        seen_identity[identity] = task.task_id
        existing_split = family_split.get(task.family)
        if existing_split is not None and existing_split is not task.split:
            raise EvaluationError(
                f"theorem-family split leakage for family {task.family!r}"
            )
        family_split[task.family] = task.split


def summarize_runs(
    tasks: object,
    runs: object,
    comparison: ComparisonContract,
) -> EvaluationSummary:
    """Build a deterministic summary. Failures stay in every denominator."""
    if not isinstance(comparison, ComparisonContract):
        raise EvaluationError("summarize_runs requires a ComparisonContract")
    task_list = _require_task_seq(tasks)
    validate_dataset(task_list)
    run_list = _require_run_seq(runs)
    task_by_id = {task.task_id: task for task in task_list}
    if len(task_by_id) != len(task_list):
        raise EvaluationError("duplicate task_id")
    for task in task_list:
        _assert_task_matches_comparison(task, comparison)
    seen_keys: set[tuple[str, str, int]] = set()
    by_variant: dict[SystemVariant, list[EvaluationRun]] = {
        variant: [] for variant in comparison.variants
    }
    modes: set[RunMode] = set()
    for run in run_list:
        if run.task_id not in task_by_id:
            raise EvaluationError(f"run {run.run_id} references unknown task")
        if run.variant not in comparison.variants:
            raise EvaluationError(f"run {run.run_id} variant is not in the comparison")
        key = (run.task_id, run.variant.value, run.seed)
        if key in seen_keys:
            raise EvaluationError(
                "duplicate task/variant/seed run "
                f"{run.task_id}/{run.variant.value}/{run.seed}"
            )
        seen_keys.add(key)
        by_variant[run.variant].append(run)
        modes.add(run.run_mode)
    seeds = {run.seed for run in run_list}
    if not seeds:
        seeds = {0}
    expected_pairs = {
        (task.task_id, seed) for task in task_list for seed in seeds
    }
    for variant in comparison.variants:
        present = {(item.task_id, item.seed) for item in by_variant[variant]}
        if present != expected_pairs:
            raise EvaluationError(
                f"variant {variant.value} is missing aligned task/seed runs"
            )
    causal = modes == {RunMode.LIVE_BRANCH}
    label = "causal" if causal else "descriptive"
    solve_at_budget = []
    cost_per_verified = []
    error_counts = []
    error_rates = []
    totals: dict[str, list[tuple[str, dict[str, Any]]]] = {
        "cost": [],
        "wall": [],
        "input": [],
        "output": [],
        "tool_cpu": [],
        "tool_gpu": [],
        "overhead": [],
    }
    for variant in comparison.variants:
        group = by_variant[variant]
        n = len(group)
        accepted = [item for item in group if _is_accepted_resolution(item)]
        solve_at_budget.append(
            (variant.value, RationalMetric.observed(len(accepted), n))
        )
        costs = [item.usage.dimension_total("cost_microusd") for item in group]
        total_cost = _sum_quantities(costs, "microusd")
        if total_cost.status is ObservationStatus.UNKNOWN:
            cost_per_verified.append((variant.value, RationalMetric.unknown()))
        elif len(accepted) == 0:
            cost_per_verified.append(
                (
                    variant.value,
                    RationalMetric.undefined(
                        numerator=total_cost.value, denominator=0
                    ),
                )
            )
        else:
            cost_per_verified.append(
                (
                    variant.value,
                    RationalMetric.observed(int(total_cost.value), len(accepted)),
                )
            )
        errors = sum(1 for item in group if item.verified_error)
        error_counts.append((variant.value, errors))
        error_rates.append((variant.value, RationalMetric.observed(errors, n)))
        totals["cost"].append((variant.value, total_cost.to_dict()))
        totals["wall"].append(
            (
                variant.value,
                _sum_quantities(
                    [item.usage.dimension_total("wall_ms") for item in group], "ms"
                ).to_dict(),
            )
        )
        totals["input"].append(
            (
                variant.value,
                _sum_quantities(
                    [item.usage.dimension_total("input_tokens") for item in group],
                    "tokens",
                ).to_dict(),
            )
        )
        totals["output"].append(
            (
                variant.value,
                _sum_quantities(
                    [item.usage.dimension_total("output_tokens") for item in group],
                    "tokens",
                ).to_dict(),
            )
        )
        totals["tool_cpu"].append(
            (
                variant.value,
                _sum_quantities(
                    [item.usage.dimension_total("tool_cpu_ms") for item in group], "ms"
                ).to_dict(),
            )
        )
        totals["tool_gpu"].append(
            (
                variant.value,
                _sum_quantities(
                    [item.usage.dimension_total("tool_gpu_ms") for item in group], "ms"
                ).to_dict(),
            )
        )
        totals["overhead"].append(
            (
                variant.value,
                _sum_quantities(
                    [item.usage.overhead_wall_ms() for item in group], "ms"
                ).to_dict(),
            )
        )
    paired = _paired_costs(by_variant, comparison.variants)
    task_ids = tuple(sorted(task.task_id for task in task_list))
    return EvaluationSummary(
        comparison_id=comparison.comparison_id,
        task_ids=task_ids,
        variants=comparison.variants,
        n_tasks=len(task_list),
        n_runs=len(run_list),
        causal_eligible=causal,
        metrics_label=label,
        solve_at_budget=tuple(solve_at_budget),
        cost_per_verified_solve=tuple(cost_per_verified),
        verified_error_count=tuple(error_counts),
        verified_error_rate=tuple(error_rates),
        total_cost_microusd=tuple(totals["cost"]),
        total_wall_ms=tuple(totals["wall"]),
        total_input_tokens=tuple(totals["input"]),
        total_output_tokens=tuple(totals["output"]),
        total_tool_cpu_ms=tuple(totals["tool_cpu"]),
        total_tool_gpu_ms=tuple(totals["tool_gpu"]),
        overhead_wall_ms=tuple(totals["overhead"]),
        paired_costs=paired,
    )


def summarize_shadow_continuations(records: object) -> ShadowSummary:
    """Conservative lower/upper identification bounds. Censored is not a solve."""
    items = _require_shadow_seq(records)
    if not items:
        raise EvaluationError("shadow continuation records must be nonempty")
    added = items[0].added_budget_microusd
    seen: set[tuple[str, str, int]] = set()
    n_solved = n_not = n_censored = 0
    for item in items:
        if item.added_budget_microusd != added:
            raise EvaluationError("shadow continuations must share a fixed added budget")
        key = (item.task_id, item.variant.value, item.seed)
        if key in seen:
            raise EvaluationError("duplicate shadow continuation record")
        seen.add(key)
        if item.stop is ShadowStop.COMPLETED_SOLVED:
            n_solved += 1
        elif item.stop is ShadowStop.COMPLETED_NOT_SOLVED:
            n_not += 1
        else:
            n_censored += 1
    n = len(items)
    lower = RationalMetric.observed(n_solved, n)
    upper = RationalMetric.observed(n_solved + n_censored, n)
    return ShadowSummary(
        n_records=n,
        n_solved=n_solved,
        n_not_solved=n_not,
        n_censored=n_censored,
        added_budget_microusd=added,
        solve_lower=lower,
        solve_upper=upper,
        censored_counted_as_success=False,
    )


def export_evaluation_json(
    summary: EvaluationSummary,
    runs: object = (),
    shadow: ShadowSummary | None = None,
) -> str:
    """Canonical JSON. Replay/non-causal metrics are labelled descriptive."""
    if not isinstance(summary, EvaluationSummary):
        raise EvaluationError("export_evaluation_json requires an EvaluationSummary")
    run_list = _require_run_seq(runs) if runs else ()
    payload: dict[str, Any] = {
        "causal_eligible": summary.causal_eligible,
        "kind": "evaluation_summary",
        "metrics_label": summary.metrics_label,
        "replay_metrics": "descriptive" if not summary.causal_eligible else "not_applicable",
        "runs": [item.to_dict() for item in sorted(run_list, key=lambda row: row.run_id)],
        "summary": summary.to_dict(),
    }
    if shadow is not None:
        if not isinstance(shadow, ShadowSummary):
            raise EvaluationError("shadow must be a ShadowSummary")
        payload["shadow"] = shadow.to_dict()
    return canonical_json(payload)


def export_runs_csv(runs: object) -> str:
    """Stable-column CSV. Unknown values are empty, never zero-filled."""
    run_list = sorted(_require_run_seq(runs), key=lambda row: row.run_id)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_CSV_COLUMNS)
    for run in run_list:
        cost = run.usage.dimension_total("cost_microusd")
        wall = run.usage.dimension_total("wall_ms")
        inputs = run.usage.dimension_total("input_tokens")
        outputs = run.usage.dimension_total("output_tokens")
        cpu = run.usage.dimension_total("tool_cpu_ms")
        gpu = run.usage.dimension_total("tool_gpu_ms")
        overhead = run.usage.overhead_wall_ms()
        writer.writerow(
            [
                run.run_id,
                run.task_id,
                run.variant.value,
                str(run.seed),
                run.outcome.value,
                "" if run.acceptance_id is None else run.acceptance_id,
                run.run_mode.value,
                "true" if run.verified_error else "false",
                run.started_at,
                run.ended_at,
                *_csv_quantity(cost),
                *_csv_quantity(wall),
                *_csv_quantity(inputs),
                *_csv_quantity(outputs),
                *_csv_quantity(cpu),
                *_csv_quantity(gpu),
                *_csv_quantity(overhead),
            ]
        )
    return buffer.getvalue()


def _paired_costs(
    by_variant: Mapping[SystemVariant, list[EvaluationRun]],
    variants: tuple[SystemVariant, ...],
) -> tuple[PairedCostRecord, ...]:
    solved_index: dict[SystemVariant, dict[tuple[str, int], EvaluationRun]] = {}
    for variant in variants:
        solved_index[variant] = {
            (item.task_id, item.seed): item
            for item in by_variant[variant]
            if _is_accepted_resolution(item)
        }
    records: list[PairedCostRecord] = []
    ordered = sorted(variants, key=lambda item: item.value)
    for i, left in enumerate(ordered):
        for right in ordered[i + 1 :]:
            common = sorted(set(solved_index[left]) & set(solved_index[right]))
            for key in common:
                run_a = solved_index[left][key]
                run_b = solved_index[right][key]
                records.append(
                    PairedCostRecord(
                        task_id=key[0],
                        seed=key[1],
                        variant_a=left,
                        variant_b=right,
                        cost_a=run_a.usage.dimension_total("cost_microusd"),
                        cost_b=run_b.usage.dimension_total("cost_microusd"),
                    )
                )
    return tuple(records)


def _assert_task_matches_comparison(task: TaskSpec, comparison: ComparisonContract) -> None:
    if task.model_config_hash != comparison.model_config_hash:
        raise EvaluationError("task model config is not equal across the comparison")
    if task.tool_config_hash != comparison.tool_config_hash:
        raise EvaluationError("task tool config is not equal across the comparison")
    if task.context_config_hash != comparison.context_config_hash:
        raise EvaluationError("task context config is not equal across the comparison")
    if task.verifier_config_hash != comparison.verifier_config_hash:
        raise EvaluationError("task verifier config is not equal across the comparison")


def _task_id(
    *,
    family: str,
    split: DatasetSplit,
    initial_state_hash: str,
    cutoff_hash: str,
    model_config_hash: str,
    tool_config_hash: str,
    context_config_hash: str,
    verifier_config_hash: str,
    budget_config_hash: str,
    no_future_info_audit_hash: str,
    future_derived: bool,
) -> str:
    return stable_id(
        "eval-task",
        _ID_VERSION,
        family,
        split.value,
        initial_state_hash,
        cutoff_hash,
        model_config_hash,
        tool_config_hash,
        context_config_hash,
        verifier_config_hash,
        budget_config_hash,
        no_future_info_audit_hash,
        "future" if future_derived else "cutoff",
    )


def _comparison_id(
    *,
    variants: tuple[SystemVariant, ...],
    model_config_hash: str,
    tool_config_hash: str,
    context_config_hash: str,
    verifier_config_hash: str,
    baseline_commit: str,
    baseline_config_hash: str,
    compute_enabled: bool,
    ablation: AblationFeature,
) -> str:
    return stable_id(
        "eval-comparison",
        _ID_VERSION,
        ",".join(item.value for item in variants),
        model_config_hash,
        tool_config_hash,
        context_config_hash,
        verifier_config_hash,
        baseline_commit,
        baseline_config_hash,
        "compute" if compute_enabled else "no-compute",
        ablation.value,
    )


def _is_accepted_resolution(run: EvaluationRun) -> bool:
    return run.outcome.value in _TERMINAL_ACCEPTED and run.acceptance_id is not None


def _usage_fingerprint(usage: UsageBreakdown) -> str:
    return canonical_json(usage.to_dict())


def _run_id(
    *,
    task_id: str,
    variant: SystemVariant,
    seed: int,
    outcome: RunOutcome,
    run_mode: RunMode,
    verified_error: bool,
    acceptance_id: str | None,
    usage: UsageBreakdown,
    started_at: str,
    ended_at: str,
) -> str:
    return stable_id(
        "eval-run",
        _ID_VERSION,
        task_id,
        variant.value,
        str(seed),
        outcome.value,
        run_mode.value,
        "verified-error" if verified_error else "clean",
        acceptance_id or "no-acceptance",
        _usage_fingerprint(usage),
        started_at,
        ended_at,
    )


def _shadow_id(
    *,
    task_id: str,
    variant: SystemVariant,
    seed: int,
    checkpoint_id: str,
    original_outcome: RunOutcome,
    added_budget_microusd: int,
    added_usage: UsageBreakdown,
    started_at: str,
    ended_at: str,
    stop: ShadowStop,
    acceptance_id: str | None,
) -> str:
    return stable_id(
        "eval-shadow",
        _ID_VERSION,
        task_id,
        variant.value,
        str(seed),
        checkpoint_id,
        original_outcome.value,
        str(added_budget_microusd),
        _usage_fingerprint(added_usage),
        started_at,
        ended_at,
        stop.value,
        acceptance_id or "no-acceptance",
    )


def _sum_quantities(
    values: Iterable[ObservedQuantity], unit: str
) -> ObservedQuantity:
    observed = 0
    for item in values:
        if not isinstance(item, ObservedQuantity):
            raise EvaluationError("usage contributor must be ObservedQuantity")
        if item.unit != unit:
            raise EvaluationError(f"usage unit mismatch: {item.unit!r} != {unit!r}")
        if item.status is ObservationStatus.UNKNOWN:
            return ObservedQuantity(unit=unit, status=ObservationStatus.UNKNOWN, value=None)
        if item.status is not ObservationStatus.OBSERVED or item.value is None:
            return ObservedQuantity(unit=unit, status=ObservationStatus.UNKNOWN, value=None)
        observed += item.value
    return ObservedQuantity(unit=unit, status=ObservationStatus.OBSERVED, value=observed)


def _csv_quantity(quantity: ObservedQuantity) -> tuple[str, str]:
    if quantity.status is ObservationStatus.UNKNOWN or quantity.value is None:
        return quantity.status.value, ""
    return quantity.status.value, str(quantity.value)


def _metrics_to_dict(
    items: tuple[tuple[str, RationalMetric], ...]
) -> list[dict[str, Any]]:
    return [
        {"metric": metric.to_dict(), "variant": variant} for variant, metric in items
    ]


def _metrics_from_dict(data: object) -> tuple[tuple[str, RationalMetric], ...]:
    rows = _require_list(data, "metrics")
    out: list[tuple[str, RationalMetric]] = []
    for item in rows:
        payload = _mapping("metric-row", item)
        _reject_extra(payload, {"metric", "variant"}, "metric-row")
        _require_keys(payload, {"metric", "variant"}, "metric-row")
        out.append(
            (
                _require_nonempty("variant", payload["variant"]),
                RationalMetric.from_dict(payload["metric"]),
            )
        )
    return tuple(out)


def _count_from_dict(data: object) -> tuple[tuple[str, int], ...]:
    rows = _require_list(data, "verified_error_count")
    out: list[tuple[str, int]] = []
    for item in rows:
        payload = _mapping("count-row", item)
        _reject_extra(payload, {"count", "variant"}, "count-row")
        _require_keys(payload, {"count", "variant"}, "count-row")
        out.append(
            (
                _require_nonempty("variant", payload["variant"]),
                _require_nonneg_int("count", payload["count"]),
            )
        )
    return tuple(out)


def _qty_map_to_dict(
    items: tuple[tuple[str, dict[str, Any]], ...]
) -> list[dict[str, Any]]:
    return [{"quantity": quantity, "variant": variant} for variant, quantity in items]


def _qty_map_from_dict(data: object) -> tuple[tuple[str, dict[str, Any]], ...]:
    rows = _require_list(data, "quantity-map")
    out: list[tuple[str, dict[str, Any]]] = []
    for item in rows:
        payload = _mapping("quantity-row", item)
        _reject_extra(payload, {"quantity", "variant"}, "quantity-row")
        _require_keys(payload, {"quantity", "variant"}, "quantity-row")
        quantity = _mapping("quantity", payload["quantity"])
        out.append((_require_nonempty("variant", payload["variant"]), dict(quantity)))
    return tuple(out)


def _require_task_seq(value: object) -> tuple[TaskSpec, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise EvaluationError("tasks must be a sequence of TaskSpec")
    items = []
    for index, item in enumerate(value):
        if not isinstance(item, TaskSpec):
            raise EvaluationError(f"tasks[{index}] must be a TaskSpec")
        items.append(item)
    if not items:
        raise EvaluationError("dataset must contain at least one task")
    return tuple(items)


def _require_run_seq(value: object) -> tuple[EvaluationRun, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise EvaluationError("runs must be a sequence of EvaluationRun")
    items = []
    for index, item in enumerate(value):
        if not isinstance(item, EvaluationRun):
            raise EvaluationError(f"runs[{index}] must be an EvaluationRun")
        items.append(item)
    return tuple(items)


def _require_shadow_seq(value: object) -> tuple[ShadowContinuationRecord, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise EvaluationError("records must be a sequence of ShadowContinuationRecord")
    items = []
    for index, item in enumerate(value):
        if not isinstance(item, ShadowContinuationRecord):
            raise EvaluationError(
                f"records[{index}] must be a ShadowContinuationRecord"
            )
        items.append(item)
    return tuple(items)


def _require_variants(value: object) -> tuple[SystemVariant, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise EvaluationError("variants must be a nonempty sequence")
    items = []
    seen: set[SystemVariant] = set()
    for index, item in enumerate(value):
        variant = _require_enum(SystemVariant, item, f"variants[{index}]")
        if variant in seen:
            raise EvaluationError(f"duplicate variant {variant.value}")
        seen.add(variant)
        items.append(variant)
    if not items:
        raise EvaluationError("variants must be nonempty")
    return tuple(items)


def _require_list(value: object, name: str) -> tuple[Any, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise EvaluationError(f"{name} must be a list")
    return tuple(value)


def _mapping(label: str, data: object) -> Mapping[str, Any]:
    if not isinstance(data, Mapping) or isinstance(data, (str, bytes)):
        raise EvaluationError(f"{label} must be a JSON object")
    return data


def _reject_extra(payload: Mapping[str, Any], allowed: set[str], label: str) -> None:
    extra = set(payload) - allowed
    if extra:
        raise EvaluationError(f"{label} has unknown fields: {sorted(extra)}")


def _require_keys(payload: Mapping[str, Any], required: set[str], label: str) -> None:
    missing = required - set(payload)
    if missing:
        raise EvaluationError(f"{label} missing fields: {sorted(missing)}")


def _require_nonempty(name: str, value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise EvaluationError(f"{name} must be a nonempty str")
    return value


def _require_family(value: object) -> str:
    text = _require_nonempty("family", value)
    if _FAMILY_RE.fullmatch(text) is None:
        raise EvaluationError("family is malformed")
    return text


def _require_hex64(name: str, value: object) -> str:
    text = _require_nonempty(name, value)
    if _HEX64_RE.fullmatch(text) is None:
        raise EvaluationError(f"{name} must be 64-char lowercase hex")
    return text


def _require_nonneg_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvaluationError(f"{name} must be a nonnegative int")
    return value


def _require_positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise EvaluationError(f"{name} must be a positive int")
    return value


def _require_enum(enum_cls: type[Enum], value: object, name: str) -> Any:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            raise EvaluationError(f"{name} has unknown value {value!r}") from None
    raise EvaluationError(f"{name} must be {enum_cls.__name__}")


def _parse_utc(name: str, value: object) -> datetime:
    text = _require_nonempty(name, value)
    parsed = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(parsed)
    except ValueError as exc:
        raise EvaluationError(f"{name} must be ISO-8601 UTC") from exc
    if dt.tzinfo is None or dt.utcoffset() != _ZERO:
        raise EvaluationError(f"{name} must be UTC")
    return dt.astimezone(_UTC)
