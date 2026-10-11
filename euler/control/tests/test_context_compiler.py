"""Offline tests for the Phase 4 Context Compiler.

Covers Phase 4 §9: determinism, byte-for-byte Fact preservation, required-input
fail-closed, oversize, optional omission, no unrelated leakage, expansion,
cache-key changes, and zero mutation.

Runs standalone (``python -m euler.control.tests.test_context_compiler``).
"""

from __future__ import annotations

import ast
import tempfile
from pathlib import Path

from euler.control.adapters.project import (
    FactSnapshot,
    MemoryRecord,
    ProjectSnapshot,
)
from euler.control.artifacts import ArtifactStore
from euler.control.cache import (
    ContextCacheKey,
    ContextInvalidationReason,
    compare_context_keys,
)
from euler.control.context import (
    ContextBound,
    ContextCompiler,
    ContextLimits,
    ContextRequest,
    ContextTooLarge,
    LimitKind,
    MissingRequiredContext,
)
from euler.control.ids import canonical_json, sha256_hex
from euler.control.contracts import (
    CheckerResult,
    EvidenceArtifact,
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
    QuantifierBinding,
    QuantifierKind,
    TargetKind,
)
from euler.control.failures import ConfidenceClass, FailureKind, FailureRecord

from euler.control.obligations import (
    Obligation,
    Route,
)
from euler.control.schema import EventEnvelope
from euler.control.views import ResearchView

_ROOT = Path(__file__).resolve().parents[1]
_TS = "2026-01-01T00:00:00Z"
_PROOF = "By inspection of the odd-sum identity.\nQED exact."
_FACT_MD = (
    "---\n"
    "fact_id: abcdef0123456789\n"
    "problem_id: toy\n"
    "---\n\n"
    "## statement\n"
    "n is a sum of odd numbers.\n\n"
    "## proof\n"
    f"{_PROOF}\n"
)
_OTHER_MD = (
    "---\n"
    "fact_id: fedcba9876543210\n"
    "---\n\n"
    "## statement\n"
    "unrelated secret lemma\n\n"
    "## proof\n"
    "must not leak\n"
)
_GAP = "close the even-integer case without renaming the quantifier"
_OUTPUT = "return a KERNEL_CHECKED_PROOF of the exact obligation statement"


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__} from {getattr(fn, '__name__', fn)}")


def _hex(*parts: str) -> str:
    return sha256_hex("|".join(parts))


def _key_id(key) -> str:
    for name in ("key_id", "digest", "cache_key"):
        value = getattr(key, name, None)
        if isinstance(value, str) and value:
            return value
    return str(key)


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


def _contract() -> ProblemContract:
    return ProblemContract.build(
        problem_id="odd_sum",
        version=1,
        exact_statement="∀n∈ℕ, n is a sum of odd numbers.",
        scope=_scope(),
        permitted_tools=("lean",),
        external_help_policy="none",
        target_kind=TargetKind.PROOF,
        required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
        completion_checks=("proof checked",),
        budget_policy_ref="default-budget",
    )


def _obligation(contract: ProblemContract, **kwargs) -> Obligation:
    fields = dict(
        contract_id=contract.contract_id,
        exact_statement=contract.exact_statement,
        scope=contract.scope,
        desired_output="proof",
        created_by="researcher",
        version=1,
    )
    fields.update(kwargs)
    return Obligation.build(**fields)


def _evidence(contract: ProblemContract, *, content: str = "ev-bytes", **kwargs) -> EvidenceArtifact:
    fields = dict(
        content_hash=sha256_hex(content.encode("utf-8")),
        producer="worker-1",
        contract_id=contract.contract_id,
        evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
        exact_claim=contract.exact_statement,
        scope=contract.scope,
        environment_hash=_hex("env"),
        created_at=_TS,
        checker_id="checker",
        checker_version="1",
        checker_result=CheckerResult.PASS,
    )
    fields.update(kwargs)
    return EvidenceArtifact.build(**fields)


