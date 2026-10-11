"""Bounded dependency-interface and blocker recall.

Stdlib only. Automatic historical activation for a worker action goes through
these two channels and then the Context Compiler. Callers must not add an
uncapped extra transcript, global-memory digest, or raw episode dump beside
the compiled packet.

Loop integration (parent owns execution/loop.py and controller.py):
- Before dispatch, call ``recall_for_action`` then compile the resulting
  ``ContextRequest``. Do not inject ``euler.execution.memory_digest`` or other
  historical material into the same prompt.
- Full ancestor proofs stay on the packet as ``fact_markdown`` / proof handles
  and remain available for an explicit expand or a separate inspect action.
- Required statements, assumptions, and proof handles are never truncated.
  Oversize required context raises ``ContextTooLarge`` so the action defers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from euler.control.adapters.project import FactSnapshot, ProjectSnapshot
from euler.control.contracts import MathematicalScope, ValidityStatus
from euler.control.failures import FailureRecord
from euler.control.ids import canonical_json, sha256_hex
from euler.control.obligations import Obligation, Route
from euler.control.views import ResearchView

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?",
    re.DOTALL,
)

__all__ = [
    "LOOP_INTEGRATION",
    "RecallError",
    "MissingDependency",
    "DependencyInterface",
    "BlockerRecord",
    "RecallOmission",
    "RecalledSelection",
    "infer_dependency_roots",
    "validate_predecessor_closure",
    "dependency_interfaces",
    "render_dependency_interface",
    "select_blockers",
    "recall_for_action",
    "compact_blocker_payload",
]


LOOP_INTEGRATION = (
    "Fit every automatic historical activation through ContextCompiler.compile "
    "after recall_for_action. Do not append an extra historical digest, parked "
    "route dump, or raw episode replay to the worker prompt. Explicit inspect "
    "actions may load full proofs via expand_proof_content or fact_markdown."
)


class RecallError(ValueError):
    """Malformed recall input."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RecallOmission:
    item_id: str
    item_type: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "item_id": self.item_id,
            "item_type": self.item_type,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class DependencyInterface:
    """Prompt-facing theorem interface. Full proof is referenced, not inlined."""

    fact_id: str
    statement: str
    assumptions: tuple[str, ...]
    conditions: tuple[str, ...]
    predecessors: tuple[str, ...]
    proof_handle: str
    relative_path: str
    provenance: str
    full_proof_available: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "fact_id": self.fact_id,
            "statement": self.statement,
            "assumptions": list(self.assumptions),
            "conditions": list(self.conditions),
            "predecessors": list(self.predecessors),
            "proof_handle": self.proof_handle,
            "relative_path": self.relative_path,
            "provenance": self.provenance,
            "full_proof_available": self.full_proof_available,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class BlockerRecord:
    failure_id: str
    kind: str
    exact_failed_claim: str
    assumptions: tuple[str, ...]
    witness_refs: tuple[str, ...]
    provenance: str
    route_id: Optional[str]
    target_obligation_id: Optional[str]
    rejected_step: Optional[str]
    reopen_conditions: tuple[str, ...]
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "failure_id": self.failure_id,
            "kind": self.kind,
            "exact_failed_claim": self.exact_failed_claim,
            "assumptions": list(self.assumptions),
            "witness_refs": list(self.witness_refs),
            "provenance": self.provenance,
            "route_id": self.route_id,
            "target_obligation_id": self.target_obligation_id,
            "rejected_step": self.rejected_step,
            "reopen_conditions": list(self.reopen_conditions),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RecalledSelection:
    """IDs to bind into ContextSeed / ContextRequest before compile."""

    required_fact_ids: tuple[str, ...]
    required_evidence_ids: tuple[str, ...]
    relevant_failure_ids: tuple[str, ...]
    relevant_relation_ids: tuple[str, ...]
    interfaces: tuple[DependencyInterface, ...]
    blockers: tuple[BlockerRecord, ...]
    omissions: tuple[RecallOmission, ...]
    loop_integration: str = LOOP_INTEGRATION

    def to_dict(self) -> dict[str, object]:
        return {
            "required_fact_ids": list(self.required_fact_ids),
            "required_evidence_ids": list(self.required_evidence_ids),
            "relevant_failure_ids": list(self.relevant_failure_ids),
            "relevant_relation_ids": list(self.relevant_relation_ids),
            "interfaces": [item.to_dict() for item in self.interfaces],
            "blockers": [item.to_dict() for item in self.blockers],
            "omissions": [item.to_dict() for item in self.omissions],
            "loop_integration": self.loop_integration,
        }


