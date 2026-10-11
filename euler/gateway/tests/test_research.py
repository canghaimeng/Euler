# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""Unit tests for confirmed dead-end evidence reference validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from euler.core import GlobalMemory
from euler.gateway.research import validate_and_build_extra


def _context(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "EULER_PROJECT_ID": "project-a",
        "EULER_WORKER_ID": "worker-main",
        "EULER_RUN_ID": "run-1",
        "EULER_BRANCH_ID": "branch-1",
        "EULER_SUBGOAL_ID": "subgoal-1",
        "EULER_PROVENANCE": "worker_mcp",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _confirmed(gm: GlobalMemory, evidence_class: str, refs: list[str]):
    return validate_and_build_extra(
        "dead_end",
        {
            "attempt_id": "attempt-target",
            "dead_end_status": "confirmed",
            "evidence_class": evidence_class,
            "evidence_refs": refs,
        },
        project_name="project-a",
        global_memory=gm,
    )


def _record(
    gm: GlobalMemory,
    kind: str,
    *,
    worker: str = "worker-1",
    evidence: str | None = None,
    **extra,
) -> str:
    if evidence is None:
        evidence = "checked derivation" if kind in {
            "conclusion", "example", "counterexample", "proof_attempt"
        } else ""
    return gm.append(
        kind,
        claim=f"{kind} claim",
        evidence=evidence,
        author=worker,
        worker_id=worker,
        **extra,
    )


def test_candidate_dead_end_does_not_require_reference_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _context(monkeypatch)
    extra, _ = validate_and_build_extra(
        "dead_end",
        {
            "attempt_id": "attempt-1",
            "dead_end_status": "candidate",
            "evidence_refs": ["not-a-real-record"],
        },
        project_name="project-a",
    )
    assert extra["dead_end_status"] == "candidate"


def test_all_confirmed_refs_must_resolve_in_project_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _context(monkeypatch)
    gm = GlobalMemory(tmp_path)
    counterexample = _record(gm, "counterexample")

    with pytest.raises(ValueError, match="unknown project global-memory"):
        _confirmed(gm, "explicit_counterexample", [counterexample, "fake-id"])

    extra, _ = _confirmed(gm, "explicit_counterexample", [counterexample])
    assert extra["evidence_refs"] == [counterexample]


def test_explicit_counterexample_requires_counterexample_typed_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _context(monkeypatch)
    gm = GlobalMemory(tmp_path)
    attempt = _record(gm, "proof_attempt")
    with pytest.raises(ValueError, match="counterexample record ref"):
        _confirmed(gm, "explicit_counterexample", [attempt])


def test_failure_synthesis_requires_typed_key_failures_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _context(monkeypatch)
    gm = GlobalMemory(tmp_path)
    ordinary = _record(gm, "obstacle")
    with pytest.raises(ValueError, match="key_failures_summary"):
        _confirmed(gm, "failure_synthesis", [ordinary])

    summary = _record(gm, "obstacle", record_type="key_failures_summary")
    extra, _ = _confirmed(gm, "failure_synthesis", [summary])
    assert extra["evidence_refs"] == [summary]

    candidate = _record(
        gm,
        "dead_end",
        record_type="key_failures_summary",
        dead_end_status="candidate",
    )
    _confirmed(gm, "failure_synthesis", [candidate])
    noncandidate = _record(
        gm,
        "dead_end",
        record_type="key_failures_summary",
        dead_end_status="confirmed",
    )
    with pytest.raises(ValueError, match="candidate dead_end"):
        _confirmed(gm, "failure_synthesis", [noncandidate])


def test_independent_agreement_requires_two_distinct_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _context(monkeypatch)
    gm = GlobalMemory(tmp_path)
    first = _record(gm, "proof_attempt", worker="worker-1")
    same_worker = _record(gm, "obstacle", worker="worker-1")
    second = _record(gm, "proof_attempt", worker="worker-2")

    with pytest.raises(ValueError, match="two distinct worker_id"):
        _confirmed(gm, "independent_agreement", [first, same_worker])
    _confirmed(gm, "independent_agreement", [first, second])


@pytest.mark.parametrize(
    "evidence_class",
    ["logical_contradiction", "unavoidable_missing_assumption"],
)
def test_other_classes_require_supporting_not_plan_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    evidence_class: str,
) -> None:
    _context(monkeypatch)
    gm = GlobalMemory(tmp_path)
    plan = _record(gm, "plan")
    with pytest.raises(ValueError, match="requires at least one"):
        _confirmed(gm, evidence_class, [plan])

    obstacle = _record(gm, "obstacle")
    _confirmed(gm, evidence_class, [plan, obstacle])
