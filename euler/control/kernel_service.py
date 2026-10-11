"""Main-only kernel execution and separately reviewed statement alignment."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from . import kernel_evidence as kernel
from .contracts import EvidenceArtifact, ValidityStatus
from .ids import canonical_json, sha256_hex
from .kernel_dependencies import KernelDependencyError, inspect_kernel_dependencies
from .local_actions import LocalActionError, run_local_action
from .project import EulerProject
from .research_service import ResearchService, ResearchServiceError, _now


def _target(project, obligation_id):
    view = project.research_view()
    obligation = view.get_obligation(obligation_id)
    if obligation is None:
        raise ResearchServiceError("unknown obligation")
    contract = view.get_contract(obligation.contract_id)
    if contract is None:
        raise ResearchServiceError("missing contract")
    return contract, obligation


def _live_source_manifest(source: Path, *, deadline: float):
    """Hash live sources together with verified dependency pin identity."""
    try:
        deps = inspect_kernel_dependencies(source, deadline=deadline)
    except KernelDependencyError as exc:
        raise ResearchServiceError(str(exc)) from exc
    try:
        manifest = kernel._hash_project_sources(
            source, dependency_fingerprint=deps.fingerprint
        )
    except kernel.KernelEvidenceError as exc:
        raise ResearchServiceError(str(exc)) from exc
    return deps, manifest


def check_kernel(project: EulerProject, obligation_id: str, request: Mapping[str, Any], *,
                 request_id: str, actor: str) -> dict[str, Any]:
    """Run the actual configured checker; caller PASS records/executables are forbidden."""
    allowed = {"project_dir", "module_name", "theorem_name", "expected_type", "timeout_ms"}
    if not isinstance(request, Mapping) or set(request) - allowed:
        raise ResearchServiceError("kernel request accepts source/target fields, not checker results or executable overrides")
    contract, obligation = _target(project, obligation_id)
    target = kernel.KernelCheckRequest(**dict(request), contract_id=contract.contract_id,
        obligation_id=obligation_id, exact_claim=obligation.exact_statement,
        lake_bin=os.environ.get("EULER_KERNEL_LAKE_BIN") or None,
        lean_bin=os.environ.get("EULER_KERNEL_LEAN_BIN") or None)
    source = Path(target.project_dir).expanduser().resolve()
    deadline = time.monotonic() + max(0.1, target.timeout_ms / 1000.0)
    deps, manifest = _live_source_manifest(source, deadline=deadline)
    parameters = {**dict(request), "source_manifest": manifest.to_dict(),
                  "dependency_fingerprint": deps.fingerprint,
                  "checker_version": kernel.KERNEL_CHECKER_VERSION,
                  "lake_bin": target.lake_bin, "lean_bin": target.lean_bin}

    def execute():
        # No injected transport on this public path. The selected host checker
        # executes before its output can enter the research graph.
        artifact = kernel.run_kernel_check(target, artifacts=project.artifacts)
        artifact_ref = kernel.persist_kernel_artifact(artifact, project.artifacts)
        refs = [artifact_ref.content_hash]
        source_index = {}
        if artifact.status is kernel.KernelCheckStatus.PASS:
            current_deps, current = _live_source_manifest(source, deadline=deadline)
            if current.manifest_hash != artifact.source_manifest.manifest_hash:
                raise ResearchServiceError("source changed while kernel check was running; recheck the new version")
            if current_deps.fingerprint != artifact.source_manifest.dependency_fingerprint:
                raise ResearchServiceError("dependency pins changed while kernel check was running; recheck the new version")
            if artifact.source_manifest.dependency_fingerprint != deps.fingerprint:
                raise ResearchServiceError("kernel artifact dependency pins do not match the live source pins")
            # Preserve the exact project sources, not only a mutable path.
            for relative, expected in artifact.source_manifest.source_hashes:
                try:
                    path = kernel.assert_contained_source(source, source / relative)
                except kernel.KernelEvidenceError as exc:
                    raise ResearchServiceError(str(exc)) from exc
                blob = path.read_bytes()
                if sha256_hex(blob) != expected:
                    raise ResearchServiceError("kernel source changed before archival")
                ref = project.artifacts.put_bytes(blob, media_type="text/plain")
                source_index[relative] = ref.content_hash
                refs.append(ref.content_hash)
            for relative in ("lean-toolchain", "lakefile.toml", "lakefile.lean", "lake-manifest.json"):
                path = source / relative
                if path.is_file() or path.is_symlink():
                    try:
                        resolved = kernel.assert_contained_source(source, path)
                    except kernel.KernelEvidenceError as exc:
                        raise ResearchServiceError(str(exc)) from exc
                    ref = project.artifacts.put_bytes(resolved.read_bytes(), media_type="text/plain")
                    source_index[relative] = ref.content_hash
                    refs.append(ref.content_hash)
            pin_ref = project.artifacts.put_bytes(
                canonical_json([item.identity_dict() for item in current_deps.packages]).encode(),
                media_type="application/json",
            )
            source_index["dependency_pins.json"] = pin_ref.content_hash
            refs.append(pin_ref.content_hash)
            for relative, _expected in artifact.source_manifest.source_hashes:
                if relative not in source_index:
                    raise ResearchServiceError(f"kernel source archive missing {relative}")
            _, after = _live_source_manifest(source, deadline=deadline)
            if after.manifest_hash != artifact.source_manifest.manifest_hash:
                raise ResearchServiceError("source changed during archival")
        index_ref = project.artifacts.put_bytes(canonical_json(source_index).encode(), media_type="application/json")
        refs.append(index_ref.content_hash)
        result = {"status": artifact.status.value, "kernel_artifact_hash": artifact_ref.content_hash,
                  "obligation_id": obligation_id, "theorem": artifact.theorem_name,
                  "formal_statement": artifact.theorem_type, "axioms": list(artifact.axioms),
                  "source_manifest_hash": artifact.source_manifest.manifest_hash,
                  "dependency_fingerprint": artifact.source_manifest.dependency_fingerprint,
                  "source_archive_hash": index_ref.content_hash, "detail": artifact.detail}
        return result, tuple(refs)

    try:
        result = run_local_action(project, obligation_id=obligation_id, request_id=request_id,
            mechanism="lean-kernel-check", parameters=parameters, timeout_ms=target.timeout_ms,
            actor=actor, execute=execute)
    except LocalActionError as exc:
        raise ResearchServiceError(str(exc)) from exc
    if result.get("replayed") and result.get("source_manifest_hash") != manifest.manifest_hash:
        raise ResearchServiceError("cached kernel result does not match current source pins")
    if result.get("status") != "DEFERRED" and result.get("kernel_artifact_hash"):
        service = ResearchService(project, actor=actor)
        service._append("euler.kernel_checked", {
            "request_id": request_id, "kernel_artifact_hash": result["kernel_artifact_hash"],
            "obligation_id": obligation_id, "contract_id": contract.contract_id,
            "project_dir": str(source), "source_manifest_hash": result["source_manifest_hash"],
            "source_archive_hash": result["source_archive_hash"], "lease_id": result["lease_id"],
            "status": result["status"],
        }, "kernel-check:" + request_id, _now())
    return result


def align_kernel(project: EulerProject, obligation_id: str, kernel_artifact_hash: str, *,
                 formal_statement: str, confirmed_scope_hash: str, notes: str,
                 actor: str, auditor_version: str = "1", version: int = 1) -> dict[str, Any]:
    """Attest the reviewed translation, then register kernel-level evidence.

    This explicit main/auditor assertion is separate from the compiler result.
    It never asserts that Lean establishes natural-language equivalence itself.
    """
    contract, obligation = _target(project, obligation_id)
    if confirmed_scope_hash != sha256_hex(canonical_json(obligation.scope.to_dict())) or not isinstance(notes, str) or not notes.strip():
        raise ResearchServiceError("matching reviewed scope and nonempty alignment rationale required")
    checks = [dict(e.payload) for _, e in project.event_store.iter_project(project.project_id)
              if e.event_type == "euler.kernel_checked"
              and dict(e.payload).get("kernel_artifact_hash") == kernel_artifact_hash
              and dict(e.payload).get("obligation_id") == obligation_id]
    if not checks or checks[-1].get("status") != "PASS":
        raise ResearchServiceError("kernel artifact was not produced by a successful registered checker action")
    check = checks[-1]
    artifact = kernel.load_kernel_artifact(kernel_artifact_hash, project.artifacts)
    if formal_statement != artifact.theorem_type:
        raise ResearchServiceError("reviewed formal statement differs from actual checked theorem type")
    source = Path(check["project_dir"])
    current_deps, current = _live_source_manifest(source, deadline=time.monotonic() + 30.0)
    if current.manifest_hash != artifact.source_manifest.manifest_hash:
        raise ResearchServiceError("source version changed after check; run a new check")
    if current_deps.fingerprint != artifact.source_manifest.dependency_fingerprint:
        raise ResearchServiceError("source version changed after check; run a new check")
    for relative, expected in artifact.source_manifest.source_hashes:
        try:
            path = kernel.assert_contained_source(source, source / relative)
        except kernel.KernelEvidenceError as exc:
            raise ResearchServiceError(str(exc)) from exc
        if sha256_hex(path.read_bytes()) != expected:
            raise ResearchServiceError("source version changed after check; run a new check")
    review_identity = sha256_hex(canonical_json({"kernel_artifact_hash": kernel_artifact_hash,
        "obligation_id": obligation_id, "formal_statement": formal_statement,
        "confirmed_scope_hash": confirmed_scope_hash, "notes": notes,
        "actor": actor, "auditor_version": auditor_version, "version": version}))
    for _, event in project.event_store.iter_project(project.project_id):
        data = dict(event.payload)
        if event.event_type == "euler.kernel_alignment_registered" and data.get("review_identity") == review_identity:
            old = project.research_view().get_evidence(data["evidence_id"])
            if old is None or old.validity is not ValidityStatus.VALID:
                raise ResearchServiceError("previous alignment evidence is no longer valid")
            return {"obligation_id": obligation_id, "evidence_id": old.artifact_id,
                    "alignment_hash": data["alignment_hash"], "evidence_kind": old.evidence_kind.value}
    alignment = kernel.attest_statement_alignment(version=version, contract=contract,
        obligation=obligation, kernel_artifact=artifact, auditor_id=actor,
        auditor_version=auditor_version, aligned=True, created_at=_now(), notes=notes,
        kernel_artifact_hash=kernel_artifact_hash)
    evidence = kernel.build_kernel_checked_evidence(kernel_artifact=artifact, alignment=alignment,
        contract=contract, obligation=obligation, artifacts=project.artifacts, producer=actor,
        created_at=_now())
    alignment_ref = project.artifacts.put_bytes(canonical_json(alignment.to_dict()).encode(), media_type="application/json")
    service = ResearchService(project, actor=actor)
    with project.event_store.transaction(immediate=True):
        existing = project.research_view().get_evidence(evidence.artifact_id)
        if existing is None:
            service._append("evidence.registered", {"evidence": evidence.to_dict()},
                            "kernel-evidence:" + evidence.artifact_id, _now())
        elif existing.validity is not ValidityStatus.VALID:
            raise ResearchServiceError("kernel evidence was invalidated")
        service._append("euler.kernel_alignment_registered", {
            "obligation_id": obligation_id, "kernel_artifact_hash": kernel_artifact_hash,
            "alignment_hash": alignment_ref.content_hash, "evidence_id": evidence.artifact_id,
            "review_identity": review_identity,
        }, "kernel-alignment:" + review_identity, _now())
        project.research_view()
    return {"obligation_id": obligation_id, "evidence_id": evidence.artifact_id,
            "alignment_hash": alignment_ref.content_hash, "evidence_kind": evidence.evidence_kind.value}


@dataclass(frozen=True, slots=True, kw_only=True)
class KernelAcceptanceBundle:
    """One coherent kernel artifact + auditor alignment + registered evidence."""

    obligation_id: str
    kernel_artifact_hash: str
    alignment_hash: str
    evidence_id: str
    alignment: kernel.StatementAlignmentAttestation
    evidence: EvidenceArtifact


def iter_kernel_alignment_records(project: EulerProject, obligation_id: str) -> tuple[dict[str, Any], ...]:
    found: list[dict[str, Any]] = []
    for _seq, event in project.event_store.iter_project(project.project_id):
        if event.event_type != "euler.kernel_alignment_registered":
            continue
        payload = dict(event.payload)
        if payload.get("obligation_id") == obligation_id:
            found.append(payload)
    return tuple(found)


def select_kernel_bundle(
    project: EulerProject,
    obligation_id: str,
    *,
    preferred_alignment: Optional[kernel.StatementAlignmentAttestation] = None,
    evidence_ids: Optional[tuple[str, ...]] = None,
) -> Optional[KernelAcceptanceBundle]:
    """Newest valid (or preferred) kernel+alignment+evidence tuple for one obligation."""
    view = project.research_view()
    obligation = view.get_obligation(obligation_id)
    allowed = None if evidence_ids is None else set(evidence_ids)
    if obligation is not None and evidence_ids is None and obligation.evidence_refs:
        allowed = set(obligation.evidence_refs)
    records = list(iter_kernel_alignment_records(project, obligation_id))
    for payload in reversed(records):
        try:
            blob = project.artifacts.get_bytes(str(payload["alignment_hash"]))
            alignment = kernel.StatementAlignmentAttestation.from_dict(json.loads(blob))
        except (KeyError, json.JSONDecodeError, kernel.KernelEvidenceError, OSError):
            continue
        if preferred_alignment is not None and alignment.attestation_id != preferred_alignment.attestation_id:
            continue
        evidence = view.get_evidence(str(payload.get("evidence_id") or ""))
        if evidence is None or evidence.validity is not ValidityStatus.VALID:
            continue
        if allowed is not None and evidence.artifact_id not in allowed:
            continue
        kernel_hash = str(payload.get("kernel_artifact_hash") or "")
        if alignment.kernel_artifact_hash != kernel_hash:
            continue
        if evidence.content_hash != kernel_hash:
            continue
        if obligation is not None and alignment.exact_claim != obligation.exact_statement:
            continue
        if obligation is not None and alignment.obligation_id != obligation.obligation_id:
            continue
        try:
            artifact = kernel.load_kernel_artifact(kernel_hash, project.artifacts)
        except kernel.KernelEvidenceError:
            continue
        if artifact.status is not kernel.KernelCheckStatus.PASS:
            continue
        if artifact.theorem_type != alignment.formal_statement:
            continue
        return KernelAcceptanceBundle(
            obligation_id=obligation_id,
            kernel_artifact_hash=kernel_hash,
            alignment_hash=str(payload["alignment_hash"]),
            evidence_id=evidence.artifact_id,
            alignment=alignment,
            evidence=evidence,
        )
    return None


def latest_kernel_alignment(project: EulerProject, obligation_id: str):
    """Load the coherent current main/auditor alignment for final audit."""
    bundle = select_kernel_bundle(project, obligation_id)
    if bundle is None:
        return None
    return bundle.alignment