def _event(event_type: str, payload: dict, key: str) -> EventEnvelope:
    return EventEnvelope.build(
        project_id="proj",
        actor="tester",
        event_type=event_type,
        idempotency_key=key,
        payload=payload,
        timestamp_utc=_TS,
    )


def _fact(fact_id: str, markdown: str, *, predecessors=()) -> FactSnapshot:
    raw = markdown
    return FactSnapshot(
        fact_id=fact_id,
        relative_path=f"fact_graph/facts/{fact_id}.md",
        raw_markdown=raw,
        content_sha256=sha256_hex(raw.encode("utf-8")),
        statement="n is a sum of odd numbers." if fact_id.startswith("abc") else "unrelated",
        predecessors=tuple(predecessors),
        external_refs=(),
    )


def _snapshot(*, extra_memory: bool = True) -> ProjectSnapshot:
    memory = ()
    if extra_memory:
        memory = (
            MemoryRecord(
                kind="plan",
                record_id="mem-secret",
                relative_path="global_memory/plan.jsonl",
                canonical_json='{"id":"mem-secret","claim":"secret plan"}',
            ),
        )
    return ProjectSnapshot(
        active_facts=(
            _fact("abcdef0123456789", _FACT_MD),
            _fact("fedcba9876543210", _OTHER_MD),
        ),
        revoked_fact_ids=("deaddeaddeaddead",),
        memory_records=memory,
        receipts=(),
    )


def _seed_view(contract, obligation, evidence, **extra):
    view = ResearchView()
    seq = 1
    view.apply(seq, _event("problem.contract.created", {"contract": contract.to_dict()}, "c"))
    seq += 1
    view.apply(seq, _event("evidence.registered", {"evidence": evidence.to_dict()}, "e"))
    seq += 1
    view.apply(seq, _event("obligation.created", {"obligation": obligation.to_dict()}, "o"))
    seq += 1
    route = extra.get("route")
    if route is not None:
        view.apply(seq, _event("route.created", {"route": route.to_dict()}, "r"))
        seq += 1
    for index, failure in enumerate(extra.get("failures", ())):
        view.apply(
            seq,
            _event("failure.recorded", {"failure": failure.to_dict()}, f"f{index}"),
        )
        seq += 1
    for index, relation in enumerate(extra.get("relations", ())):
        view.apply(
            seq,
            _event("relation.created", {"relation": relation.to_dict()}, f"rel{index}"),
        )
        seq += 1
    return view


def _request(contract, obligation, **overrides) -> ContextRequest:
    kwargs = dict(
        request_id="req-1",
        contract_id=contract.contract_id,
        obligation_id=obligation.obligation_id,
        action_id=_hex("action"),
        concrete_gap=_GAP,
        output_contract=_OUTPUT,
        tool_environment_hash=_hex("tool-v1"),
        acceptance_policy_hash=_hex("accept-v1"),
        reference_versions_hash=_hex("refs-v1"),
        expand_evidence_content=False,
        limits=ContextLimits.unlimited(),
        required_fact_ids=("abcdef0123456789",),
        required_evidence_ids=(),
    )
    kwargs.update(overrides)
    return ContextRequest(**kwargs)


def _compile(request, view, snapshot, store) -> object:
    return ContextCompiler().compile(request, view, snapshot, store)


# --------------------------------------------------------------------------- #
# core packet                                                                 #
# --------------------------------------------------------------------------- #