def infer_dependency_roots(
    *,
    snapshot: ProjectSnapshot,
    view: ResearchView,
    obligation: Obligation,
    route: Route | None = None,
    explicit_fact_ids: Sequence[str] = (),
    explicit_evidence_ids: Sequence[str] = (),
) -> tuple[str, ...]:
    """Union explicit roots with facts linked from obligation/route/evidence.

    Predecessor closure is validated separately; this only selects roots.
    """
    if not isinstance(snapshot, ProjectSnapshot):
        raise RecallError("snapshot must be a ProjectSnapshot")
    if not isinstance(view, ResearchView):
        raise RecallError("view must be a ResearchView")
    if not isinstance(obligation, Obligation):
        raise RecallError("obligation must be an Obligation")
    active = {item.fact_id: item for item in snapshot.active_facts}
    by_hash = {item.content_sha256: item.fact_id for item in snapshot.active_facts}
    by_statement = _statement_index(snapshot.active_facts)
    ordered: list[str] = []
    seen: set[str] = set()

    def add(fact_id: object) -> None:
        if not isinstance(fact_id, str) or fact_id == "":
            return
        if fact_id in seen:
            return
        seen.add(fact_id)
        ordered.append(fact_id)

    for fact_id in explicit_fact_ids:
        add(fact_id)

    evidence_ids = list(explicit_evidence_ids)
    evidence_ids.extend(obligation.evidence_refs)
    if route is not None:
        evidence_ids.extend(route.evidence_refs)
        for dep in route.dependencies:
            if dep in active:
                add(dep)
        for required in route.required_obligation_ids:
            child = view.get_obligation(required)
            if child is None:
                continue
            evidence_ids.extend(child.evidence_refs)
            add_matching = by_statement.get(_norm_statement(child.exact_statement))
            if add_matching:
                add(add_matching)

    seen_evidence: set[str] = set()
    for artifact_id in evidence_ids:
        if not isinstance(artifact_id, str) or artifact_id == "" or artifact_id in seen_evidence:
            continue
        seen_evidence.add(artifact_id)
        artifact = view.get_evidence(artifact_id)
        if artifact is None:
            continue
        for digest in (artifact.content_hash, *artifact.input_hashes):
            linked = by_hash.get(digest)
            if linked:
                add(linked)
        matched = by_statement.get(_norm_statement(artifact.exact_claim))
        if matched:
            add(matched)

    matched_obligation = by_statement.get(_norm_statement(obligation.exact_statement))
    if matched_obligation:
        add(matched_obligation)
    return tuple(ordered)


