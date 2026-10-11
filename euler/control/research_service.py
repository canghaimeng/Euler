"""Transactional public research-state operations over existing Euler records.

Proposals are not evidence. Checked evidence is imported through dedicated
adapters; this API never writes a Fact or accepts arbitrary checker-PASS JSON.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .acceptance import collect_fact_admissions
from .adapters.project import snapshot_project
from .contracts import CheckerResult, EvidenceArtifact, EvidenceKind, ValidityStatus, evidence_satisfies
from .ids import canonical_json, sha256_hex
from .obligations import ObligationStatus
from .project import EulerProject
from .schema import EventEnvelope


class ResearchServiceError(ValueError):
    """Invalid proposal, unsupported evidence import, or scope mismatch."""


PUBLIC_EVENTS = frozenset({
    "obligation.created", "obligation.status_changed", "obligation.reopened",
    "route.created", "route.status_changed", "relation.created", "failure.recorded",
})


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def frontier_payload(project: EulerProject) -> dict[str, Any]:
    """A replayable O/R/B/E projection, with exact records and graph links."""
    view = project.research_view()
    obligations = [o for o in view.obligations()
                   if o.status in {ObligationStatus.OPEN, ObligationStatus.IN_PROGRESS}
                   or o.validity is ValidityStatus.STALE]
    ids = {o.obligation_id for o in obligations}
    routes = [r for r in view.routes() if r.target_obligation_id in ids
              and r.status.value not in {"PARKED", "REFUTED", "COMPLETED"}]
    route_ids = {r.route_id for r in routes}
    blockers = [b for b in view.failures()
                if b.target_obligation_id in ids or b.route_id in route_ids]
    refs = {ref for record in (*obligations, *routes) for ref in record.evidence_refs}
    refs.update(ref for blocker in blockers for ref in blocker.witness_refs)
    relations = [r for r in view.relations()
                 if r.source in ids | route_ids or r.target in ids | route_ids]
    refs.update(ref for relation in relations for ref in relation.evidence_refs)
    evidence = [e.to_dict() for e in view.evidence() if e.artifact_id in refs]
    data = {
        "schema_version": 1, "project_id": project.project_id,
        "contract_id": project.manifest.contract_id,
        "root_obligation_id": project.manifest.target_obligation_id,
        "obligations": [o.to_dict() for o in obligations],
        "routes": [r.to_dict() for r in routes],
        "blockers": [b.to_dict() for b in blockers], "evidence": evidence,
        "relations": [r.to_dict() for r in relations],
    }
    return {**data, "frontier_hash": _digest(data)}


class ResearchService:
    def __init__(self, project: EulerProject, *, actor: str):
        if not isinstance(project, EulerProject) or not actor.strip():
            raise ResearchServiceError("an open project and named actor are required")
        self.project = project
        self.actor = actor

    def _append(self, event_type: str, payload: Mapping[str, Any], key: str,
                timestamp: str) -> dict[str, Any]:
        result = self.project.event_store.append(EventEnvelope.build(
            project_id=self.project.project_id, actor=self.actor,
            event_type=event_type, idempotency_key=key, payload=dict(payload),
            timestamp_utc=timestamp, correlation_id=self.project.project_id,
        ))
        return {"event_id": result.event.event_id, "created": result.created}

    def apply(self, operations: Sequence[Mapping[str, Any]], *,
              timestamp_utc: str | None = None) -> dict[str, Any]:
        """Validate and commit a whole ordered change set, or roll it all back."""
        if isinstance(operations, (str, bytes)) or not operations:
            raise ResearchServiceError("operations must be a nonempty sequence")
        ts = timestamp_utc or _now()
        results = []
        with self.project.event_store.transaction(immediate=True):
            before = frontier_payload(self.project)["frontier_hash"]
            for operation in operations:
                if not isinstance(operation, Mapping) or set(operation) - {"event_type", "payload", "idempotency_key"}:
                    raise ResearchServiceError("malformed research operation")
                kind = operation.get("event_type")
                payload = operation.get("payload")
                if kind not in PUBLIC_EVENTS or not isinstance(payload, Mapping):
                    raise ResearchServiceError("unsupported operation; checked evidence requires an adapter")
                if kind == "route.status_changed" and payload.get("status") == "COMPLETED":
                    self._check_route_completion(payload)
                # New research objects start as proposals, never fabricated results.
                for event, field, allowed in [
                    ("obligation.created", "obligation", {"OPEN"}),
                    ("route.created", "route", {"PROPOSED"}),
                ]:
                    if kind == event:
                        record = payload.get(field, {})
                        if not isinstance(record, Mapping) or record.get("status", next(iter(allowed))) not in allowed:
                            raise ResearchServiceError("create a proposal before changing its status")
                key = operation.get("idempotency_key") or "research:" + _digest([kind, payload])
                results.append(self._append(kind, payload, key, ts))
                # Replay inside the transaction checks scope, known IDs, evidence,
                # allowed transitions and AND/OR references before anything commits.
                self.project.research_view()
            frontier = frontier_payload(self.project)
        return {"events": results, "before_frontier_hash": before, "frontier": frontier}

    def _check_route_completion(self, payload: Mapping[str, Any]) -> None:
        view = self.project.research_view()
        route = view.get_route(payload.get("route_id"))
        if route is None:
            raise ResearchServiceError("unknown route")
        if route.status.value == "COMPLETED":
            return  # The original validated event can be replayed idempotently.
        target = view.get_obligation(route.target_obligation_id)
        for oid in route.required_obligation_ids:
            premise = view.get_obligation(oid)
            if premise is None or premise.status is not ObligationStatus.DISCHARGED or premise.validity is not ValidityStatus.VALID:
                raise ResearchServiceError("route completion requires every AND premise to be discharged")
        claim = route.progress_claim
        if not route.required_obligation_ids and target.status is ObligationStatus.DISCHARGED:
            claim = target.exact_statement
        if not claim:
            raise ResearchServiceError("route completion requires an explicit checked closing implication")
        evidence = [view.get_evidence(ref) for ref in payload.get("evidence_refs", [])]
        if not any(e is not None and e.validity is ValidityStatus.VALID
                   and e.checker_result is CheckerResult.PASS
                   and e.evidence_kind is not EvidenceKind.HEURISTIC_OBSERVATION
                   and e.contract_id == target.contract_id and e.scope == target.scope
                   and e.exact_claim == claim for e in evidence):
            raise ResearchServiceError("route completion needs evidence for its exact closing implication")

    def admit_fact(self, fact_id: str, obligation_id: str, *,
                   confirmed_scope_hash: str, alignment_note: str,
                   timestamp_utc: str | None = None) -> dict[str, Any]:
        """Link an existing verifier-admitted Fact to an explicitly reviewed scope.

        The review is an accountable alignment assertion, not a compiler proof of
        semantic equivalence. This produces LLM-level evidence only.
        """
        ts = timestamp_utc or _now()
        with self.project.event_store.transaction(immediate=True):
            view = self.project.research_view()
            obligation = view.get_obligation(obligation_id)
            if obligation is None:
                raise ResearchServiceError("unknown obligation")
            if confirmed_scope_hash != _digest(obligation.scope.to_dict()) or not alignment_note.strip():
                raise ResearchServiceError("explicit matching scope review is required")
            snapshot = snapshot_project(self.project.project_dir)
            facts = {f.fact_id: f for f in snapshot.active_facts}
            fact = facts.get(fact_id)
            if fact is None or fact.statement != obligation.exact_statement:
                raise ResearchServiceError("Fact must be active and match the obligation statement")
            admissions = [a for a in collect_fact_admissions(snapshot)
                          if a.accepted and a.fact_id == fact_id
                          and a.exact_claim == obligation.exact_statement]
            if not admissions:
                raise ResearchServiceError("Fact has no accepted fact_submit admission")
            admission = sorted(admissions, key=lambda a: a.record_id)[0]
            dependencies: dict[str, str] = {}

            def collect(fid: str) -> None:
                if fid in dependencies:
                    return
                current = facts.get(fid)
                if current is None:
                    raise ResearchServiceError("Fact dependency missing or revoked")
                dependencies[fid] = current.content_sha256
                for predecessor in current.predecessors:
                    collect(predecessor)

            collect(fact_id)
            source = self.project.artifacts.put_bytes(fact.raw_markdown.encode(), media_type="text/markdown")
            review = self.project.artifacts.put_bytes(canonical_json({
                "actor": self.actor, "scope_hash": confirmed_scope_hash,
                "note": alignment_note, "fact_id": fact_id,
                "obligation_id": obligation_id, "admission": admission.to_dict(),
            }).encode(), media_type="application/json")
            evidence = EvidenceArtifact.build(
                content_hash=source.content_hash, producer="fact_submit:" + admission.record_id,
                contract_id=obligation.contract_id, evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
                exact_claim=obligation.exact_statement, scope=obligation.scope,
                input_hashes=tuple(sorted(set(dependencies.values()))),
                environment_hash=review.content_hash, checker_id="fact_submit",
                checker_version="1", checker_result=CheckerResult.PASS, created_at=ts,
            )
            existing = view.get_evidence(evidence.artifact_id)
            if existing is None:
                self._append("evidence.registered", {"evidence": evidence.to_dict()},
                             "fact-evidence:" + evidence.artifact_id, ts)
            elif existing.validity is not ValidityStatus.VALID:
                raise ResearchServiceError("this evidence was invalidated; re-verification is required")
            self._append("euler.fact_linked", {
                "fact_id": fact_id, "obligation_id": obligation_id,
                "evidence_id": evidence.artifact_id, "dependency_hashes": dependencies,
                "alignment_artifact": review.content_hash,
            }, "fact-link:" + evidence.artifact_id, ts)
            self.project.research_view()
        return {"fact_id": fact_id, "obligation_id": obligation_id,
                "evidence_id": evidence.artifact_id, "evidence_kind": evidence.evidence_kind.value}

    def sync_fact_dependencies(self, *, timestamp_utc: str | None = None) -> dict[str, Any]:
        """Invalidate mapped evidence and reopen affected claims after revocation."""
        ts = timestamp_utc or _now()
        invalidated = []
        reopened = []
        snapshot = snapshot_project(self.project.project_dir)
        hashes = {fact.fact_id: fact.content_sha256 for fact in snapshot.active_facts}
        with self.project.event_store.transaction(immediate=True):
            links = [dict(event.payload) for _, event in self.project.event_store.iter_project(self.project.project_id)
                     if event.event_type == "euler.fact_linked"]
            for link in links:
                view = self.project.research_view()
                evidence = view.get_evidence(link["evidence_id"])
                if evidence is None or evidence.validity is not ValidityStatus.VALID:
                    continue
                expected = link["dependency_hashes"]
                if all(hashes.get(fid) == digest for fid, digest in expected.items()):
                    continue
                key = "fact-invalidated:" + evidence.artifact_id
                self._append("evidence.invalidated", {"artifact_id": evidence.artifact_id}, key, ts)
                invalidated.append(evidence.artifact_id)
                for obligation in view.obligations():
                    if evidence.artifact_id not in obligation.evidence_refs:
                        continue
                    if obligation.status in {ObligationStatus.DISCHARGED, ObligationStatus.REFUTED}:
                        self._append("obligation.reopened", {
                            "obligation_id": obligation.obligation_id,
                            "reason": "An admitted Fact or one of its proof dependencies changed or was revoked",
                            "new_premises": "previous verified dependency unavailable",
                        }, key + ":" + obligation.obligation_id, ts)
                        reopened.append(obligation.obligation_id)
                self.project.research_view()
        return {"invalidated_evidence": invalidated, "reopened_obligations": reopened}

    def reuse_verified_evidence(self, obligation_id: str, *, timestamp_utc: str | None = None,
                               synchronize: bool = True) -> dict[str, Any]:
        """Use an already checked exact interface without paying for another worker.

        The proof must retain its actual admission/checker provenance. This is a
        research-state update, never a new Fact or a new worker accomplishment.
        """
        ts = timestamp_utc or _now()
        if synchronize:
            self.sync_fact_dependencies(timestamp_utc=ts)
        with self.project.event_store.transaction(immediate=True):
            view = self.project.research_view()
            obligation = view.get_obligation(obligation_id)
            if obligation is None:
                raise ResearchServiceError("unknown obligation")
            if obligation.status not in {ObligationStatus.OPEN, ObligationStatus.IN_PROGRESS}:
                return {"obligation_id": obligation_id, "reused": False, "reason": "not open"}
            contract = view.get_contract(obligation.contract_id)
            snapshot = snapshot_project(self.project.project_dir)
            admissions = {a.fact_id for a in collect_fact_admissions(snapshot) if a.accepted
                          and a.exact_claim == obligation.exact_statement}
            admitted_hashes = {f.content_sha256 for f in snapshot.active_facts if f.fact_id in admissions}
            checked_kernel_ids = {
                dict(event.payload).get("evidence_id")
                for _, event in self.project.event_store.iter_project(self.project.project_id)
                if event.event_type == "euler.kernel_alignment_registered"
            }
            candidates = []
            for artifact in view.evidence():
                if not evidence_satisfies(artifact, contract.required_evidence_level, obligation.scope,
                                          contract.contract_id, exact_claim=obligation.exact_statement):
                    continue
                if artifact.evidence_kind is EvidenceKind.LLM_CHECKED_PROOF:
                    if artifact.content_hash not in admitted_hashes:
                        continue
                elif artifact.evidence_kind is EvidenceKind.KERNEL_CHECKED_PROOF:
                    if artifact.artifact_id not in checked_kernel_ids:
                        continue
                    for ref in artifact.input_hashes:
                        self.project.artifacts.verify(ref)
                else:
                    continue
                self.project.artifacts.verify(artifact.content_hash)
                candidates.append(artifact.artifact_id)
            if not candidates:
                return {"obligation_id": obligation_id, "reused": False, "reason": "no aligned admitted evidence"}
            before = frontier_payload(self.project)["frontier_hash"]
            refs = sorted(candidates)
            key = "evidence-reuse:" + _digest([obligation_id, before, refs])
            self._append("obligation.status_changed", {"obligation_id": obligation_id,
                "status": "DISCHARGED", "evidence_refs": refs}, key, ts)
            after = frontier_payload(self.project)["frontier_hash"]
            self._append("euler.evidence_reused", {"obligation_id": obligation_id, "evidence_ids": refs,
                "before_frontier_hash": before, "after_frontier_hash": after}, key, ts)
        return {"obligation_id": obligation_id, "reused": True, "evidence_ids": refs}