def test_packet_preserves_contract_scope_and_fact_proof():
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore(Path(tmp) / "cas")
        contract = _contract()
        evidence_text = "checked proof bytes"
        evidence = _evidence(contract, content=evidence_text)
        store.put_bytes(evidence_text.encode("utf-8"))
        obligation = _obligation(contract)
        view = _seed_view(contract, obligation, evidence)
        snapshot = _snapshot()
        request = _request(
            contract,
            obligation,
            required_evidence_ids=(evidence.artifact_id,),
            expand_evidence_content=True,
        )
        shuffled = request.to_dict()
        shuffled["required_fact_ids"] = list(reversed(list(shuffled["required_fact_ids"])))
        # single required id; rebuild from dict with reordered keys
        reordered = {key: shuffled[key] for key in reversed(list(shuffled))}
        packet_a = _compile(request, view, snapshot, store)
        packet_b = _compile(ContextRequest.from_dict(reordered), view, snapshot, store)
        assert packet_a.rendered_text == packet_b.rendered_text
        assert _key_id(packet_a.cache_key) == _key_id(packet_b.cache_key)
        assert packet_a.manifest.section_order == (
            "CONTRACT",
            "OBLIGATION",
            "ROUTE",
            "FACTS",
            "EVIDENCE",
            "FAILURES",
            "RELATIONS",
            "ACTION",
            "MANIFEST",
        )
        assert isinstance(packet_a.cache_key, ContextCacheKey)
        assert isinstance(packet_a.manifest.included_fact_ids, tuple)
        assert isinstance(packet_a.manifest.omissions, tuple)
        assert contract.exact_statement in packet_a.rendered_text
        assert canonical_json(contract.to_dict()) in packet_a.rendered_text
        assert canonical_json(contract.scope.to_dict()) in packet_a.rendered_text
        assert canonical_json(obligation.to_dict()) in packet_a.rendered_text
        assert canonical_json(evidence.to_dict()) in packet_a.rendered_text
        assert "FORALL" in packet_a.rendered_text
        assert "n is an integer" in packet_a.rendered_text
        assert packet_a.fact_markdown == (_FACT_MD,)
        assert "n is a sum of odd numbers." in packet_a.rendered_text
        assert "abcdef0123456789" in packet_a.rendered_text
        assert "proof_handle:" in packet_a.rendered_text
        assert _PROOF not in packet_a.rendered_text
        expanded = _compile(
            _request(
                contract,
                obligation,
                required_evidence_ids=(evidence.artifact_id,),
                expand_evidence_content=True,
                expand_proof_content=True,
            ),
            view,
            snapshot,
            store,
        )
        assert _FACT_MD in expanded.rendered_text
        assert _PROOF in expanded.rendered_text
        assert packet_a.concrete_gap == _GAP
        assert packet_a.output_contract == _OUTPUT
        assert _GAP in packet_a.rendered_text
        assert _OUTPUT in packet_a.rendered_text
        assert "===== MANIFEST =====" in packet_a.rendered_text
        assert packet_a.cache_key.key_id in packet_a.rendered_text
        assert "abcdef0123456789" in packet_a.rendered_text.split("===== MANIFEST =====", 1)[1]
        assert packet_a.evidence_bytes == (evidence_text.encode("utf-8"),)
        assert "unrelated secret lemma" not in packet_a.rendered_text
        assert "must not leak" not in packet_a.rendered_text
        assert "secret plan" not in packet_a.rendered_text
        assert "mem-secret" not in packet_a.rendered_text
        assert ContextBound(kind=LimitKind.UNLIMITED, value=None).kind is LimitKind.UNLIMITED


def test_required_revoked_missing_and_stale_fail_closed():
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore(Path(tmp) / "cas")
        contract = _contract()
        evidence = _evidence(contract)
        obligation = _obligation(contract)
        view = _seed_view(contract, obligation, evidence)
        snapshot = _snapshot()
        _raises(
            MissingRequiredContext,
            _compile,
            _request(contract, obligation, required_fact_ids=("deaddeaddeaddead",)),
            view,
            snapshot,
            store,
        )
        _raises(
            MissingRequiredContext,
            _compile,
            _request(contract, obligation, required_fact_ids=("missingmissing12",)),
            view,
            snapshot,
            store,
        )
        stale = _evidence(contract, content="stale-ev")
        view.apply(9, _event("evidence.registered", {"evidence": stale.to_dict()}, "stale"))
        view.apply(10, _event("evidence.invalidated", {"artifact_id": stale.artifact_id}, "inv"))
        _raises(
            MissingRequiredContext,
            _compile,
            _request(
                contract,
                obligation,
                required_fact_ids=("abcdef0123456789",),
                required_evidence_ids=(stale.artifact_id,),
            ),
            view,
            snapshot,
            store,
        )
        _raises(
            MissingRequiredContext,
            _compile,
            _request(
                contract,
                obligation,
                required_evidence_ids=(_hex("no-such-evidence"),),
            ),
            view,
            snapshot,
            store,
        )


