"""Kernel-checked proof adapter: real Lean execution plus alignment.

Stdlib plus existing research-control types. This module never writes the Fact
Graph, EventStore, or global memory. Compiler success is not natural-language
equivalence; callers must attach a versioned statement-alignment attestation.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

from euler.control.artifacts import ArtifactStore, ArtifactStoreError
from euler.control.contracts import (
    CheckerResult,
    EvidenceArtifact,
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
)
from euler.control.ids import canonical_json, sha256_hex, stable_id
from euler.control.obligations import Obligation

_CHECKER_ID = "euler-lean-kernel"
_CHECKER_VERSION = "3"
_ARTIFACT_DOMAIN = "kernel-check-artifact"
_ALIGN_DOMAIN = "statement-alignment"
_ID_VERSION = 1
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_AXIOM_NONE_RE = re.compile(
    r"'([^']+)'\s+does not depend on any axioms", re.IGNORECASE
)
_AXIOM_LIST_RE = re.compile(
    r"'([^']+)'\s+depends on axioms:\s*\[([^\]]*)\]", re.IGNORECASE
)
_CHECK_RE = re.compile(r"^(\S+)\s+:\s+(.+)$")
_DRIVER_NAME = "_EulerKernelCheck.lean"
_DEFAULT_TIMEOUT_MS = 60_000
TRUSTED_PROVENANCE = "lake-env"
UNTRUSTED_PROVENANCE = "untrusted"
_LAKE_NAMES = frozenset({"lake", "lake.exe"})
_SKIP_COPY_DIRS = frozenset({".git", ".lake", "lake-packages", "__pycache__"})
_PIN_FILES = frozenset({"lean-toolchain", "lakefile.toml", "lakefile.lean", "lake-manifest.json"})
# Isolation copies toolchain pins, lakefiles, lake-manifest, and project
# ``.lean`` sources. Declared lake packages are materialized afterwards from
# the local ``.lake/packages`` cache (see kernel_dependencies.py). The adapter
# does not download mathlib or write the original cache.

# Lean 4 kernel axioms only. A broader allow-list is rejected as a request error.
APPROVED_FOUNDATIONAL_AXIOMS = frozenset(
    {
        "propext",
        "Quot.sound",
        "Classical.choice",
    }
)
FORBIDDEN_AXIOM_NAMES = frozenset({"sorryAx", "Lean.sorryAx", "sorry"})

__all__ = [
    "APPROVED_FOUNDATIONAL_AXIOMS",
    "FORBIDDEN_AXIOM_NAMES",
    "KERNEL_CHECKER_ID",
    "KERNEL_CHECKER_VERSION",
    "KernelEvidenceError",
    "KernelCheckStatus",
    "KernelSourceManifest",
    "KernelCheckRequest",
    "KernelCheckArtifact",
    "StatementAlignmentAttestation",
    "KernelCheckerTransport",
    "TRUSTED_PROVENANCE",
    "UNTRUSTED_PROVENANCE",
    "LeanLakeTransport",
    "InjectedKernelTransport",
    "normalize_axiom_name",
    "persist_kernel_artifact",
    "load_kernel_artifact",
    "run_kernel_check",
    "attest_statement_alignment",
    "build_kernel_checked_evidence",
    "assert_contained_source",
]

KERNEL_CHECKER_ID = _CHECKER_ID
KERNEL_CHECKER_VERSION = _CHECKER_VERSION


class KernelEvidenceError(ValueError):
    """Malformed kernel request, illegal axiom policy, or unusable artifact."""


class KernelCheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INFRA = "INFRA"


@dataclass(frozen=True, slots=True, kw_only=True)
class KernelSourceManifest:
    toolchain: str
    lakefile_hash: str
    lake_manifest_hash: Optional[str]
    source_hashes: tuple[tuple[str, str], ...]
    manifest_hash: str
    dependency_fingerprint: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "toolchain", _require_nonempty("toolchain", self.toolchain)
        )
        object.__setattr__(
            self, "lakefile_hash", _require_hex64("lakefile_hash", self.lakefile_hash)
        )
        if self.lake_manifest_hash is not None:
            object.__setattr__(
                self,
                "lake_manifest_hash",
                _require_hex64("lake_manifest_hash", self.lake_manifest_hash),
            )
        pairs = _require_source_pairs(self.source_hashes)
        object.__setattr__(self, "source_hashes", pairs)
        if self.dependency_fingerprint is not None:
            object.__setattr__(
                self,
                "dependency_fingerprint",
                _require_hex64("dependency_fingerprint", self.dependency_fingerprint),
            )
        expected = sha256_hex(
            canonical_json(
                {
                    "toolchain": self.toolchain,
                    "lakefile_hash": self.lakefile_hash,
                    "lake_manifest_hash": self.lake_manifest_hash,
                    "source_hashes": [[name, digest] for name, digest in pairs],
                    "dependency_fingerprint": self.dependency_fingerprint,
                }
            ).encode("utf-8")
        )
        object.__setattr__(
            self, "manifest_hash", _require_hex64("manifest_hash", self.manifest_hash)
        )
        if self.manifest_hash != expected:
            raise KernelEvidenceError("manifest_hash does not match source fields")

    @classmethod
    def build(
        cls,
        *,
        toolchain: str,
        lakefile_hash: str,
        lake_manifest_hash: Optional[str] = None,
        source_hashes: Sequence[tuple[str, str]] = (),
        dependency_fingerprint: Optional[str] = None,
    ) -> "KernelSourceManifest":
        pairs = tuple((str(name), str(digest)) for name, digest in source_hashes)
        manifest_hash = sha256_hex(
            canonical_json(
                {
                    "toolchain": toolchain,
                    "lakefile_hash": lakefile_hash,
                    "lake_manifest_hash": lake_manifest_hash,
                    "source_hashes": [[name, digest] for name, digest in pairs],
                    "dependency_fingerprint": dependency_fingerprint,
                }
            ).encode("utf-8")
        )
        return cls(
            toolchain=toolchain,
            lakefile_hash=lakefile_hash,
            lake_manifest_hash=lake_manifest_hash,
            source_hashes=pairs,
            manifest_hash=manifest_hash,
            dependency_fingerprint=dependency_fingerprint,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "toolchain": self.toolchain,
            "lakefile_hash": self.lakefile_hash,
            "lake_manifest_hash": self.lake_manifest_hash,
            "source_hashes": [[name, digest] for name, digest in self.source_hashes],
            "manifest_hash": self.manifest_hash,
            "dependency_fingerprint": self.dependency_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: object) -> "KernelSourceManifest":
        payload = _mapping("KernelSourceManifest", data)
        allowed = {
            "toolchain",
            "lakefile_hash",
            "lake_manifest_hash",
            "source_hashes",
            "manifest_hash",
            "dependency_fingerprint",
        }
        _reject_extra(payload, allowed, "KernelSourceManifest")
        _require_keys(
            payload, {"toolchain", "lakefile_hash", "source_hashes", "manifest_hash"},
            "KernelSourceManifest",
        )
        raw_pairs = payload["source_hashes"]
        pairs: list[tuple[str, str]] = []
        if not isinstance(raw_pairs, (list, tuple)):
            raise KernelEvidenceError("source_hashes must be a list of pairs")
        for item in raw_pairs:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise KernelEvidenceError("source_hashes entries must be [path, hash]")
            pairs.append((str(item[0]), str(item[1])))
        return cls(
            toolchain=payload["toolchain"],
            lakefile_hash=payload["lakefile_hash"],
            lake_manifest_hash=payload.get("lake_manifest_hash"),
            source_hashes=tuple(pairs),
            manifest_hash=payload["manifest_hash"],
            dependency_fingerprint=payload.get("dependency_fingerprint"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class KernelCheckRequest:
    project_dir: str
    module_name: str
    theorem_name: str
    expected_type: str
    contract_id: str
    obligation_id: str
    exact_claim: str
    timeout_ms: int = _DEFAULT_TIMEOUT_MS
    approved_axioms: tuple[str, ...] = tuple(sorted(APPROVED_FOUNDATIONAL_AXIOMS))
    lean_bin: Optional[str] = None
    lake_bin: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "project_dir", _require_nonempty("project_dir", self.project_dir)
        )
        object.__setattr__(
            self, "module_name", _require_ident("module_name", self.module_name)
        )
        object.__setattr__(
            self, "theorem_name", _require_ident("theorem_name", self.theorem_name)
        )
        object.__setattr__(
            self, "expected_type", _require_nonempty("expected_type", self.expected_type)
        )
        object.__setattr__(
            self, "contract_id", _require_hex64("contract_id", self.contract_id)
        )
        object.__setattr__(
            self, "obligation_id", _require_hex64("obligation_id", self.obligation_id)
        )
        object.__setattr__(
            self, "exact_claim", _require_nonempty("exact_claim", self.exact_claim)
        )
        timeout = _require_positive_int("timeout_ms", self.timeout_ms)
        object.__setattr__(self, "timeout_ms", timeout)
        approved = tuple(
            _require_nonempty(f"approved_axioms[{i}]", item)
            for i, item in enumerate(self.approved_axioms)
        )
        if not approved:
            raise KernelEvidenceError("approved_axioms must be nonempty")
        extra = {normalize_axiom_name(item) for item in approved} - {
            normalize_axiom_name(item) for item in APPROVED_FOUNDATIONAL_AXIOMS
        }
        if extra:
            raise KernelEvidenceError(
                "approved_axioms is broader than the foundational Lean kernel set: "
                + ",".join(sorted(extra))
            )
        forbidden = {
            normalize_axiom_name(item) for item in approved
        } & {normalize_axiom_name(item) for item in FORBIDDEN_AXIOM_NAMES}
        if forbidden:
            raise KernelEvidenceError("approved_axioms cannot include sorryAx")
        object.__setattr__(self, "approved_axioms", tuple(sorted(set(approved))))
        if self.lean_bin is not None:
            object.__setattr__(self, "lean_bin", _require_nonempty("lean_bin", self.lean_bin))
            if Path(self.lean_bin).name not in {"lean", "lean.exe"}:
                raise KernelEvidenceError(
                    "lean_bin must be a lean executable; it is not used for checking"
                )
        if self.lake_bin is not None:
            object.__setattr__(self, "lake_bin", _require_nonempty("lake_bin", self.lake_bin))
            if Path(self.lake_bin).name not in _LAKE_NAMES:
                raise KernelEvidenceError(
                    "lake_bin must be named lake; arbitrary checker binaries are not trusted"
                )


@dataclass(frozen=True, slots=True, kw_only=True)
class KernelCheckArtifact:
    artifact_id: str
    status: KernelCheckStatus
    checker_id: str
    checker_version: str
    toolchain: str
    lean_githash: Optional[str]
    module_name: str
    theorem_name: str
    theorem_type: str
    axioms: tuple[str, ...]
    source_manifest: KernelSourceManifest
    contract_id: str
    obligation_id: str
    exact_claim: str
    command: tuple[str, ...]
    stdout: str
    stderr: str
    detail: str
    created_at: str
    provenance_kind: str = UNTRUSTED_PROVENANCE
    lake_executable: Optional[str] = None
    lake_executable_digest: Optional[str] = None
    build_status: Optional[str] = None
    check_status: Optional[str] = None
    build_returncode: Optional[int] = None
    check_returncode: Optional[int] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", _require_enum(KernelCheckStatus, self.status, "status")
        )
        object.__setattr__(
            self, "checker_id", _require_nonempty("checker_id", self.checker_id)
        )
        object.__setattr__(
            self,
            "checker_version",
            _require_nonempty("checker_version", self.checker_version),
        )
        object.__setattr__(
            self, "toolchain", _require_nonempty("toolchain", self.toolchain)
        )
        if self.lean_githash is not None:
            object.__setattr__(
                self, "lean_githash", _require_nonempty("lean_githash", self.lean_githash)
            )
        object.__setattr__(
            self, "module_name", _require_ident("module_name", self.module_name)
        )
        object.__setattr__(
            self, "theorem_name", _require_ident("theorem_name", self.theorem_name)
        )
        object.__setattr__(self, "theorem_type", str(self.theorem_type or ""))
        object.__setattr__(self, "axioms", _require_str_tuple("axioms", self.axioms))
        if not isinstance(self.source_manifest, KernelSourceManifest):
            raise KernelEvidenceError("source_manifest must be a KernelSourceManifest")
        object.__setattr__(
            self, "contract_id", _require_hex64("contract_id", self.contract_id)
        )
        object.__setattr__(
            self, "obligation_id", _require_hex64("obligation_id", self.obligation_id)
        )
        object.__setattr__(
            self, "exact_claim", _require_nonempty("exact_claim", self.exact_claim)
        )
        object.__setattr__(
            self, "command", _require_str_tuple("command", self.command)
        )
        object.__setattr__(self, "stdout", str(self.stdout or ""))
        object.__setattr__(self, "stderr", str(self.stderr or ""))
        object.__setattr__(self, "detail", str(self.detail or ""))
        object.__setattr__(
            self,
            "provenance_kind",
            _require_nonempty("provenance_kind", self.provenance_kind),
        )
        if self.lake_executable is not None:
            object.__setattr__(
                self,
                "lake_executable",
                _require_nonempty("lake_executable", self.lake_executable),
            )
        if self.lake_executable_digest is not None:
            object.__setattr__(
                self,
                "lake_executable_digest",
                _require_hex64("lake_executable_digest", self.lake_executable_digest),
            )
        if self.build_status is not None:
            object.__setattr__(
                self, "build_status", _require_nonempty("build_status", self.build_status)
            )
        if self.check_status is not None:
            object.__setattr__(
                self, "check_status", _require_nonempty("check_status", self.check_status)
            )
        if self.build_returncode is not None:
            if isinstance(self.build_returncode, bool) or not isinstance(self.build_returncode, int):
                raise KernelEvidenceError("build_returncode must be int")
        if self.check_returncode is not None:
            if isinstance(self.check_returncode, bool) or not isinstance(self.check_returncode, int):
                raise KernelEvidenceError("check_returncode must be int")
        _parse_utc("created_at", self.created_at)
        expected = _artifact_id_from_record(self)
        object.__setattr__(
            self, "artifact_id", _require_hex64("artifact_id", self.artifact_id)
        )
        if self.artifact_id != expected:
            raise KernelEvidenceError("artifact_id does not match semantic fields")
        _assert_pass_is_honest(self)

    @classmethod
    def build(cls, **fields: Any) -> "KernelCheckArtifact":
        fields = dict(fields)
        fields.setdefault("lean_githash", None)
        fields.setdefault("checker_id", _CHECKER_ID)
        fields.setdefault("checker_version", _CHECKER_VERSION)
        fields.setdefault("provenance_kind", UNTRUSTED_PROVENANCE)
        fields.setdefault("lake_executable", None)
        fields.setdefault("lake_executable_digest", None)
        fields.setdefault("build_status", None)
        fields.setdefault("check_status", None)
        fields.setdefault("build_returncode", None)
        fields.setdefault("check_returncode", None)
        status = _require_enum(KernelCheckStatus, fields["status"], "status")
        source_manifest = fields["source_manifest"]
        payload = _artifact_payload(
            status=status,
            checker_id=str(fields.get("checker_id") or _CHECKER_ID),
            checker_version=str(fields.get("checker_version") or _CHECKER_VERSION),
            toolchain=str(fields["toolchain"]),
            lean_githash=fields.get("lean_githash"),
            module_name=str(fields["module_name"]),
            theorem_name=str(fields["theorem_name"]),
            theorem_type=str(fields.get("theorem_type") or ""),
            axioms=tuple(fields.get("axioms") or ()),
            source_manifest=source_manifest,
            contract_id=str(fields["contract_id"]),
            obligation_id=str(fields["obligation_id"]),
            exact_claim=str(fields["exact_claim"]),
            command=tuple(fields.get("command") or ()),
            detail=str(fields.get("detail") or ""),
            provenance_kind=str(fields.get("provenance_kind") or UNTRUSTED_PROVENANCE),
            lake_executable=fields.get("lake_executable"),
            lake_executable_digest=fields.get("lake_executable_digest"),
            build_status=fields.get("build_status"),
            check_status=fields.get("check_status"),
        )
        artifact_id = stable_id(_ARTIFACT_DOMAIN, _ID_VERSION, canonical_json(payload))
        return cls(artifact_id=artifact_id, status=status, **{
            key: value for key, value in fields.items() if key != "status"
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "status": self.status.value,
            "checker_id": self.checker_id,
            "checker_version": self.checker_version,
            "toolchain": self.toolchain,
            "lean_githash": self.lean_githash,
            "module_name": self.module_name,
            "theorem_name": self.theorem_name,
            "theorem_type": self.theorem_type,
            "axioms": list(self.axioms),
            "source_manifest": self.source_manifest.to_dict(),
            "contract_id": self.contract_id,
            "obligation_id": self.obligation_id,
            "exact_claim": self.exact_claim,
            "command": list(self.command),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "detail": self.detail,
            "created_at": self.created_at,
            "provenance_kind": self.provenance_kind,
            "lake_executable": self.lake_executable,
            "lake_executable_digest": self.lake_executable_digest,
            "build_status": self.build_status,
            "check_status": self.check_status,
            "build_returncode": self.build_returncode,
            "check_returncode": self.check_returncode,
        }

    @classmethod
    def from_dict(cls, data: object) -> "KernelCheckArtifact":
        payload = _mapping("KernelCheckArtifact", data)
        allowed = {
            "artifact_id",
            "status",
            "checker_id",
            "checker_version",
            "toolchain",
            "lean_githash",
            "module_name",
            "theorem_name",
            "theorem_type",
            "axioms",
            "source_manifest",
            "contract_id",
            "obligation_id",
            "exact_claim",
            "command",
            "stdout",
            "stderr",
            "detail",
            "created_at",
            "provenance_kind",
            "lake_executable",
            "lake_executable_digest",
            "build_status",
            "check_status",
            "build_returncode",
            "check_returncode",
        }
        _reject_extra(payload, allowed, "KernelCheckArtifact")
        optional = {
            "lean_githash",
            "provenance_kind",
            "lake_executable",
            "lake_executable_digest",
            "build_status",
            "check_status",
            "build_returncode",
            "check_returncode",
        }
        _require_keys(payload, allowed - optional, "KernelCheckArtifact")
        return cls(
            artifact_id=payload["artifact_id"],
            status=payload["status"],
            checker_id=payload["checker_id"],
            checker_version=payload["checker_version"],
            toolchain=payload["toolchain"],
            lean_githash=payload.get("lean_githash"),
            module_name=payload["module_name"],
            theorem_name=payload["theorem_name"],
            theorem_type=payload["theorem_type"],
            axioms=tuple(payload["axioms"]),
            source_manifest=KernelSourceManifest.from_dict(payload["source_manifest"]),
            contract_id=payload["contract_id"],
            obligation_id=payload["obligation_id"],
            exact_claim=payload["exact_claim"],
            command=tuple(payload["command"]),
            stdout=payload["stdout"],
            stderr=payload["stderr"],
            detail=payload["detail"],
            created_at=payload["created_at"],
            provenance_kind=str(payload.get("provenance_kind") or UNTRUSTED_PROVENANCE),
            lake_executable=payload.get("lake_executable"),
            lake_executable_digest=payload.get("lake_executable_digest"),
            build_status=payload.get("build_status"),
            check_status=payload.get("check_status"),
            build_returncode=payload.get("build_returncode"),
            check_returncode=payload.get("check_returncode"),
        )

    def content_bytes(self) -> bytes:
        return canonical_json(self.to_dict()).encode("utf-8")


@dataclass(frozen=True, slots=True, kw_only=True)
class StatementAlignmentAttestation:
    attestation_id: str
    version: int
    contract_id: str
    obligation_id: str
    exact_claim: str
    formal_statement: str
    theorem_name: str
    module_name: str
    kernel_artifact_hash: str
    auditor_id: str
    auditor_version: str
    aligned: bool
    created_at: str
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "version", _require_positive_int("version", self.version)
        )
        object.__setattr__(
            self, "contract_id", _require_hex64("contract_id", self.contract_id)
        )
        object.__setattr__(
            self, "obligation_id", _require_hex64("obligation_id", self.obligation_id)
        )
        object.__setattr__(
            self, "exact_claim", _require_nonempty("exact_claim", self.exact_claim)
        )
        object.__setattr__(
            self,
            "formal_statement",
            _require_nonempty("formal_statement", self.formal_statement),
        )
        object.__setattr__(
            self, "theorem_name", _require_ident("theorem_name", self.theorem_name)
        )
        object.__setattr__(
            self, "module_name", _require_ident("module_name", self.module_name)
        )
        object.__setattr__(
            self,
            "kernel_artifact_hash",
            _require_hex64("kernel_artifact_hash", self.kernel_artifact_hash),
        )
        object.__setattr__(
            self, "auditor_id", _require_nonempty("auditor_id", self.auditor_id)
        )
        object.__setattr__(
            self,
            "auditor_version",
            _require_nonempty("auditor_version", self.auditor_version),
        )
        if not isinstance(self.aligned, bool):
            raise KernelEvidenceError("aligned must be bool")
        _parse_utc("created_at", self.created_at)
        object.__setattr__(self, "notes", str(self.notes or ""))
        expected = _alignment_id_from_record(self)
        object.__setattr__(
            self, "attestation_id", _require_hex64("attestation_id", self.attestation_id)
        )
        if self.attestation_id != expected:
            raise KernelEvidenceError("attestation_id does not match semantic fields")

    @classmethod
    def build(cls, **fields: Any) -> "StatementAlignmentAttestation":
        payload = _alignment_payload(
            version=int(fields["version"]),
            contract_id=str(fields["contract_id"]),
            obligation_id=str(fields["obligation_id"]),
            exact_claim=str(fields["exact_claim"]),
            formal_statement=str(fields["formal_statement"]),
            theorem_name=str(fields["theorem_name"]),
            module_name=str(fields["module_name"]),
            kernel_artifact_hash=str(fields["kernel_artifact_hash"]),
            auditor_id=str(fields["auditor_id"]),
            auditor_version=str(fields["auditor_version"]),
            aligned=bool(fields["aligned"]),
            notes=str(fields.get("notes") or ""),
        )
        attestation_id = stable_id(_ALIGN_DOMAIN, _ID_VERSION, canonical_json(payload))
        return cls(attestation_id=attestation_id, **fields)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attestation_id": self.attestation_id,
            "version": self.version,
            "contract_id": self.contract_id,
            "obligation_id": self.obligation_id,
            "exact_claim": self.exact_claim,
            "formal_statement": self.formal_statement,
            "theorem_name": self.theorem_name,
            "module_name": self.module_name,
            "kernel_artifact_hash": self.kernel_artifact_hash,
            "auditor_id": self.auditor_id,
            "auditor_version": self.auditor_version,
            "aligned": self.aligned,
            "created_at": self.created_at,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: object) -> "StatementAlignmentAttestation":
        payload = _mapping("StatementAlignmentAttestation", data)
        allowed = {
            "attestation_id",
            "version",
            "contract_id",
            "obligation_id",
            "exact_claim",
            "formal_statement",
            "theorem_name",
            "module_name",
            "kernel_artifact_hash",
            "auditor_id",
            "auditor_version",
            "aligned",
            "created_at",
            "notes",
        }
        _reject_extra(payload, allowed, "StatementAlignmentAttestation")
        _require_keys(payload, allowed - {"notes"}, "StatementAlignmentAttestation")
        return cls(
            attestation_id=payload["attestation_id"],
            version=payload["version"],
            contract_id=payload["contract_id"],
            obligation_id=payload["obligation_id"],
            exact_claim=payload["exact_claim"],
            formal_statement=payload["formal_statement"],
            theorem_name=payload["theorem_name"],
            module_name=payload["module_name"],
            kernel_artifact_hash=payload["kernel_artifact_hash"],
            auditor_id=payload["auditor_id"],
            auditor_version=payload["auditor_version"],
            aligned=payload["aligned"],
            created_at=payload["created_at"],
            notes=str(payload.get("notes") or ""),
        )


class KernelCheckerTransport(Protocol):
    def check(self, request: KernelCheckRequest) -> KernelCheckArtifact:
        """Execute or simulate a kernel check. Must not write truth stores."""


class InjectedKernelTransport:
    """Test transport. Returned artifacts still pass semantic validation."""

    def __init__(self, artifact: KernelCheckArtifact):
        if not isinstance(artifact, KernelCheckArtifact):
            raise KernelEvidenceError("injected transport requires a KernelCheckArtifact")
        self._artifact = artifact

    def check(self, request: KernelCheckRequest) -> KernelCheckArtifact:
        if self._artifact.contract_id != request.contract_id:
            raise KernelEvidenceError("injected artifact contract_id mismatch")
        if self._artifact.obligation_id != request.obligation_id:
            raise KernelEvidenceError("injected artifact obligation_id mismatch")
        if self._artifact.theorem_name != request.theorem_name:
            raise KernelEvidenceError("injected artifact theorem_name mismatch")
        if self._artifact.module_name != request.module_name:
            raise KernelEvidenceError("injected artifact module_name mismatch")
        return self._artifact


class LeanLakeTransport:
    """Copy the supplied Lean project and run a bounded lake/lean target check."""

    def check(self, request: KernelCheckRequest) -> KernelCheckArtifact:
        root = Path(request.project_dir).expanduser().resolve()
        _assert_readable_project(root)
        created_at = _utc_now()
        deadline = time.monotonic() + max(0.1, request.timeout_ms / 1000.0)
        try:
            lake_path, lake_digest = _resolve_lake_executable(request.lake_bin)
        except KernelEvidenceError as exc:
            raise KernelEvidenceError(str(exc)) from exc
        driver = _driver_source(request.module_name, request.theorem_name)
        with tempfile.TemporaryDirectory(prefix="euler-kernel-") as raw:
            isolated = Path(raw) / "proj"
            try:
                _copy_isolated(root, isolated)
            except KernelEvidenceError as exc:
                try:
                    manifest = _hash_project_sources(root)
                    toolchain = manifest.toolchain
                except KernelEvidenceError:
                    toolchain = "unknown"
                    manifest = KernelSourceManifest.build(
                        toolchain=toolchain,
                        lakefile_hash=sha256_hex(b"unreadable"),
                        source_hashes=(("unreadable.lean", sha256_hex(b"unreadable")),),
                    )
                return _stage_artifact(
                    request,
                    status=KernelCheckStatus.FAIL,
                    toolchain=toolchain,
                    githash=None,
                    lake_path=lake_path,
                    lake_digest=lake_digest,
                    manifest=manifest,
                    command=(str(lake_path),),
                    stdout="",
                    stderr=str(exc),
                    detail=str(exc),
                    created_at=created_at,
                    build=_Proc(KernelCheckStatus.FAIL, "", str(exc), str(exc), 1),
                    check=_Proc(KernelCheckStatus.FAIL, "", "", "not run", 1),
                )
            if time.monotonic() >= deadline:
                return _timeout_artifact(
                    request, lake_path, lake_digest, isolated, created_at, "copy"
                )
            from euler.control.kernel_dependencies import (
                KernelDependencyError,
                prepare_kernel_dependencies,
            )
            try:
                prepared = prepare_kernel_dependencies(
                    root, isolated, deadline=deadline
                )
            except KernelDependencyError as exc:
                timed_out = "timed out" in str(exc).lower()
                status = (
                    KernelCheckStatus.INFRA if timed_out else KernelCheckStatus.FAIL
                )
                try:
                    manifest = _hash_project_sources(isolated)
                    toolchain = manifest.toolchain
                except KernelEvidenceError:
                    toolchain = "unknown"
                    manifest = KernelSourceManifest.build(
                        toolchain=toolchain,
                        lakefile_hash=sha256_hex(b"unreadable"),
                        source_hashes=(("unreadable.lean", sha256_hex(b"unreadable")),),
                    )
                return _stage_artifact(
                    request,
                    status=status,
                    toolchain=toolchain,
                    githash=None,
                    lake_path=lake_path,
                    lake_digest=lake_digest,
                    manifest=manifest,
                    command=(str(lake_path),),
                    stdout="",
                    stderr=str(exc),
                    detail=str(exc),
                    created_at=created_at,
                    build=_Proc(status, "", str(exc), str(exc), -1 if timed_out else 1),
                    check=_Proc(status, "", "", "not run", -1 if timed_out else 1),
                )
            if time.monotonic() >= deadline:
                return _timeout_artifact(
                    request, lake_path, lake_digest, isolated, created_at, "dependencies"
                )
            (isolated / _DRIVER_NAME).write_text(driver, encoding="utf-8")
            manifest = _hash_project_sources(
                isolated, dependency_fingerprint=prepared.fingerprint
            )
            toolchain = manifest.toolchain
            githash, gh_proc = _lake_env_lean_githash(lake_path, isolated, deadline)
            build_cmd = [str(lake_path), "--no-cache", "build"]
            build = _run_bounded(build_cmd, isolated, _remaining(deadline))
            check_cmd = [str(lake_path), "env", "lean", _DRIVER_NAME]
            if _remaining(deadline) <= 0 and build.status is not KernelCheckStatus.INFRA:
                check = _Proc(
                    KernelCheckStatus.INFRA, "", "", "timed out before target check", -1
                )
            else:
                check = _run_bounded(check_cmd, isolated, _remaining(deadline))
            after = _hash_project_sources(
                isolated,
                only_paths=tuple(name for name, _digest in manifest.source_hashes),
                dependency_fingerprint=prepared.fingerprint,
            )
            if after.manifest_hash != manifest.manifest_hash:
                return _stage_artifact(
                    request,
                    status=KernelCheckStatus.FAIL,
                    toolchain=toolchain,
                    githash=githash,
                    lake_path=lake_path,
                    lake_digest=lake_digest,
                    manifest=manifest,
                    command=tuple(check_cmd),
                    stdout=check.stdout,
                    stderr=check.stderr + "\n" + after.manifest_hash,
                    detail="source bytes changed during the kernel check",
                    created_at=created_at,
                    build=build,
                    check=check,
                )
            judged_status, judged_detail, theorem_type, axioms = _judge_from_check(
                request, check
            )
            status, detail = _combine_stage_outcomes(build, check, judged_status, judged_detail)
            if status is KernelCheckStatus.PASS and not githash:
                status = KernelCheckStatus.FAIL
                detail = "lake-env lean githash was not recorded from the isolated project"
            dep_names = ",".join(
                f"{pin.name}@{pin.rev}" for pin in prepared.packages
            ) or "none"
            stdout = "\n".join(
                part
                for part in (
                    f"[githash] {(gh_proc.stdout if gh_proc else '')}",
                    (
                        f"[deps] fingerprint={prepared.fingerprint} "
                        f"method={prepared.method} packages={dep_names}"
                    ),
                    f"[build] {build.stdout}",
                    f"[check] {check.stdout}",
                )
                if part
            )
            stderr = "\n".join(
                part
                for part in (
                    f"[githash] {(gh_proc.stderr if gh_proc else '')}",
                    f"[build] {build.stderr}",
                    f"[check] {check.stderr}",
                )
                if part
            )
            return KernelCheckArtifact.build(
                status=status,
                checker_id=_CHECKER_ID,
                checker_version=_CHECKER_VERSION,
                toolchain=toolchain,
                lean_githash=githash,
                module_name=request.module_name,
                theorem_name=request.theorem_name,
                theorem_type=theorem_type if status is KernelCheckStatus.PASS else theorem_type,
                axioms=axioms,
                source_manifest=manifest,
                contract_id=request.contract_id,
                obligation_id=request.obligation_id,
                exact_claim=request.exact_claim,
                command=tuple(check_cmd),
                stdout=stdout,
                stderr=stderr,
                detail=detail,
                created_at=created_at,
                provenance_kind=TRUSTED_PROVENANCE,
                lake_executable=str(lake_path),
                lake_executable_digest=lake_digest,
                build_status=build.status.value,
                check_status=check.status.value,
                build_returncode=build.returncode,
                check_returncode=check.returncode,
            )


def normalize_axiom_name(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        return ""
    # Namespace identity is semantic: Lean.propext is not the core propext.
    # Only a pretty-printed universe-parameter suffix can be omitted.
    return re.sub(r"\.\{[^{}]*\}$", "", text).strip()


def persist_kernel_artifact(
    artifact: KernelCheckArtifact, artifacts: ArtifactStore
):
    if not isinstance(artifact, KernelCheckArtifact):
        raise KernelEvidenceError("artifact must be a KernelCheckArtifact")
    if not isinstance(artifacts, ArtifactStore):
        raise KernelEvidenceError("artifacts must be an ArtifactStore")
    try:
        return artifacts.put_bytes(
            artifact.content_bytes(), media_type="application/json"
        )
    except ArtifactStoreError as exc:
        raise KernelEvidenceError(f"cannot persist kernel artifact: {exc}") from exc


def load_kernel_artifact(
    content_hash: str, artifacts: ArtifactStore
) -> KernelCheckArtifact:
    content_hash = _require_hex64("content_hash", content_hash)
    if not isinstance(artifacts, ArtifactStore):
        raise KernelEvidenceError("artifacts must be an ArtifactStore")
    try:
        artifacts.verify(content_hash)
        blob = artifacts.get_bytes(content_hash)
    except ArtifactStoreError as exc:
        raise KernelEvidenceError(f"kernel artifact missing or tampered: {exc}") from exc
    try:
        payload = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KernelEvidenceError("kernel artifact is not JSON") from exc
    return KernelCheckArtifact.from_dict(payload)


def run_kernel_check(
    request: KernelCheckRequest,
    *,
    transport: KernelCheckerTransport | None = None,
    artifacts: ArtifactStore | None = None,
) -> KernelCheckArtifact:
    """Run a kernel check and optionally persist the immutable artifact."""
    if not isinstance(request, KernelCheckRequest):
        raise KernelEvidenceError("request must be a KernelCheckRequest")
    bus = transport if transport is not None else LeanLakeTransport()
    artifact = bus.check(request)
    if not isinstance(artifact, KernelCheckArtifact):
        raise KernelEvidenceError("kernel transport must return KernelCheckArtifact")
    artifact = _coerce_untrusted_pass(artifact, request)
    if artifacts is not None:
        persist_kernel_artifact(artifact, artifacts)
    return artifact


def attest_statement_alignment(
    *,
    version: int,
    contract: ProblemContract,
    obligation: Obligation,
    kernel_artifact: KernelCheckArtifact,
    auditor_id: str,
    auditor_version: str,
    aligned: bool,
    created_at: str,
    notes: str = "",
    kernel_artifact_hash: str | None = None,
) -> StatementAlignmentAttestation:
    """Main/auditor attestation. Compiler success cannot set aligned=True alone."""
    if not isinstance(contract, ProblemContract):
        raise KernelEvidenceError("contract must be a ProblemContract")
    if not isinstance(obligation, Obligation):
        raise KernelEvidenceError("obligation must be an Obligation")
    if not isinstance(kernel_artifact, KernelCheckArtifact):
        raise KernelEvidenceError("kernel_artifact must be a KernelCheckArtifact")
    if obligation.contract_id != contract.contract_id:
        raise KernelEvidenceError("obligation is not bound to the contract")
    if kernel_artifact.contract_id != contract.contract_id:
        raise KernelEvidenceError("kernel artifact contract_id mismatch")
    if kernel_artifact.obligation_id != obligation.obligation_id:
        raise KernelEvidenceError("kernel artifact obligation_id mismatch")
    if kernel_artifact.exact_claim != obligation.exact_statement:
        raise KernelEvidenceError("kernel artifact claim does not match the selected obligation")
    if kernel_artifact_hash is None:
        kernel_artifact_hash = sha256_hex(kernel_artifact.content_bytes())
    return StatementAlignmentAttestation.build(
        version=version,
        contract_id=contract.contract_id,
        obligation_id=obligation.obligation_id,
        exact_claim=obligation.exact_statement,
        formal_statement=kernel_artifact.theorem_type,
        theorem_name=kernel_artifact.theorem_name,
        module_name=kernel_artifact.module_name,
        kernel_artifact_hash=kernel_artifact_hash,
        auditor_id=auditor_id,
        auditor_version=auditor_version,
        aligned=aligned,
        created_at=created_at,
        notes=notes,
    )


def build_kernel_checked_evidence(
    *,
    kernel_artifact: KernelCheckArtifact,
    alignment: StatementAlignmentAttestation,
    contract: ProblemContract,
    obligation: Obligation,
    artifacts: ArtifactStore,
    producer: str,
    created_at: str,
    scope: MathematicalScope | None = None,
) -> EvidenceArtifact:
    """Lift a PASS kernel artifact plus aligned attestation into KERNEL evidence.

    Does not register the artifact on ResearchView; parent wires registration.
    """
    if not isinstance(kernel_artifact, KernelCheckArtifact):
        raise KernelEvidenceError("kernel_artifact must be a KernelCheckArtifact")
    if not isinstance(alignment, StatementAlignmentAttestation):
        raise KernelEvidenceError("alignment must be a StatementAlignmentAttestation")
    if not isinstance(contract, ProblemContract):
        raise KernelEvidenceError("contract must be a ProblemContract")
    if not isinstance(obligation, Obligation):
        raise KernelEvidenceError("obligation must be an Obligation")
    if not isinstance(artifacts, ArtifactStore):
        raise KernelEvidenceError("artifacts must be an ArtifactStore")
    producer = _require_nonempty("producer", producer)
    _parse_utc("created_at", created_at)
    if kernel_artifact.status is not KernelCheckStatus.PASS:
        raise KernelEvidenceError("KERNEL_CHECKED_PROOF requires a PASS kernel artifact")
    if not alignment.aligned:
        raise KernelEvidenceError(
            "compiler success cannot certify natural-language equivalence"
        )
    if alignment.version < 1:
        raise KernelEvidenceError("alignment attestation must be versioned")
    if alignment.contract_id != contract.contract_id:
        raise KernelEvidenceError("alignment contract_id mismatch")
    if alignment.obligation_id != obligation.obligation_id:
        raise KernelEvidenceError("alignment obligation_id mismatch")
    if alignment.exact_claim != obligation.exact_statement:
        raise KernelEvidenceError("alignment does not match the selected obligation statement")
    if alignment.theorem_name != kernel_artifact.theorem_name:
        raise KernelEvidenceError("alignment theorem_name mismatch")
    if alignment.module_name != kernel_artifact.module_name:
        raise KernelEvidenceError("alignment module_name mismatch")
    if alignment.formal_statement != kernel_artifact.theorem_type:
        raise KernelEvidenceError("alignment formal_statement mismatch")
    if kernel_artifact.contract_id != contract.contract_id:
        raise KernelEvidenceError("kernel artifact contract_id mismatch")
    if kernel_artifact.obligation_id != obligation.obligation_id:
        raise KernelEvidenceError("kernel artifact obligation_id mismatch")
    if kernel_artifact.exact_claim != obligation.exact_statement:
        raise KernelEvidenceError("kernel artifact claim does not match the selected obligation")
    _require_trusted_execution(kernel_artifact)
    if scope is not None and scope != obligation.scope:
        raise KernelEvidenceError("explicit scope must match the selected obligation")
    ref = persist_kernel_artifact(kernel_artifact, artifacts)
    if alignment.kernel_artifact_hash != ref.content_hash:
        raise KernelEvidenceError("alignment kernel_artifact_hash mismatch")
    align_ref = artifacts.put_bytes(
        canonical_json(alignment.to_dict()).encode("utf-8"),
        media_type="application/json",
    )
    env_hash = sha256_hex(
        canonical_json(
            {
                "checker_id": kernel_artifact.checker_id,
                "checker_version": kernel_artifact.checker_version,
                "toolchain": kernel_artifact.toolchain,
                "lean_githash": kernel_artifact.lean_githash,
                "source_manifest": kernel_artifact.source_manifest.manifest_hash,
            }
        ).encode("utf-8")
    )
    used_scope = obligation.scope if scope is None else scope
    return EvidenceArtifact.build(
        content_hash=ref.content_hash,
        producer=producer,
        contract_id=contract.contract_id,
        evidence_kind=EvidenceKind.KERNEL_CHECKED_PROOF,
        exact_claim=obligation.exact_statement,
        scope=used_scope,
        input_hashes=(ref.content_hash, align_ref.content_hash),
        environment_hash=env_hash,
        checker_id=kernel_artifact.checker_id,
        checker_version=kernel_artifact.checker_version,
        checker_result=CheckerResult.PASS,
        created_at=created_at,
    )


@dataclass(frozen=True, slots=True)
class _Proc:
    status: KernelCheckStatus
    stdout: str
    stderr: str
    detail: str
    returncode: int


def _require_trusted_execution(artifact: KernelCheckArtifact) -> None:
    if artifact.provenance_kind != TRUSTED_PROVENANCE:
        raise KernelEvidenceError(
            "KERNEL_CHECKED_PROOF requires lake-env checker provenance"
        )
    if artifact.checker_id != _CHECKER_ID:
        raise KernelEvidenceError("KERNEL_CHECKED_PROOF requires the Euler Lean checker")
    if artifact.checker_version != _CHECKER_VERSION:
        raise KernelEvidenceError("kernel artifact requires rechecking with the current checker version")
    def check_stage(output: str) -> str:
        if output.startswith("[githash]"):
            return output.partition("\n[check] ")[2]
        return output
    parsed = _parse_lean_check(check_stage(artifact.stdout) + "\n" + check_stage(artifact.stderr), artifact.theorem_name)
    if (not parsed.get("axioms_found")
            or not parsed.get("structured") or parsed.get("is_proposition") is not True
            or _normalize_type(parsed.get("theorem_type", "")) != _normalize_type(artifact.theorem_type)
            or set(parsed.get("axioms", ())) != set(artifact.axioms)):
        raise KernelEvidenceError("recorded theorem/axioms do not match the complete checker output")
    if not artifact.lean_githash:
        raise KernelEvidenceError("KERNEL_CHECKED_PROOF requires an isolated lake-env lean githash")
    lake_name = Path(artifact.lake_executable or "").name
    if lake_name not in _LAKE_NAMES:
        raise KernelEvidenceError(
            "KERNEL_CHECKED_PROOF requires a recorded lake executable; "
            "arbitrary checker binaries are not trusted"
        )
    lake_path = Path(artifact.lake_executable)
    if not lake_path.is_file():
        raise KernelEvidenceError("recorded lake executable is missing")
    if artifact.lake_executable_digest:
        digest = sha256_hex(lake_path.read_bytes())
        if digest != artifact.lake_executable_digest:
            raise KernelEvidenceError("recorded lake executable digest does not match")
    if artifact.build_status != KernelCheckStatus.PASS.value or artifact.build_returncode not in (None, 0):
        raise KernelEvidenceError("KERNEL_CHECKED_PROOF requires a successful lake build stage")
    if artifact.check_status != KernelCheckStatus.PASS.value or artifact.check_returncode not in (None, 0):
        raise KernelEvidenceError("KERNEL_CHECKED_PROOF requires a successful target check stage")


def require_trusted_kernel_artifact(artifact: KernelCheckArtifact) -> None:
    """Validate current checker provenance and its complete target/axiom report."""
    _assert_pass_is_honest(artifact)
    _require_trusted_execution(artifact)


def _combine_stage_outcomes(
    build: "_Proc",
    check: "_Proc",
    judged: KernelCheckStatus,
    judged_detail: str,
) -> tuple[KernelCheckStatus, str]:
    if build.status is KernelCheckStatus.INFRA:
        return KernelCheckStatus.INFRA, f"build: {build.detail}"
    if check.status is KernelCheckStatus.INFRA:
        return KernelCheckStatus.INFRA, f"check: {check.detail}"
    if build.returncode != 0 and check.returncode == 0:
        return (
            KernelCheckStatus.FAIL,
            "inconsistent: lake build failed but the target probe succeeded",
        )
    if build.returncode != 0:
        return KernelCheckStatus.FAIL, f"lake build failed: {build.detail}"
    if judged is KernelCheckStatus.PASS and check.returncode != 0:
        return (
            KernelCheckStatus.FAIL,
            "inconsistent: target check failed after a passing judgment",
        )
    return judged, judged_detail


def _judge_from_check(
    request: KernelCheckRequest, check: "_Proc"
) -> tuple[KernelCheckStatus, str, str, tuple[str, ...]]:
    parsed = _parse_lean_check(check.stdout + "\n" + check.stderr, request.theorem_name)
    return _judge_target(
        request,
        parsed,
        compile_ok=check.returncode == 0,
        proc_detail=check.detail,
    )


def _judge_target(
    request: KernelCheckRequest,
    parsed: dict[str, Any],
    *,
    compile_ok: bool,
    proc_detail: str,
) -> tuple[KernelCheckStatus, str, str, tuple[str, ...]]:
    if not parsed.get("found"):
        return (
            KernelCheckStatus.FAIL,
            "target theorem was not checked; lake/lean success without the named theorem does not count",
            "",
            (),
        )
    if not parsed.get("axioms_found"):
        return (KernelCheckStatus.FAIL, "missing or ambiguous complete axiom report", "", ())
    if not parsed.get("structured"):
        return (KernelCheckStatus.FAIL, "missing structured Lean checker receipt", "", ())
    if parsed.get("is_proposition") is not True:
        return (KernelCheckStatus.FAIL, "named declaration type is not a proposition", str(parsed.get("theorem_type", "")), ())
    theorem_type = str(parsed.get("theorem_type") or "")
    axioms = tuple(parsed.get("axioms") or ())
    if _normalize_type(theorem_type) != _normalize_type(request.expected_type):
        return (
            KernelCheckStatus.FAIL,
            "checked theorem type does not match the expected type",
            theorem_type,
            axioms,
        )
    if not compile_ok:
        return (
            KernelCheckStatus.FAIL,
            proc_detail or "lean returned a nonzero status for the target check",
            theorem_type,
            axioms,
        )
    approved = {normalize_axiom_name(item) for item in request.approved_axioms}
    for axiom in axioms:
        norm = normalize_axiom_name(axiom)
        if norm in {normalize_axiom_name(item) for item in FORBIDDEN_AXIOM_NAMES}:
            return (
                KernelCheckStatus.FAIL,
                f"forbidden axiom {axiom}",
                theorem_type,
                axioms,
            )
        if norm and norm not in approved:
            return (
                KernelCheckStatus.FAIL,
                f"unapproved axiom {axiom}",
                theorem_type,
                axioms,
            )
    return (KernelCheckStatus.PASS, "target theorem kernel-checked", theorem_type, axioms)


def _parse_lean_check(text: str, theorem_name: str) -> dict[str, Any]:
    prefix = "EULER_KERNEL_RESULT "
    frames = [line[len(prefix):] for line in text.splitlines() if line.startswith(prefix)]
    if frames:
        try:
            if len(frames) != 1:
                raise ValueError("ambiguous checker receipts")
            record = json.loads(frames[0])
            if (not isinstance(record, dict) or record.get("name") != theorem_name
                    or not isinstance(record.get("type"), str)
                    or not isinstance(record.get("axioms"), list)
                    or not all(isinstance(name, str) and name for name in record["axioms"])
                    or not isinstance(record.get("isProp"), bool)):
                raise ValueError("invalid checker receipt")
            return {"found": True, "theorem_type": record["type"], "axioms": tuple(record["axioms"]),
                    "axioms_found": True, "is_proposition": record["isProp"], "structured": True}
        except (ValueError, TypeError):
            return {"found": False, "theorem_type": "", "axioms": (), "axioms_found": False}
    # Lean's pretty printer wraps both types and axiom lists. Missing output is
    # unknown, never the empty axiom set. Reject duplicated reports rather than
    # selecting an arbitrary occurrence from import diagnostics.
    types = list(re.finditer(r"(?m)^" + re.escape(theorem_name) + r"(?:\.\{[^}\n]*\})?\s*:\s*", text))
    reports = [(m, ()) for m in _AXIOM_NONE_RE.finditer(text) if m.group(1) == theorem_name]
    reports += [(m, tuple(part.strip() for part in m.group(2).split(",") if part.strip()))
                for m in _AXIOM_LIST_RE.finditer(text) if m.group(1) == theorem_name]
    valid = len(types) == 1 and len(reports) == 1 and types[0].end() < reports[0][0].start()
    return {"found": len(types) == 1,
            "theorem_type": text[types[0].end():reports[0][0].start()].strip() if valid else "",
            "axioms": reports[0][1] if valid else (), "axioms_found": valid}


def _coerce_untrusted_pass(
    artifact: KernelCheckArtifact, request: KernelCheckRequest
) -> KernelCheckArtifact:
    if artifact.contract_id != request.contract_id:
        raise KernelEvidenceError("kernel artifact is not bound to the contract")
    if artifact.obligation_id != request.obligation_id:
        raise KernelEvidenceError("kernel artifact is not bound to the obligation")
    if artifact.exact_claim != request.exact_claim:
        raise KernelEvidenceError("kernel artifact claim does not match the request")
    if artifact.theorem_name != request.theorem_name:
        raise KernelEvidenceError("kernel artifact theorem_name mismatch")
    if artifact.module_name != request.module_name:
        raise KernelEvidenceError("kernel artifact module_name mismatch")
    if artifact.status is not KernelCheckStatus.PASS:
        return artifact
    if not artifact.theorem_type:
        raise KernelEvidenceError("PASS kernel artifact is missing the theorem type")
    if _normalize_type(artifact.theorem_type) != _normalize_type(request.expected_type):
        raise KernelEvidenceError("PASS kernel artifact has the wrong theorem type")
    approved = {normalize_axiom_name(item) for item in request.approved_axioms}
    for axiom in artifact.axioms:
        norm = normalize_axiom_name(axiom)
        if norm in {normalize_axiom_name(item) for item in FORBIDDEN_AXIOM_NAMES}:
            raise KernelEvidenceError("PASS kernel artifact uses sorryAx")
        if norm and norm not in approved:
            raise KernelEvidenceError(f"PASS kernel artifact uses unapproved axiom {axiom}")
    return artifact


def _assert_pass_is_honest(artifact: KernelCheckArtifact) -> None:
    if artifact.status is not KernelCheckStatus.PASS:
        return
    if not artifact.theorem_name or not artifact.theorem_type:
        raise KernelEvidenceError("PASS requires a named theorem and its type")
    for axiom in artifact.axioms:
        norm = normalize_axiom_name(axiom)
        if norm in {normalize_axiom_name(item) for item in FORBIDDEN_AXIOM_NAMES}:
            raise KernelEvidenceError("PASS cannot include sorryAx")
        if norm and norm not in {
            normalize_axiom_name(item) for item in APPROVED_FOUNDATIONAL_AXIOMS
        }:
            raise KernelEvidenceError(
                f"PASS cannot include unapproved axiom {axiom}"
            )


def _hash_project_sources(
    root: Path,
    *,
    only_paths: tuple[str, ...] | None = None,
    dependency_fingerprint: Optional[str] = None,
) -> KernelSourceManifest:
    toolchain = _read_toolchain_pin(root)
    lakefile = root / "lakefile.toml"
    if not lakefile.is_file():
        lakefile = root / "lakefile.lean"
    if not lakefile.is_file():
        raise KernelEvidenceError("Lean project is missing lakefile.toml or lakefile.lean")
    lakefile_hash = sha256_hex(lakefile.read_bytes())
    manifest_file = root / "lake-manifest.json"
    lake_manifest_hash = (
        sha256_hex(manifest_file.read_bytes())
        if manifest_file.is_file() and (
            only_paths is None or "lake-manifest.json" in only_paths
        )
        else None
    )
    sources: list[tuple[str, str]] = []
    allowed = None if only_paths is None else set(only_paths)
    for path in _iter_isolated_files(root):
        rel = path.relative_to(root).as_posix()
        if rel == _DRIVER_NAME:
            continue
        if allowed is not None and rel not in allowed:
            continue
        sources.append((rel, sha256_hex(path.read_bytes())))
    lean_sources = [item for item in sources if item[0].endswith(".lean")]
    if not lean_sources:
        raise KernelEvidenceError("Lean project contains no source .lean files")
    return KernelSourceManifest.build(
        toolchain=toolchain,
        lakefile_hash=lakefile_hash,
        lake_manifest_hash=lake_manifest_hash,
        source_hashes=tuple(sources),
        dependency_fingerprint=dependency_fingerprint,
    )


def assert_contained_source(root: Path, path: Path) -> Path:
    """Resolve ``path`` and require it stay inside ``root``. Reject escapes."""
    root_res = root.expanduser().resolve()
    candidate = path if path.is_absolute() else (root_res / path)
    if candidate.is_symlink() or candidate.exists():
        resolved = candidate.resolve()
    else:
        resolved = candidate
    try:
        resolved.relative_to(root_res)
    except ValueError as exc:
        raise KernelEvidenceError(
            f"source path escapes project root: {path}"
        ) from exc
    return resolved


def _iter_isolated_files(root: Path) -> list[Path]:
    found: list[Path] = []
    root_res = root.resolve()
    for path in sorted(root.rglob("*")):
        if not path.is_file() and not path.is_symlink():
            continue
        rel = path.relative_to(root)
        if any(part in _SKIP_COPY_DIRS for part in rel.parts):
            continue
        if path.name in _PIN_FILES or path.suffix == ".lean":
            if path.is_symlink():
                assert_contained_source(root_res, path)
            found.append(path)
    return found


def _copy_isolated(src: Path, dest: Path) -> None:
    src_res = src.resolve()
    dest.mkdir(parents=True)
    for path in _iter_isolated_files(src):
        rel = path.relative_to(src)
        if path.is_symlink():
            try:
                path.resolve().relative_to(src_res)
            except ValueError as exc:
                raise KernelEvidenceError(
                    f"symlink escapes project root: {rel.as_posix()}"
                ) from exc
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target, follow_symlinks=True)


def _assert_readable_project(root: Path) -> None:
    if not root.is_dir():
        raise KernelEvidenceError(f"Lean project directory does not exist: {root}")
    if not (root / "lean-toolchain").is_file():
        raise KernelEvidenceError("Lean project is missing lean-toolchain")


def _read_toolchain_pin(root: Path) -> str:
    path = root / "lean-toolchain"
    if not path.is_file():
        raise KernelEvidenceError("Lean project is missing lean-toolchain")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise KernelEvidenceError("lean-toolchain is empty")
    return text


def _resolve_lake_executable(configured: Optional[str]) -> tuple[Path, str]:
    raw = configured or shutil.which("lake")
    if not raw:
        raise KernelEvidenceError("lake executable is not available")
    path = Path(raw).expanduser()
    if not path.is_file():
        located = shutil.which(raw)
        if located:
            path = Path(located)
    path = path.resolve()
    if path.name not in _LAKE_NAMES:
        raise KernelEvidenceError(
            "configured checker must be named lake; arbitrary binaries are not trusted"
        )
    digest = sha256_hex(path.read_bytes())
    return path, digest


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _lake_env_lean_githash(
    lake: Path, cwd: Path, deadline: float
) -> tuple[Optional[str], Optional["_Proc"]]:
    remaining = _remaining(deadline)
    if remaining <= 0:
        return None, _Proc(KernelCheckStatus.INFRA, "", "", "timed out before githash", -1)
    proc = _run_bounded([str(lake), "env", "lean", "--githash"], cwd, remaining)
    if proc.returncode != 0:
        return None, proc
    text = (proc.stdout or "").strip().split()
    return (text[0] if text else None), proc


def _stage_artifact(
    request: KernelCheckRequest,
    *,
    status: KernelCheckStatus,
    toolchain: str,
    githash: Optional[str],
    lake_path: Path,
    lake_digest: str,
    manifest: KernelSourceManifest,
    command: tuple[str, ...],
    stdout: str,
    stderr: str,
    detail: str,
    created_at: str,
    build: "_Proc",
    check: "_Proc",
) -> KernelCheckArtifact:
    return KernelCheckArtifact.build(
        status=status,
        checker_id=_CHECKER_ID,
        checker_version=_CHECKER_VERSION,
        toolchain=toolchain,
        lean_githash=githash,
        module_name=request.module_name,
        theorem_name=request.theorem_name,
        theorem_type="",
        axioms=(),
        source_manifest=manifest,
        contract_id=request.contract_id,
        obligation_id=request.obligation_id,
        exact_claim=request.exact_claim,
        command=command,
        stdout=stdout,
        stderr=stderr,
        detail=detail,
        created_at=created_at,
        provenance_kind=TRUSTED_PROVENANCE,
        lake_executable=str(lake_path),
        lake_executable_digest=lake_digest,
        build_status=build.status.value,
        check_status=check.status.value,
        build_returncode=build.returncode,
        check_returncode=check.returncode,
    )


def _timeout_artifact(
    request: KernelCheckRequest,
    lake_path: Path,
    lake_digest: str,
    isolated: Path,
    created_at: str,
    stage: str,
) -> KernelCheckArtifact:
    try:
        manifest = _hash_project_sources(isolated)
        toolchain = manifest.toolchain
    except KernelEvidenceError:
        toolchain = "unknown"
        manifest = KernelSourceManifest.build(
            toolchain=toolchain,
            lakefile_hash=sha256_hex(b"missing"),
            source_hashes=(("missing.lean", sha256_hex(b"missing")),),
        )
    return _stage_artifact(
        request,
        status=KernelCheckStatus.INFRA,
        toolchain=toolchain,
        githash=None,
        lake_path=lake_path,
        lake_digest=lake_digest,
        manifest=manifest,
        command=(str(lake_path),),
        stdout="",
        stderr="",
        detail=f"timed out during {stage}",
        created_at=created_at,
        build=_Proc(KernelCheckStatus.INFRA, "", "", f"timed out during {stage}", -1),
        check=_Proc(KernelCheckStatus.INFRA, "", "", "not run", -1),
    )


def _driver_source(module_name: str, theorem_name: str) -> str:
    return (
        "import Lean\n"
        f"import {module_name}\n"
        "open Lean Elab Command in\n"
        "run_cmd do\n"
        f"  let n := `{theorem_name}\n"
        "  let info ← Lean.getConstInfo n\n"
        "  let printed ← Lean.Elab.Command.liftTermElabM (Lean.Meta.ppExpr info.type)\n"
        "  let proposition ← Lean.Elab.Command.liftTermElabM (Lean.Meta.isProp info.type)\n"
        "  let axioms ← Lean.collectAxioms n\n"
        '  let result := Lean.Json.mkObj [("name", toJson n.toString), ("type", toJson printed.pretty), '
        '("axioms", toJson (axioms.map Lean.Name.toString)), ("isProp", toJson proposition)]\n'
        '  liftIO <| IO.println ("EULER_KERNEL_RESULT " ++ result.compress)\n'
    )


def _run_bounded(command: list[str], cwd: Path, timeout_s: float) -> _Proc:
    if timeout_s <= 0:
        return _Proc(
            status=KernelCheckStatus.INFRA,
            stdout="",
            stderr="",
            detail="timed out before command start",
            returncode=-1,
        )
    env = dict(os.environ)
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        return _Proc(
            status=KernelCheckStatus.INFRA,
            stdout="",
            stderr=str(exc),
            detail=f"missing executable: {command[0]}",
            returncode=127,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return _Proc(
            status=KernelCheckStatus.INFRA,
            stdout=stdout,
            stderr=stderr,
            detail="timed out under the remaining monotonic budget",
            returncode=-1,
        )
    except OSError as exc:
        return _Proc(
            status=KernelCheckStatus.INFRA,
            stdout="",
            stderr=str(exc),
            detail=f"os error running {' '.join(command)}: {exc}",
            returncode=127,
        )
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if completed.returncode != 0:
        return _Proc(
            status=KernelCheckStatus.FAIL,
            stdout=stdout,
            stderr=stderr,
            detail=f"command exited {completed.returncode}",
            returncode=completed.returncode,
        )
    return _Proc(
        status=KernelCheckStatus.PASS,
        stdout=stdout,
        stderr=stderr,
        detail="ok",
        returncode=0,
    )


def _normalize_type(value: str) -> str:
    return " ".join(str(value or "").split())


def _artifact_id_from_record(artifact: KernelCheckArtifact) -> str:
    return stable_id(
        _ARTIFACT_DOMAIN,
        _ID_VERSION,
        canonical_json(
            _artifact_payload(
                status=artifact.status,
                checker_id=artifact.checker_id,
                checker_version=artifact.checker_version,
                toolchain=artifact.toolchain,
                lean_githash=artifact.lean_githash,
                module_name=artifact.module_name,
                theorem_name=artifact.theorem_name,
                theorem_type=artifact.theorem_type,
                axioms=artifact.axioms,
                source_manifest=artifact.source_manifest,
                contract_id=artifact.contract_id,
                obligation_id=artifact.obligation_id,
                exact_claim=artifact.exact_claim,
                command=artifact.command,
                detail=artifact.detail,
                provenance_kind=artifact.provenance_kind,
                lake_executable=artifact.lake_executable,
                lake_executable_digest=artifact.lake_executable_digest,
                build_status=artifact.build_status,
                check_status=artifact.check_status,
            )
        ),
    )


def _artifact_payload(
    *,
    status: KernelCheckStatus,
    checker_id: str,
    checker_version: str,
    toolchain: str,
    lean_githash: Optional[str],
    module_name: str,
    theorem_name: str,
    theorem_type: str,
    axioms: tuple[str, ...],
    source_manifest: KernelSourceManifest,
    contract_id: str,
    obligation_id: str,
    exact_claim: str,
    command: tuple[str, ...],
    detail: str,
    provenance_kind: str = UNTRUSTED_PROVENANCE,
    lake_executable: Optional[str] = None,
    lake_executable_digest: Optional[str] = None,
    build_status: Optional[str] = None,
    check_status: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "status": status.value if isinstance(status, KernelCheckStatus) else str(status),
        "checker_id": checker_id,
        "checker_version": checker_version,
        "toolchain": toolchain,
        "lean_githash": lean_githash,
        "module_name": module_name,
        "theorem_name": theorem_name,
        "theorem_type": theorem_type,
        "axioms": list(axioms),
        "source_manifest": source_manifest.manifest_hash,
        "contract_id": contract_id,
        "obligation_id": obligation_id,
        "exact_claim": exact_claim,
        "command": list(command),
        "detail": detail,
        "provenance_kind": provenance_kind,
        "lake_executable": lake_executable,
        "lake_executable_digest": lake_executable_digest,
        "build_status": build_status,
        "check_status": check_status,
    }


def _alignment_id_from_record(item: StatementAlignmentAttestation) -> str:
    return stable_id(
        _ALIGN_DOMAIN,
        _ID_VERSION,
        canonical_json(
            _alignment_payload(
                version=item.version,
                contract_id=item.contract_id,
                obligation_id=item.obligation_id,
                exact_claim=item.exact_claim,
                formal_statement=item.formal_statement,
                theorem_name=item.theorem_name,
                module_name=item.module_name,
                kernel_artifact_hash=item.kernel_artifact_hash,
                auditor_id=item.auditor_id,
                auditor_version=item.auditor_version,
                aligned=item.aligned,
                notes=item.notes,
            )
        ),
    )


def _alignment_payload(
    *,
    version: int,
    contract_id: str,
    obligation_id: str,
    exact_claim: str,
    formal_statement: str,
    theorem_name: str,
    module_name: str,
    kernel_artifact_hash: str,
    auditor_id: str,
    auditor_version: str,
    aligned: bool,
    notes: str,
) -> dict[str, Any]:
    return {
        "version": version,
        "contract_id": contract_id,
        "obligation_id": obligation_id,
        "exact_claim": exact_claim,
        "formal_statement": formal_statement,
        "theorem_name": theorem_name,
        "module_name": module_name,
        "kernel_artifact_hash": kernel_artifact_hash,
        "auditor_id": auditor_id,
        "auditor_version": auditor_version,
        "aligned": aligned,
        "notes": notes,
    }


def _require_nonempty(name: str, value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise KernelEvidenceError(f"{name} must be a nonempty str")
    return value


def _require_ident(name: str, value: object) -> str:
    text = _require_nonempty(name, value)
    if any(ch.isspace() for ch in text) or ".." in text:
        raise KernelEvidenceError(f"{name} must be a Lean identifier")
    return text


def _require_hex64(name: str, value: object) -> str:
    text = _require_nonempty(name, value)
    if _HEX64_RE.fullmatch(text) is None:
        raise KernelEvidenceError(f"{name} must be 64-char lowercase hex")
    return text


def _require_positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise KernelEvidenceError(f"{name} must be a positive int")
    return value


def _require_enum(enum_cls: type[Enum], value: object, name: str) -> Any:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            raise KernelEvidenceError(f"{name} has unknown value {value!r}") from None
    raise KernelEvidenceError(f"{name} must be {enum_cls.__name__}")


def _require_str_tuple(name: str, value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise KernelEvidenceError(f"{name} must be a list of str")
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or item == "":
            raise KernelEvidenceError(f"{name}[{index}] must be a nonempty str")
        out.append(item)
    return tuple(out)


def _require_source_pairs(value: object) -> tuple[tuple[str, str], ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise KernelEvidenceError("source_hashes must be a list of pairs")
    out: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise KernelEvidenceError(f"source_hashes[{index}] must be a pair")
        name = _require_nonempty(f"source_hashes[{index}].path", item[0])
        digest = _require_hex64(f"source_hashes[{index}].hash", item[1])
        out.append((name, digest))
    return tuple(out)


def _parse_utc(name: str, value: object) -> datetime:
    if not isinstance(value, str) or value == "":
        raise KernelEvidenceError(f"{name} must be ISO-8601 UTC")
    parsed = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(parsed)
    except ValueError as exc:
        raise KernelEvidenceError(f"{name} must be ISO-8601 UTC") from exc
    if dt.tzinfo is None:
        raise KernelEvidenceError(f"{name} must be UTC")
    return dt


def _mapping(label: str, data: object) -> Mapping[str, Any]:
    if not isinstance(data, Mapping) or isinstance(data, (str, bytes)):
        raise KernelEvidenceError(f"{label} must be a JSON object")
    return data


def _reject_extra(payload: Mapping[str, Any], allowed: set[str], label: str) -> None:
    extra = set(payload) - allowed
    if extra:
        raise KernelEvidenceError(f"{label} has unknown fields: {sorted(extra)}")


def _require_keys(payload: Mapping[str, Any], required: set[str], label: str) -> None:
    missing = required - set(payload)
    if missing:
        raise KernelEvidenceError(f"{label} missing fields: {sorted(missing)}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