class MissingDependency(RecallError):
    """A required Fact is missing, revoked, cyclic, or dangling."""

    def __init__(self, message: str, *, missing_ids: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.missing_ids = missing_ids


def validate_predecessor_closure(
    roots: Sequence[str],
    snapshot: ProjectSnapshot,
) -> tuple[FactSnapshot, ...]:
    """Walk predecessors; fail closed on missing, revoked, or cyclic Facts."""
    active = {item.fact_id: item for item in snapshot.active_facts}
    revoked = set(snapshot.revoked_fact_ids)
    visiting: set[str] = set()
    done: set[str] = set()
    ordered: list[FactSnapshot] = []

    def visit(fact_id: str) -> None:
        if fact_id in done:
            return
        if fact_id in visiting:
            raise MissingDependency(
                f"required Fact predecessor cycle at {fact_id}",
                missing_ids=(fact_id,),
            )
        if fact_id in revoked:
            raise MissingDependency(
                f"required Fact {fact_id} is revoked",
                missing_ids=(fact_id,),
            )
        fact = active.get(fact_id)
        if fact is None:
            raise MissingDependency(
                f"required Fact {fact_id} is missing or dangling",
                missing_ids=(fact_id,),
            )
        visiting.add(fact_id)
        for pred in fact.predecessors:
            visit(pred)
        visiting.remove(fact_id)
        done.add(fact_id)
        ordered.append(fact)

    seen_roots: set[str] = set()
    for fact_id in roots:
        if fact_id in seen_roots:
            continue
        seen_roots.add(fact_id)
        visit(fact_id)
    return tuple(ordered)


def dependency_interfaces(
    facts: Sequence[FactSnapshot],
    *,
    scope: MathematicalScope | None = None,
) -> tuple[DependencyInterface, ...]:
    assumptions = () if scope is None else tuple(scope.assumptions)
    interfaces: list[DependencyInterface] = []
    for fact in facts:
        statement = fact.statement or _section_body(fact.raw_markdown, "statement")
        fact_assumptions = _frontmatter_assumptions(fact.raw_markdown) or assumptions
        conditions = tuple(fact_assumptions)
        proof_body = _section_body(fact.raw_markdown, "proof")
        handle = fact.content_sha256
        provenance = f"{fact.relative_path}#sha256:{handle}"
        interfaces.append(
            DependencyInterface(
                fact_id=fact.fact_id,
                statement=statement,
                assumptions=tuple(fact_assumptions),
                conditions=conditions,
                predecessors=tuple(fact.predecessors),
                proof_handle=handle,
                relative_path=fact.relative_path,
                provenance=provenance,
                full_proof_available=bool(proof_body) or bool(fact.raw_markdown),
            )
        )
    return tuple(interfaces)


def render_dependency_interface(interface: DependencyInterface) -> str:
    lines = [
        f"FACT {interface.fact_id}",
        f"statement: {interface.statement}",
        "assumptions:",
    ]
    if interface.assumptions:
        lines.extend(f"- {item}" for item in interface.assumptions)
    else:
        lines.append("- (none recorded)")
    if interface.conditions and interface.conditions != interface.assumptions:
        lines.append("conditions:")
        lines.extend(f"- {item}" for item in interface.conditions)
    lines.append("predecessors: " + ",".join(interface.predecessors))
    lines.append(f"proof_handle: {interface.proof_handle}")
    lines.append(f"path: {interface.relative_path}")
    lines.append(f"provenance: {interface.provenance}")
    lines.append("full_proof: available-by-handle")
    return "\n".join(lines)


def select_blockers(
    view: ResearchView,
    obligation: Obligation,
    route: Route | None,
    *,
    explicit_failure_ids: Sequence[str] = (),
    max_optional_failures: int | None = None,
) -> tuple[list[FailureRecord], list[RecallOmission]]:
    """Select scoped blockers by route/mechanism/scope, plus explicit IDs."""
    omissions: list[RecallOmission] = []
    selected: list[FailureRecord] = []
    seen: set[str] = set()
    explicit = tuple(explicit_failure_ids)
    explicit_set = set(explicit)

    def consider(record: FailureRecord, *, required_explicit: bool) -> None:
        if record.failure_id in seen:
            return
        reason = _blocker_inapplicable(record, obligation, route)
        if reason is not None:
            omissions.append(
                RecallOmission(
                    item_id=record.failure_id,
                    item_type="failure",
                    reason=reason,
                )
            )
            seen.add(record.failure_id)
            return
        seen.add(record.failure_id)
        selected.append(record)
        _ = required_explicit

    for failure_id in explicit:
        record = view.get_failure(failure_id)
        if record is None:
            omissions.append(
                RecallOmission(
                    item_id=failure_id, item_type="failure", reason="missing"
                )
            )
            seen.add(failure_id)
            continue
        consider(record, required_explicit=True)

    for record in view.failures():
        if record.failure_id in explicit_set or record.failure_id in seen:
            continue
        if _automatic_blocker_match(record, obligation, route):
            consider(record, required_explicit=False)

    selected.sort(key=lambda item: item.failure_id)
    selected.sort(key=lambda item: item.created_at, reverse=True)
    selected.sort(key=_blocker_rank_key(obligation, route))
    selected, dup_omissions = _dedupe_blockers(selected)
    omissions.extend(dup_omissions)
    if max_optional_failures is not None:
        kept = selected[:max_optional_failures]
        for item in selected[max_optional_failures:]:
            omissions.append(
                RecallOmission(
                    item_id=item.failure_id,
                    item_type="failure",
                    reason="optional_cap",
                )
            )
        selected = kept
    return selected, omissions


def compact_blocker_payload(record: FailureRecord) -> dict[str, object]:
    return BlockerRecord(
        failure_id=record.failure_id,
        kind=record.kind.value,
        exact_failed_claim=record.exact_failed_claim,
        assumptions=tuple(record.scope.assumptions),
        witness_refs=tuple(record.witness_refs),
        provenance=record.provenance,
        route_id=record.route_id,
        target_obligation_id=record.target_obligation_id,
        rejected_step=record.rejected_step,
        reopen_conditions=tuple(record.reopen_conditions),
        created_at=record.created_at,
    ).to_dict()


def recall_for_action(
    *,
    view: ResearchView,
    snapshot: ProjectSnapshot,
    obligation: Obligation,
    route: Route | None = None,
    explicit_fact_ids: Sequence[str] = (),
    explicit_evidence_ids: Sequence[str] = (),
    explicit_failure_ids: Sequence[str] = (),
    explicit_relation_ids: Sequence[str] = (),
    max_optional_failures: int | None = None,
) -> RecalledSelection:
    """Infer dependency interfaces and scoped blockers for one action."""
    roots = infer_dependency_roots(
        snapshot=snapshot,
        view=view,
        obligation=obligation,
        route=route,
        explicit_fact_ids=explicit_fact_ids,
        explicit_evidence_ids=explicit_evidence_ids,
    )
    facts = validate_predecessor_closure(roots, snapshot)
    interfaces = dependency_interfaces(facts, scope=obligation.scope)
    blockers, omissions = select_blockers(
        view,
        obligation,
        route,
        explicit_failure_ids=explicit_failure_ids,
        max_optional_failures=max_optional_failures,
    )
    evidence_ids = _unique_ids(
        list(explicit_evidence_ids)
        + list(obligation.evidence_refs)
        + (list(route.evidence_refs) if route is not None else [])
    )
    valid_evidence: list[str] = []
    for artifact_id in evidence_ids:
        artifact = view.get_evidence(artifact_id)
        if artifact is None:
            continue
        validity = view.evidence_validity(artifact_id) or artifact.validity
        if validity is ValidityStatus.VALID and artifact.validity is ValidityStatus.VALID:
            valid_evidence.append(artifact_id)
    relation_ids = _unique_ids(explicit_relation_ids)
    return RecalledSelection(
        required_fact_ids=tuple(item.fact_id for item in facts),
        required_evidence_ids=tuple(valid_evidence),
        relevant_failure_ids=tuple(item.failure_id for item in blockers),
        relevant_relation_ids=tuple(relation_ids),
        interfaces=interfaces,
        blockers=tuple(
            BlockerRecord(
                failure_id=item.failure_id,
                kind=item.kind.value,
                exact_failed_claim=item.exact_failed_claim,
                assumptions=tuple(item.scope.assumptions),
                witness_refs=tuple(item.witness_refs),
                provenance=item.provenance,
                route_id=item.route_id,
                target_obligation_id=item.target_obligation_id,
                rejected_step=item.rejected_step,
                reopen_conditions=tuple(item.reopen_conditions),
                created_at=item.created_at,
            )
            for item in blockers
        ),
        omissions=tuple(omissions),
    )


def _unique_ids(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in values:
        if not isinstance(item, str) or item == "" or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _statement_index(facts: Sequence[FactSnapshot]) -> dict[str, str]:
    index: dict[str, str] = {}
    for fact in facts:
        key = _norm_statement(fact.statement or _section_body(fact.raw_markdown, "statement"))
        if key and key not in index:
            index[key] = fact.fact_id
    return index


def _norm_statement(text: str) -> str:
    return " ".join(text.split()).casefold()


def _section_body(markdown: str, heading: str) -> str:
    if not isinstance(markdown, str) or markdown == "":
        return ""
    target = heading.casefold()
    lines = markdown.splitlines()
    collecting = False
    body: list[str] = []
    for line in lines:
        match = _HEADING_RE.match(line.strip())
        if match is not None:
            if collecting:
                break
            collecting = match.group(1).casefold() == target
            continue
        if collecting:
            body.append(line.rstrip())
    return "\n".join(body).strip()


def _frontmatter_assumptions(markdown: str) -> tuple[str, ...]:
    match = _FRONTMATTER_RE.match(markdown)
    if match is None:
        return ()
    assumptions: list[str] = []
    for raw_line in match.group(1).splitlines():
        line = raw_line.strip()
        if line.startswith("assumptions:"):
            rest = line.split(":", 1)[1].strip()
            if rest.startswith("[") and rest.endswith("]"):
                inner = rest[1:-1].strip()
                if inner:
                    assumptions.extend(
                        part.strip().strip("'\"") for part in inner.split(",") if part.strip()
                    )
            continue
        if line.startswith("- ") and assumptions:
            assumptions.append(line[2:].strip().strip("'\""))
    return tuple(item for item in assumptions if item)


def _blocker_inapplicable(
    record: FailureRecord, obligation: Obligation, route: Route | None
) -> str | None:
    if record.scope != obligation.scope:
        return "scope_mismatch"
    if record.contract_id is not None and record.contract_id != obligation.contract_id:
        return "contract_mismatch"
    return None


def _automatic_blocker_match(
    record: FailureRecord, obligation: Obligation, route: Route | None
) -> bool:
    if _blocker_inapplicable(record, obligation, route) is not None:
        return False
    if record.target_obligation_id == obligation.obligation_id:
        return True
    if route is not None:
        if record.route_id == route.route_id:
            return True
        if _mechanism_overlap(record, route):
            return True
    return record.route_id is None and record.target_obligation_id is None


def _mechanism_overlap(record: FailureRecord, route: Route) -> bool:
    haystack = set(_tokenize(route.mechanism))
    haystack |= set(_tokenize(route.progress_claim))
    needle = set(_tokenize(f"{record.kind.value} {record.exact_failed_claim} {record.provenance}"))
    if record.rejected_step:
        needle |= set(_tokenize(record.rejected_step))
    return bool(haystack & needle)


def _tokenize(text: str) -> list[str]:
    return [part for part in re.split(r"[^A-Za-z0-9]+", text.casefold()) if part]


def _blocker_rank_key(obligation: Obligation, route: Route | None):
    def key(item: FailureRecord) -> tuple[int, int, int, str]:
        link = 0
        if item.target_obligation_id == obligation.obligation_id:
            link = 0
        elif route is not None and item.route_id == route.route_id:
            link = 1
        elif route is not None and _mechanism_overlap(item, route):
            link = 2
        else:
            link = 3
        scope_rank = 0 if item.scope == obligation.scope else 1
        return (link, scope_rank, 0, item.failure_id)

    return key


def _dedupe_blockers(
    records: list[FailureRecord],
) -> tuple[list[FailureRecord], list[RecallOmission]]:
    kept: list[FailureRecord] = []
    omissions: list[RecallOmission] = []
    seen_keys: set[str] = set()
    for record in records:
        digest = sha256_hex(
            canonical_json(
                {
                    "kind": record.kind.value,
                    "claim": record.exact_failed_claim,
                    "scope": record.scope.to_dict(),
                    "route_id": record.route_id,
                    "rejected_step": record.rejected_step,
                }
            )
        )
        if digest in seen_keys:
            omissions.append(
                RecallOmission(
                    item_id=record.failure_id,
                    item_type="failure",
                    reason="duplicate",
                )
            )
            continue
        seen_keys.add(digest)
        kept.append(record)
    return kept, omissions