def test_mandatory_oversize_fails_without_truncation():
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore(Path(tmp) / "cas")
        contract = _contract()
        evidence = _evidence(contract)
        obligation = _obligation(contract)
        view = _seed_view(contract, obligation, evidence)
        snapshot = _snapshot()
        request = _request(
            contract,
            obligation,
            limits=ContextLimits.build(max_chars=12, max_bytes=12),
        )
        try:
            _compile(request, view, snapshot, store)
        except ContextTooLarge as exc:
            assert exc.measured_chars > 12
            assert exc.measured_bytes > 12
            assert contract.contract_id in exc.required_ids
            assert "abcdef0123456789" in exc.required_ids
        else:
            raise AssertionError("expected ContextTooLarge")


def test_optional_failures_are_ranked_capped_and_omitted():
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore(Path(tmp) / "cas")
        contract = _contract()
        evidence = _evidence(contract)
        obligation = _obligation(contract)
        other_scope = _scope(domain="Q")
        linked = FailureRecord.build(
            kind=FailureKind.INFRASTRUCTURE_FAILURE,
            exact_failed_claim="timeout",
            scope=contract.scope,
            confidence=ConfidenceClass.HIGH,
            context_version=1,
            provenance="worker",
            created_at="2026-01-01T00:00:00Z",
            target_obligation_id=obligation.obligation_id,
            contract_id=contract.contract_id,
        )
        scoped = FailureRecord.build(
            kind=FailureKind.MISSING_PREMISE,
            exact_failed_claim="need lemma",
            scope=contract.scope,
            confidence=ConfidenceClass.MEDIUM,
            context_version=1,
            provenance="audit",
            created_at="2026-01-02T00:00:00Z",
            missing_conditions=("lemma",),
        )
        distant = FailureRecord.build(
            kind=FailureKind.BUDGET_EXHAUSTION,
            exact_failed_claim="out of budget",
            scope=other_scope,
            confidence=ConfidenceClass.LOW,
            context_version=1,
            provenance="ledger",
            created_at="2026-01-03T00:00:00Z",
        )
        view = _seed_view(
            contract,
            obligation,
            evidence,
            failures=(distant, scoped, linked),
        )
        snapshot = _snapshot()
        packet = _compile(
            _request(
                contract,
                obligation,
                relevant_failure_ids=(
                    distant.failure_id,
                    scoped.failure_id,
                    linked.failure_id,
                ),
                limits=ContextLimits.build(max_optional_failures=1),
            ),
            view,
            snapshot,
            store,
        )
        assert packet.manifest.included_failure_ids == (linked.failure_id,)
        omitted_ids = {item.item_id: item.reason for item in packet.manifest.omissions}
        assert scoped.failure_id in omitted_ids
        assert distant.failure_id in omitted_ids
        assert omitted_ids[scoped.failure_id] == "optional_cap"
        assert omitted_ids[distant.failure_id] == "scope_mismatch"
        assert linked.failure_id in packet.rendered_text
        assert distant.exact_failed_claim not in packet.rendered_text


