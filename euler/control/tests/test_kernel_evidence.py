"""Kernel evidence adapter tests: negatives, alignment, one real Lean check."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

from euler.control.artifacts import ArtifactStore
from euler.control.contracts import (
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
    QuantifierBinding,
    QuantifierKind,
    TargetKind,
)
from euler.control.ids import sha256_hex
from euler.control.kernel_evidence import (
    APPROVED_FOUNDATIONAL_AXIOMS,
    InjectedKernelTransport,
    KernelCheckArtifact,
    KernelCheckRequest,
    KernelCheckStatus,
    KernelEvidenceError,
    KernelSourceManifest,
    LeanLakeTransport,
    TRUSTED_PROVENANCE,
    UNTRUSTED_PROVENANCE,
    _Proc,
    _combine_stage_outcomes,
    attest_statement_alignment,
    build_kernel_checked_evidence,
    persist_kernel_artifact,
    run_kernel_check,
)
from euler.control.obligations import Obligation

_TS = "2026-01-01T00:00:00Z"
_CLAIM = "two plus two equals four"
_CHILD_CLAIM = "two plus two equals four as a local lemma"
_TYPE = "2 + 2 = 4"
_TOOLCHAIN = os.environ.get("EULER_TEST_LEAN_TOOLCHAIN", "leanprover/lean4:v4.34.1")


def _hex(*parts: str) -> str:
    return sha256_hex("|".join(parts))


def _scope() -> MathematicalScope:
    return MathematicalScope.build(
        assumptions=(),
        quantifiers=(
            QuantifierBinding(variable="n", kind=QuantifierKind.FORALL, domain="N"),
        ),
        objects=(),
        parameter_domain="N",
        definitions_hash=_hex("defs"),
    )


def _contract(*, statement: str = _CLAIM) -> ProblemContract:
    return ProblemContract.build(
        problem_id="two-plus-two",
        version=1,
        exact_statement=statement,
        scope=_scope(),
        permitted_tools=("lean",),
        external_help_policy="none",
        target_kind=TargetKind.FORMALIZATION,
        required_evidence_level=EvidenceKind.KERNEL_CHECKED_PROOF,
        completion_checks=("kernel checked",),
        budget_policy_ref="default-budget",
    )


def _obligation(contract: ProblemContract, *, statement: str | None = None) -> Obligation:
    return Obligation.build(
        contract_id=contract.contract_id,
        exact_statement=contract.exact_statement if statement is None else statement,
        scope=contract.scope,
        desired_output="KERNEL_CHECKED_PROOF",
        created_by="researcher",
        version=1,
    )


def _write_lean_project(root: Path, source: str, *, name: str = "TinyKernel") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "lean-toolchain").write_text(_TOOLCHAIN + "\n", encoding="utf-8")
    (root / "lakefile.toml").write_text(
        (
            f'name = "{name.lower()}"\n'
            f'version = "0.1.0"\n'
            f'defaultTargets = ["{name}"]\n'
            f"[[lean_lib]]\n"
            f'name = "{name}"\n'
        ),
        encoding="utf-8",
    )
    (root / f"{name}.lean").write_text(source, encoding="utf-8")
    return root


def _request(contract, obligation, project_dir: Path, **overrides) -> KernelCheckRequest:
    kwargs = {
        "project_dir": str(project_dir),
        "module_name": "TinyKernel",
        "theorem_name": "two_add_two",
        "expected_type": _TYPE,
        "contract_id": contract.contract_id,
        "obligation_id": obligation.obligation_id,
        "exact_claim": obligation.exact_statement,
        "timeout_ms": 60_000,
    }
    lake = os.environ.get("EULER_TEST_LAKE") or shutil.which("lake")
    if lake:
        kwargs["lake_bin"] = lake
    kwargs.update(overrides)
    return KernelCheckRequest(**kwargs)


def _manifest() -> KernelSourceManifest:
    return KernelSourceManifest.build(
        toolchain=_TOOLCHAIN,
        lakefile_hash=_hex("lake"),
        source_hashes=(("TinyKernel.lean", _hex("src")),),
    )


def _artifact(contract, obligation, *, status=KernelCheckStatus.PASS, **fields) -> KernelCheckArtifact:
    base = {
        "status": status,
        "checker_id": "euler-lean-kernel",
        "checker_version": "1",
        "toolchain": _TOOLCHAIN,
        "module_name": "TinyKernel",
        "theorem_name": "two_add_two",
        "theorem_type": _TYPE if status is KernelCheckStatus.PASS else "",
        "axioms": (),
        "source_manifest": _manifest(),
        "contract_id": contract.contract_id,
        "obligation_id": obligation.obligation_id,
        "exact_claim": obligation.exact_statement,
        "command": ("lake", "env", "lean", "_EulerKernelCheck.lean"),
        "stdout": f"two_add_two : {_TYPE}\n",
        "stderr": "",
        "detail": "ok",
        "created_at": _TS,
        "provenance_kind": UNTRUSTED_PROVENANCE,
    }
    base.update(fields)
    return KernelCheckArtifact.build(**base)


def test_supplied_pass_json_is_not_kernel_evidence():
    contract = _contract()
    obligation = _obligation(contract)
    with tempfile.TemporaryDirectory() as raw:
        root = _write_lean_project(
            Path(raw) / "proj", "theorem two_add_two : 2 + 2 = 4 := rfl\n"
        )
        (root / "RESULT.json").write_text(
            json.dumps({"status": "PASS", "theorem": "two_add_two"}), encoding="utf-8"
        )

        class _JsonTransport:
            def check(self, request):
                return json.loads((root / "RESULT.json").read_text(encoding="utf-8"))

        try:
            run_kernel_check(_request(contract, obligation, root), transport=_JsonTransport())
            raise AssertionError("PASS JSON must not count")
        except KernelEvidenceError:
            pass


def test_broad_axiom_whitelist_is_rejected():
    contract = _contract()
    obligation = _obligation(contract)
    try:
        KernelCheckRequest(
            project_dir="/tmp/x",
            module_name="TinyKernel",
            theorem_name="two_add_two",
            expected_type=_TYPE,
            contract_id=contract.contract_id,
            obligation_id=obligation.obligation_id,
            exact_claim=_CLAIM,
            approved_axioms=tuple(sorted(APPROVED_FOUNDATIONAL_AXIOMS)) + ("Nat.add_comm",),
        )
        raise AssertionError("broad whitelist must fail")
    except KernelEvidenceError as exc:
        assert "broader" in str(exc)


def test_injected_sorry_and_user_axiom_cannot_pass():
    contract = _contract()
    obligation = _obligation(contract)
    try:
        _artifact(contract, obligation, axioms=("sorryAx",))
        raise AssertionError("sorryAx PASS must fail")
    except KernelEvidenceError:
        pass
    try:
        _artifact(contract, obligation, axioms=("userAx",))
        raise AssertionError("user axiom PASS must fail")
    except KernelEvidenceError:
        pass
    failed = _artifact(
        contract,
        obligation,
        status=KernelCheckStatus.FAIL,
        theorem_type="1 = 0",
        axioms=("sorryAx",),
        detail="forbidden axiom sorryAx",
    )
    assert failed.status is KernelCheckStatus.FAIL


def test_injected_pass_cannot_be_lifted_to_kernel_evidence():
    contract = _contract()
    obligation = _obligation(contract)
    kernel = _artifact(contract, obligation)
    with tempfile.TemporaryDirectory() as raw:
        store = ArtifactStore(Path(raw) / "cas")
        aligned = attest_statement_alignment(
            version=1,
            contract=contract,
            obligation=obligation,
            kernel_artifact=kernel,
            auditor_id="auditor",
            auditor_version="1",
            aligned=True,
            created_at=_TS,
            kernel_artifact_hash=sha256_hex(kernel.content_bytes()),
        )
        try:
            build_kernel_checked_evidence(
                kernel_artifact=kernel,
                alignment=aligned,
                contract=contract,
                obligation=obligation,
                artifacts=store,
                producer="tester",
                created_at=_TS,
            )
            raise AssertionError("untrusted injected PASS must not lift")
        except KernelEvidenceError as exc:
            assert "lake-env" in str(exc) or "provenance" in str(exc).lower()


def test_unaligned_attestation_cannot_lift():
    contract = _contract()
    obligation = _obligation(contract)
    kernel = _artifact(contract, obligation)
    with tempfile.TemporaryDirectory() as raw:
        store = ArtifactStore(Path(raw) / "cas")
        unaligned = attest_statement_alignment(
            version=1,
            contract=contract,
            obligation=obligation,
            kernel_artifact=kernel,
            auditor_id="auditor",
            auditor_version="1",
            aligned=False,
            created_at=_TS,
            kernel_artifact_hash=sha256_hex(kernel.content_bytes()),
        )
        try:
            build_kernel_checked_evidence(
                kernel_artifact=kernel,
                alignment=unaligned,
                contract=contract,
                obligation=obligation,
                artifacts=store,
                producer="tester",
                created_at=_TS,
            )
            raise AssertionError("unaligned attestation must not lift kernel evidence")
        except KernelEvidenceError as exc:
            assert "natural-language" in str(exc)


def test_injected_wrong_theorem_is_rejected():
    contract = _contract()
    obligation = _obligation(contract)
    artifact = _artifact(contract, obligation, theorem_name="two_add_two")
    transport = InjectedKernelTransport(artifact)
    req = _request(
        contract,
        obligation,
        Path("/tmp"),
        theorem_name="other_theorem",
        expected_type=_TYPE,
    )
    try:
        run_kernel_check(req, transport=transport)
        raise AssertionError("wrong theorem must not count")
    except KernelEvidenceError:
        pass


def test_fake_checker_binary_is_rejected():
    contract = _contract()
    obligation = _obligation(contract)
    with tempfile.TemporaryDirectory() as raw:
        fake = Path(raw) / "not-a-checker"
        fake.write_text("#!/bin/sh\necho PASS\n", encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        try:
            KernelCheckRequest(
                project_dir=str(Path(raw)),
                module_name="TinyKernel",
                theorem_name="two_add_two",
                expected_type=_TYPE,
                contract_id=contract.contract_id,
                obligation_id=obligation.obligation_id,
                exact_claim=_CLAIM,
                lake_bin=str(fake),
            )
            raise AssertionError("arbitrary checker binary must be rejected")
        except KernelEvidenceError as exc:
            assert "lake" in str(exc).lower()


def test_inconsistent_build_and_check_is_rejected():
    build = _Proc(KernelCheckStatus.FAIL, "", "compile error", "command exited 1", 1)
    check = _Proc(KernelCheckStatus.PASS, "two_add_two : 2 + 2 = 4\n", "", "ok", 0)
    status, detail = _combine_stage_outcomes(
        build, check, KernelCheckStatus.PASS, "target theorem kernel-checked"
    )
    assert status is KernelCheckStatus.FAIL
    assert "inconsistent" in detail


def test_symlink_escape_is_rejected():
    contract = _contract()
    obligation = _obligation(contract)
    with tempfile.TemporaryDirectory() as raw:
        root = _write_lean_project(
            Path(raw) / "proj", "theorem two_add_two : 2 + 2 = 4 := rfl\n"
        )
        outside = Path(raw) / "secret.lean"
        outside.write_text("axiom leak : False\n", encoding="utf-8")
        link = root / "escape.lean"
        link.symlink_to(outside)
        try:
            artifact = run_kernel_check(
                _request(contract, obligation, root),
                transport=LeanLakeTransport(),
            )
        except KernelEvidenceError as exc:
            assert "symlink" in str(exc).lower() or "escapes" in str(exc).lower()
        else:
            assert artifact.status is KernelCheckStatus.FAIL
            assert "symlink" in artifact.detail.lower() or "escapes" in artifact.detail.lower()


def test_real_lean_kernel_check_child_claim_and_negatives():
    contract = _contract()
    root_ob = _obligation(contract)
    child = _obligation(contract, statement=_CHILD_CLAIM)
    assert child.exact_statement != contract.exact_statement
    with tempfile.TemporaryDirectory() as raw:
        good = _write_lean_project(
            Path(raw) / "good",
            "theorem two_add_two : 2 + 2 = 4 := rfl\n",
        )
        store = ArtifactStore(Path(raw) / "cas")
        passed = run_kernel_check(
            _request(contract, child, good),
            transport=LeanLakeTransport(),
            artifacts=store,
        )
        assert passed.status is KernelCheckStatus.PASS
        assert passed.theorem_name == "two_add_two"
        assert passed.provenance_kind == TRUSTED_PROVENANCE
        assert passed.lean_githash
        assert Path(passed.lake_executable).name.startswith("lake")
        assert passed.build_status == "PASS"
        assert passed.check_status == "PASS"
        assert passed.exact_claim == _CHILD_CLAIM
        assert passed.contract_id == contract.contract_id
        aligned = attest_statement_alignment(
            version=1,
            contract=contract,
            obligation=child,
            kernel_artifact=passed,
            auditor_id="auditor",
            auditor_version="1",
            aligned=True,
            created_at=_TS,
            kernel_artifact_hash=sha256_hex(passed.content_bytes()),
        )
        evidence = build_kernel_checked_evidence(
            kernel_artifact=passed,
            alignment=aligned,
            contract=contract,
            obligation=child,
            artifacts=store,
            producer="tester",
            created_at=_TS,
        )
        assert evidence.evidence_kind is EvidenceKind.KERNEL_CHECKED_PROOF
        assert evidence.exact_claim == _CHILD_CLAIM
        persist_kernel_artifact(passed, store)

        stale = attest_statement_alignment(
            version=1,
            contract=contract,
            obligation=child,
            kernel_artifact=passed,
            auditor_id="auditor",
            auditor_version="1",
            aligned=True,
            created_at=_TS,
            kernel_artifact_hash=_hex("stale-kernel"),
        )
        try:
            build_kernel_checked_evidence(
                kernel_artifact=passed,
                alignment=stale,
                contract=contract,
                obligation=child,
                artifacts=store,
                producer="tester",
                created_at=_TS,
            )
            raise AssertionError("stale attestation hash must fail")
        except KernelEvidenceError:
            pass

        empty = _write_lean_project(
            Path(raw) / "empty",
            "def unused : Nat := 0\n",
        )
        missing = run_kernel_check(
            _request(contract, child, empty),
            transport=LeanLakeTransport(),
        )
        assert missing.status is KernelCheckStatus.FAIL
        assert "theorem" in missing.detail.lower()

        sorry_proj = _write_lean_project(
            Path(raw) / "sorry",
            "theorem two_add_two : 2 + 2 = 4 := sorry\n",
        )
        sorry = run_kernel_check(
            _request(contract, child, sorry_proj),
            transport=LeanLakeTransport(),
        )
        assert sorry.status is KernelCheckStatus.FAIL
        assert "sorry" in sorry.detail.lower() or "sorryAx" in "".join(sorry.axioms)

        user_proj = _write_lean_project(
            Path(raw) / "userax",
            "axiom userAx : 2 + 2 = 4\ntheorem two_add_two : 2 + 2 = 4 := userAx\n",
        )
        user = run_kernel_check(
            _request(contract, child, user_proj),
            transport=LeanLakeTransport(),
        )
        assert user.status is KernelCheckStatus.FAIL
        assert "userAx" in user.detail or "unapproved" in user.detail.lower()

        premise = _write_lean_project(
            Path(raw) / "premise",
            "theorem two_add_two (p : True) : 2 + 2 = 4 := rfl\n",
        )
        added = run_kernel_check(
            _request(contract, child, premise),
            transport=LeanLakeTransport(),
        )
        assert added.status is KernelCheckStatus.FAIL
        assert "type" in added.detail.lower()

        wrong_ty = run_kernel_check(
            _request(contract, child, good, expected_type="1 = 0"),
            transport=LeanLakeTransport(),
        )
        assert wrong_ty.status is KernelCheckStatus.FAIL
        assert "type" in wrong_ty.detail.lower()

        tampered = _write_lean_project(
            Path(raw) / "tamper",
            "theorem two_add_two : 2 + 2 = 4 := rfl\n",
        )
        class _TamperTransport(LeanLakeTransport):
            def check(self, request):
                artifact = super().check(request)
                (Path(request.project_dir) / "TinyKernel.lean").write_text(
                    "theorem two_add_two : 2 + 2 = 4 := sorry\n",
                    encoding="utf-8",
                )
                return artifact

        first = run_kernel_check(
            _request(contract, child, tampered),
            transport=_TamperTransport(),
        )
        assert first.status is KernelCheckStatus.PASS
        second = run_kernel_check(
            _request(contract, child, tampered),
            transport=LeanLakeTransport(),
        )
        assert second.status is KernelCheckStatus.FAIL
        assert first.source_manifest.manifest_hash != second.source_manifest.manifest_hash