def test_expansion_toggle_and_content_hash_tamper():
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore(Path(tmp) / "cas")
        contract = _contract()
        payload = "exact evidence payload"
        evidence = _evidence(contract, content=payload)
        store.put_bytes(payload.encode("utf-8"))
        obligation = _obligation(contract)
        view = _seed_view(contract, obligation, evidence)
        snapshot = _snapshot()
        meta = _compile(
            _request(
                contract,
                obligation,
                required_evidence_ids=(evidence.artifact_id,),
                expand_evidence_content=False,
            ),
            view,
            snapshot,
            store,
        )
        assert meta.evidence_bytes == ()
        assert payload not in meta.rendered_text
        expanded = _compile(
            _request(
                contract,
                obligation,
                required_evidence_ids=(evidence.artifact_id,),
                expand_evidence_content=True,
            ),
            view,
            snapshot,
            store,
        )
        assert expanded.evidence_bytes == (payload.encode("utf-8"),)
        assert payload in expanded.rendered_text
        assert isinstance(meta.cache_key, ContextCacheKey)
        assert _key_id(meta.cache_key) != _key_id(expanded.cache_key)
        reasons = compare_context_keys(meta.cache_key, expanded.cache_key)
        assert ContextInvalidationReason.PACKET_POLICY in reasons
        other = ArtifactStore(Path(tmp) / "cas-empty")
        _raises(
            MissingRequiredContext,
            _compile,
            _request(
                contract,
                obligation,
                required_evidence_ids=(evidence.artifact_id,),
                expand_evidence_content=True,
            ),
            view,
            snapshot,
            other,
        )


def test_cache_key_changes_with_inputs_not_order():
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore(Path(tmp) / "cas")
        contract = _contract()
        evidence = _evidence(contract)
        obligation = _obligation(contract)
        view = _seed_view(contract, obligation, evidence)
        snapshot = _snapshot()
        base = _compile(_request(contract, obligation), view, snapshot, store)
        same = _compile(
            ContextRequest.from_dict(
                {key: value for key, value in reversed(list(_request(contract, obligation).to_dict().items()))}
            ),
            view,
            snapshot,
            store,
        )
        assert _key_id(base.cache_key) == _key_id(same.cache_key)
        changed = []
        for field, value in (
            ("tool_environment_hash", _hex("tool-v2")),
            ("acceptance_policy_hash", _hex("accept-v2")),
            ("reference_versions_hash", _hex("refs-v2")),
            ("output_contract", "different output contract text"),
            ("limits", ContextLimits.build(max_optional_failures=0)),
        ):
            packet = _compile(
                _request(contract, obligation, **{field: value}),
                view,
                snapshot,
                store,
            )
            changed.append(_key_id(packet.cache_key))
            assert _key_id(packet.cache_key) != _key_id(base.cache_key), field
        tool_packet = _compile(
            _request(contract, obligation, tool_environment_hash=_hex("tool-v2")),
            view,
            snapshot,
            store,
        )
        reasons = compare_context_keys(base.cache_key, tool_packet.cache_key)
        assert ContextInvalidationReason.TOOL in reasons
        gap_packet = _compile(
            _request(
                contract,
                obligation,
                concrete_gap="a different concrete gap that remains exact text",
            ),
            view,
            snapshot,
            store,
        )
        gap_reasons = compare_context_keys(base.cache_key, gap_packet.cache_key)
        assert ContextInvalidationReason.CONCRETE_GAP in gap_reasons
        frontier_before = view.frontier()
        _compile(_request(contract, obligation), view, snapshot, store)
        assert view.frontier() == frontier_before
        assert snapshot.memory_records[0].record_id == "mem-secret"


def test_predecessor_closure_order_and_cycles():
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore(Path(tmp) / "cas")
        contract = _contract()
        evidence = _evidence(contract)
        obligation = _obligation(contract)
        view = _seed_view(contract, obligation, evidence)
        root = _fact("aaaaaaaaaaaaaaaa", "ROOT_FACT_MARKDOWN\n")
        mid = _fact(
            "bbbbbbbbbbbbbbbb",
            "MID_FACT_MARKDOWN\n",
            predecessors=("aaaaaaaaaaaaaaaa",),
        )
        leaf = _fact(
            "cccccccccccccccc",
            "LEAF_FACT_MARKDOWN\n",
            predecessors=("bbbbbbbbbbbbbbbb",),
        )
        snapshot = ProjectSnapshot(
            active_facts=(leaf, mid, root, _fact("fedcba9876543210", _OTHER_MD)),
            revoked_fact_ids=("deaddeaddeaddead",),
            memory_records=(),
            receipts=(),
        )
        packet = _compile(
            _request(contract, obligation, required_fact_ids=("cccccccccccccccc",)),
            view,
            snapshot,
            store,
        )
        assert packet.manifest.included_fact_ids == (
            "aaaaaaaaaaaaaaaa",
            "bbbbbbbbbbbbbbbb",
            "cccccccccccccccc",
        )
        assert packet.fact_markdown == (
            "ROOT_FACT_MARKDOWN\n",
            "MID_FACT_MARKDOWN\n",
            "LEAF_FACT_MARKDOWN\n",
        )
        rendered_facts = packet.rendered_text.split("===== FACTS =====", 1)[1]
        assert rendered_facts.find("aaaaaaaaaaaaaaaa") < rendered_facts.find(
            "bbbbbbbbbbbbbbbb"
        ) < rendered_facts.find("cccccccccccccccc")
        assert "ROOT_FACT_MARKDOWN" not in packet.rendered_text
        assert "proof_handle:" in packet.rendered_text
        expanded = _compile(
            _request(
                contract,
                obligation,
                required_fact_ids=("cccccccccccccccc",),
                expand_proof_content=True,
            ),
            view,
            snapshot,
            store,
        )
        rendered_expanded = expanded.rendered_text.split("===== FACTS =====", 1)[1]
        assert rendered_expanded.find("ROOT_FACT_MARKDOWN") < rendered_expanded.find(
            "MID_FACT_MARKDOWN"
        ) < rendered_expanded.find("LEAF_FACT_MARKDOWN")
        assert "unrelated secret lemma" not in packet.rendered_text
        cyclic = ProjectSnapshot(
            active_facts=(
                _fact("aaaaaaaaaaaaaaaa", "A\n", predecessors=("bbbbbbbbbbbbbbbb",)),
                _fact("bbbbbbbbbbbbbbbb", "B\n", predecessors=("aaaaaaaaaaaaaaaa",)),
            ),
            revoked_fact_ids=(),
            memory_records=(),
            receipts=(),
        )
        _raises(
            MissingRequiredContext,
            _compile,
            _request(contract, obligation, required_fact_ids=("aaaaaaaaaaaaaaaa",)),
            view,
            cyclic,
            store,
        )
        dangling = ProjectSnapshot(
            active_facts=(
                _fact("cccccccccccccccc", "C\n", predecessors=("missingmissing12",)),
            ),
            revoked_fact_ids=(),
            memory_records=(),
            receipts=(),
        )
        _raises(
            MissingRequiredContext,
            _compile,
            _request(contract, obligation, required_fact_ids=("cccccccccccccccc",)),
            view,
            dangling,
            store,
        )
        revoked_pred = ProjectSnapshot(
            active_facts=(
                _fact("cccccccccccccccc", "C\n", predecessors=("deaddeaddeaddead",)),
            ),
            revoked_fact_ids=("deaddeaddeaddead",),
            memory_records=(),
            receipts=(),
        )
        _raises(
            MissingRequiredContext,
            _compile,
            _request(contract, obligation, required_fact_ids=("cccccccccccccccc",)),
            view,
            revoked_pred,
            store,
        )


def test_manifest_counts_toward_limits():
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore(Path(tmp) / "cas")
        contract = _contract()
        evidence = _evidence(contract)
        obligation = _obligation(contract)
        linked = FailureRecord.build(
            kind=FailureKind.INFRASTRUCTURE_FAILURE,
            exact_failed_claim="timeout",
            scope=contract.scope,
            confidence=ConfidenceClass.HIGH,
            context_version=1,
            provenance="worker",
            created_at="2026-01-01T00:00:00Z",
            target_obligation_id=obligation.obligation_id,
            contract_id=contract.contract_id,
        )
        view = _seed_view(contract, obligation, evidence, failures=(linked,))
        snapshot = _snapshot()
        base = _compile(
            _request(contract, obligation, auto_blocker_recall=False),
            view,
            snapshot,
            store,
        )
        with_failure = _compile(
            _request(
                contract,
                obligation,
                relevant_failure_ids=(linked.failure_id,),
            ),
            view,
            snapshot,
            store,
        )
        assert len(with_failure.rendered_text) > len(base.rendered_text)
        limited = _compile(
            _request(
                contract,
                obligation,
                relevant_failure_ids=(linked.failure_id,),
                limits=ContextLimits.build(
                    max_chars=len(with_failure.rendered_text) - 1
                ),
            ),
            view,
            snapshot,
            store,
        )
        assert linked.failure_id not in limited.manifest.included_failure_ids
        omitted = {item.item_id: item.reason for item in limited.manifest.omissions}
        assert omitted.get(linked.failure_id) == "over_limit"
        assert limited.manifest.measured_chars == len(limited.rendered_text)
        assert limited.manifest.measured_bytes == len(limited.rendered_utf8())
        assert limited.cache_key.key_id in limited.rendered_text.split(
            "===== MANIFEST =====", 1
        )[1]


def test_mismatched_route_and_zero_mutation():
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore(Path(tmp) / "cas")
        contract = _contract()
        evidence = _evidence(contract)
        obligation = _obligation(contract)
        other = _obligation(contract, exact_statement="other target", version=2)
        route = Route.build(
            target_obligation_id=other.obligation_id,
            mechanism="direct",
            required_obligation_ids=(),
        )
        view = ResearchView()
        view.apply(1, _event("problem.contract.created", {"contract": contract.to_dict()}, "c"))
        view.apply(2, _event("evidence.registered", {"evidence": evidence.to_dict()}, "e"))
        view.apply(3, _event("obligation.created", {"obligation": obligation.to_dict()}, "o"))
        view.apply(4, _event("obligation.created", {"obligation": other.to_dict()}, "o2"))
        view.apply(5, _event("route.created", {"route": route.to_dict()}, "r"))
        snapshot = _snapshot()
        before = (view.frontier(), view.obligations(), view.evidence())
        _raises(
            MissingRequiredContext,
            _compile,
            _request(contract, obligation, route_id=route.route_id),
            view,
            snapshot,
            store,
        )
        assert (view.frontier(), view.obligations(), view.evidence()) == before


def test_production_module_is_read_only():
    forbidden_modules = {
        "euler.gateway",
        "euler.verify",
        "euler.execution",
        "euler.core.factgraph",
        "euler.core.global_memory",
    }
    forbidden_names = {"FactGraph", "fact_submit", "GlobalMemory", "put_bytes"}
    for filename in ("context.py", "recall.py"):
        path = _ROOT / filename
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        imported: set[str] = set()
        named: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                for alias in node.names:
                    named.add(alias.name)
            elif isinstance(node, ast.Attribute):
                named.add(node.attr)
        assert imported.isdisjoint(forbidden_modules), (filename, imported)
        assert named.isdisjoint(forbidden_names), (filename, named & forbidden_names)
        assert "write_text" not in named


def main() -> None:
    tests = [
        test_packet_preserves_contract_scope_and_fact_proof,
        test_required_revoked_missing_and_stale_fail_closed,
        test_mandatory_oversize_fails_without_truncation,
        test_optional_failures_are_ranked_capped_and_omitted,
        test_expansion_toggle_and_content_hash_tamper,
        test_cache_key_changes_with_inputs_not_order,
        test_predecessor_closure_order_and_cycles,
        test_manifest_counts_toward_limits,
        test_mismatched_route_and_zero_mutation,
        test_production_module_is_read_only,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL CONTEXT COMPILER TESTS PASSED")


if __name__ == "__main__":
    main()
