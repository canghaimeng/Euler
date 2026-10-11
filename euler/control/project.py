"""Managed Euler project persistence over a single EventStore.

Stdlib only. Owns contained control paths, filtered ResearchView replay, and
``euler.*`` candidate metadata. Does not launch providers, write TASK, or touch
the Fact Graph.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional

from euler.control.artifacts import ArtifactStore
from euler.control.budget import BudgetLedger
from euler.control.contracts import ProblemContract
from euler.control.event_store import EventStore, IdempotencyConflict
from euler.control.ids import payload_hash
from euler.control.live_records import (
    EVENT_CANDIDATE_PROPOSED,
    ControlManifest,
    ControlStatus,
    ProposedCandidate,
)
from euler.control.obligations import Obligation
from euler.control.policy import BudgetPolicy, ResourceLimitKind
from euler.control.runtime import RuntimeCoordinator, RuntimeSnapshot
from euler.control.scheduler import CandidateAction
from euler.control.schema import BudgetLimit, BudgetLimitKind, EventEnvelope
from euler.control.views import ResearchView, UnknownResearchEvent

_CONTROL_DIR = "control"
_DB_NAME = "control.sqlite3"
_ARTIFACTS_DIR = "artifacts"
_POLICY_FILE = "policy.json"
_MANIFEST_FILE = "manifest.json"
_DISPATCH_DIR = "dispatch"
_PROJECT_JSON = "project.json"
_CONTROL_PLANE = "euler"
_SCHEMA_VERSION = 1
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_RESEARCH_EVENT_TYPES = frozenset(
    {
        "problem.contract.created",
        "evidence.registered",
        "evidence.invalidated",
        "obligation.created",
        "obligation.status_changed",
        "obligation.reopened",
        "route.created",
        "route.status_changed",
        "relation.created",
        "failure.recorded",
    }
)
_SKIP_EVENT_PREFIXES = ("euler.", "runtime.", "budget.")

_INIT_CONTRACT_KEY = "init:contract"
_INIT_OBLIGATION_KEY = "init:obligation"

__all__ = [
    "EulerProjectError",
    "EulerProjectConflict",
    "EulerProjectIncomplete",
    "EulerProject",
    "initialize_project",
    "open_project",
    "is_euler_project",
]


class EulerProjectError(ValueError):
    """Malformed, missing, or unreadable managed-project state."""


class EulerProjectConflict(EulerProjectError):
    """Existing managed state disagrees with the requested identity."""


class EulerProjectIncomplete(EulerProjectError):
    """Control directory or project.json is a partial/conflicting remnant."""


class EulerProject:
    """Opened managed project. Close or use as a context manager."""

    def __init__(self, project_dir: str | Path, *, _stores: dict[str, Any] | None = None) -> None:
        root = _require_existing_project_dir(project_dir)
        self._root = root
        self._closed = False
        if _stores is None:
            _assert_complete(root)
            stores = _open_stores(root)
        else:
            stores = _stores
        self._store: EventStore = stores["store"]
        self._budget: BudgetLedger = stores["budget"]
        self._artifacts: ArtifactStore = stores["artifacts"]
        self._policy: BudgetPolicy = stores["policy"]
        self._manifest: ControlManifest = stores["manifest"]
        self._runtime = RuntimeCoordinator(
            self._policy, self._store, self._budget, self._artifacts
        )

    @property
    def project_dir(self) -> Path:
        return self._root

    @property
    def project_id(self) -> str:
        return self._manifest.project_id

    @property
    def control_dir(self) -> Path:
        return _control_dir(self._root)

    @property
    def event_store(self) -> EventStore:
        self._ensure_open()
        return self._store

    @property
    def budget(self) -> BudgetLedger:
        self._ensure_open()
        return self._budget

    @property
    def artifacts(self) -> ArtifactStore:
        self._ensure_open()
        return self._artifacts

    @property
    def policy(self) -> BudgetPolicy:
        return self._policy

    @property
    def manifest(self) -> ControlManifest:
        return self._manifest

    @property
    def runtime(self) -> RuntimeCoordinator:
        self._ensure_open()
        return self._runtime

    def __enter__(self) -> "EulerProject":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._store.close()
        self._closed = True

    def research_view(self) -> ResearchView:
        self._ensure_open()
        view = ResearchView()
        for seq, envelope in self._store.iter_project(self.project_id):
            if envelope.event_type in _RESEARCH_EVENT_TYPES:
                view.apply(seq, envelope)
                continue
            if envelope.event_type.startswith(_SKIP_EVENT_PREFIXES):
                continue
            raise UnknownResearchEvent(
                f"unsupported research event type {envelope.event_type!r}"
            )
        return view

    def runtime_snapshot(self) -> RuntimeSnapshot:
        self._ensure_open()
        return self._runtime.replay(self.project_id)

    def append_proposed_candidate(
        self,
        candidate: CandidateAction,
        worker: str,
        *,
        actor: str,
        timestamp_utc: str,
    ) -> ProposedCandidate:
        self._ensure_open()
        if not isinstance(candidate, CandidateAction):
            raise EulerProjectError("candidate must be a CandidateAction")
        worker = _require_safe("worker", worker)
        _require_known_worker(self._root, worker)
        record = ProposedCandidate.build(
            candidate=candidate, worker=worker, created_at=timestamp_utc
        )
        envelope = EventEnvelope.build(
            project_id=self.project_id,
            actor=actor,
            event_type=EVENT_CANDIDATE_PROPOSED,
            idempotency_key=record.proposal_id,
            payload=record.to_dict(),
            timestamp_utc=timestamp_utc,
            correlation_id=self.project_id,
        )
        try:
            stored = self._store.append(envelope).event
        except IdempotencyConflict as exc:
            raise EulerProjectConflict(str(exc)) from exc
        return ProposedCandidate.from_dict(dict(stored.payload))

    def load_proposed_candidates(self) -> tuple[ProposedCandidate, ...]:
        self._ensure_open()
        out: list[ProposedCandidate] = []
        for _seq, envelope in self._store.iter_project(self.project_id):
            if envelope.event_type != EVENT_CANDIDATE_PROPOSED:
                continue
            out.append(ProposedCandidate.from_dict(dict(envelope.payload)))
        return tuple(out)

    def status(self) -> ControlStatus:
        self._ensure_open()
        proposed = self.load_proposed_candidates()
        max_seq = 0
        for seq, _envelope in self._store.iter_project(self.project_id):
            max_seq = seq
        account = self._budget.ensure_account(
            self.project_id, _budget_limit(self._policy)
        )
        return ControlStatus.build(
            project_id=self.project_id,
            contract_id=self._manifest.contract_id,
            target_obligation_id=self._manifest.target_obligation_id,
            policy_hash=self._manifest.policy_hash,
            manifest_hash=self._manifest.manifest_hash,
            created_at=self._manifest.created_at,
            actor=self._manifest.actor,
            max_seq=max_seq,
            proposed_ids=tuple(item.proposal_id for item in proposed),
            budget_kind=account.limit.kind.value,
            budget_value=account.limit.value,
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise EulerProjectError("EulerProject is closed")


def initialize_project(
    project_dir: str | Path,
    contract: ProblemContract,
    target_obligation: Obligation,
    policy: BudgetPolicy,
    actor: str,
    timestamp_utc: str,
) -> EulerProject:
    """Create or assert-same a managed Euler control plane on an existing project."""
    if not isinstance(contract, ProblemContract):
        raise EulerProjectError("contract must be a ProblemContract")
    if not isinstance(target_obligation, Obligation):
        raise EulerProjectError("target_obligation must be an Obligation")
    if not isinstance(policy, BudgetPolicy):
        raise EulerProjectError("policy must be a BudgetPolicy")
    actor = _require_nonempty("actor", actor)
    if target_obligation.contract_id != contract.contract_id:
        raise EulerProjectError("target obligation contract_id does not match contract")
    root = _require_existing_project_dir(project_dir)
    meta = _read_project_json(root)
    project_id = _require_safe("project_id", meta.get("name"))
    policy_digest = payload_hash(policy.to_dict())
    wanted = ControlManifest.build(
        project_id=project_id,
        contract_id=contract.contract_id,
        target_obligation_id=target_obligation.obligation_id,
        policy_hash=policy_digest,
        created_at=timestamp_utc,
        actor=actor,
    )
    if _control_present(root) or _manifest_ref(meta) is not None:
        if not _is_complete(root, meta):
            raise EulerProjectIncomplete(
                f"partial or conflicting control state under {root}"
            )
        opened = EulerProject(root)
        try:
            _assert_same(opened, wanted, policy)
        except Exception:
            opened.close()
            raise
        return opened

    control = _control_dir(root)
    try:
        control.mkdir(parents=True, exist_ok=False)
        (control / _ARTIFACTS_DIR).mkdir()
        (control / _DISPATCH_DIR).mkdir()
        _write_json(control / _POLICY_FILE, policy.to_dict())
        _write_json(control / _MANIFEST_FILE, wanted.to_dict())
        store = EventStore(control / _DB_NAME)
        budget = BudgetLedger(store)
        artifacts = ArtifactStore(control / _ARTIFACTS_DIR)
        _append_init_events(
            store,
            project_id=project_id,
            contract=contract,
            obligation=target_obligation,
            actor=actor,
            timestamp_utc=timestamp_utc,
        )
        budget.ensure_account(
            project_id, _budget_limit(policy), actor=actor, timestamp_utc=timestamp_utc
        )
        _install_project_json_ref(root, meta, wanted)
        return EulerProject(
            root,
            _stores={
                "store": store,
                "budget": budget,
                "artifacts": artifacts,
                "policy": policy,
                "manifest": wanted,
            },
        )
    except Exception:
        raise


def open_project(project_dir: str | Path) -> EulerProject:
    return EulerProject(project_dir)


def is_euler_project(project_dir: str | Path) -> bool:
    path = Path(project_dir)
    if not path.is_dir():
        return False
    try:
        meta = _read_project_json(path.resolve())
    except EulerProjectError:
        return False
    return _is_complete(path.resolve(), meta)


def _require_existing_project_dir(project_dir: str | Path) -> Path:
    path = Path(project_dir)
    if not path.exists() or not path.is_dir():
        raise EulerProjectError(f"project directory does not exist: {path}")
    root = path.resolve()
    if not (root / _PROJECT_JSON).is_file():
        raise EulerProjectError(f"missing {_PROJECT_JSON} in {root}")
    return root


def _control_dir(root: Path) -> Path:
    path = (root / _CONTROL_DIR).resolve()
    if root not in path.parents and path != root / _CONTROL_DIR:
        # contained: resolved control dir must stay under root
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise EulerProjectError("control path escapes project directory") from exc
    return root / _CONTROL_DIR


def _contained(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise EulerProjectError(f"path escapes project directory: {path}") from exc
    return resolved


def _control_present(root: Path) -> bool:
    control = root / _CONTROL_DIR
    return control.exists()


def _manifest_ref(meta: Mapping[str, Any]) -> Mapping[str, Any] | None:
    raw = meta.get("euler_control")
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or isinstance(raw, (str, bytes)):
        raise EulerProjectError("project.json euler_control must be an object")
    return raw


def _is_complete(root: Path, meta: Mapping[str, Any]) -> bool:
    if meta.get("control_plane") != _CONTROL_PLANE:
        return False
    ref = meta.get("euler_control")
    if not isinstance(ref, Mapping):
        return False
    control = root / _CONTROL_DIR
    required = (
        control / _DB_NAME,
        control / _POLICY_FILE,
        control / _MANIFEST_FILE,
        control / _ARTIFACTS_DIR,
        control / _DISPATCH_DIR,
    )
    if not all(path.exists() for path in required):
        return False
    if not (control / _ARTIFACTS_DIR).is_dir() or not (control / _DISPATCH_DIR).is_dir():
        return False
    return True


def _assert_complete(root: Path) -> None:
    meta = _read_project_json(root)
    if not _is_complete(root, meta):
        raise EulerProjectIncomplete(f"not a complete Euler-managed project: {root}")
    _contained(root, root / _CONTROL_DIR)
    _contained(root, root / _CONTROL_DIR / _DB_NAME)
    _contained(root, root / _CONTROL_DIR / _ARTIFACTS_DIR)


def _open_stores(root: Path) -> dict[str, Any]:
    control = _control_dir(root)
    policy = BudgetPolicy.from_dict(_read_json(control / _POLICY_FILE))
    manifest = ControlManifest.from_dict(_read_json(control / _MANIFEST_FILE))
    expected_policy = payload_hash(policy.to_dict())
    if manifest.policy_hash != expected_policy:
        raise EulerProjectConflict("policy.json does not match manifest policy_hash")
    meta = _read_project_json(root)
    ref = _manifest_ref(meta)
    if ref is None or ref.get("manifest_hash") != manifest.manifest_hash:
        raise EulerProjectConflict("project.json euler_control does not match manifest")
    if meta.get("name") != manifest.project_id:
        raise EulerProjectConflict("project.json name does not match manifest project_id")
    store = EventStore(control / _DB_NAME)
    budget = BudgetLedger(store)
    artifacts = ArtifactStore(control / _ARTIFACTS_DIR)
    return {
        "store": store,
        "budget": budget,
        "artifacts": artifacts,
        "policy": policy,
        "manifest": manifest,
    }


def _assert_same(
    opened: EulerProject, wanted: ControlManifest, policy: BudgetPolicy
) -> None:
    have = opened.manifest
    if have.project_id != wanted.project_id:
        raise EulerProjectConflict("project_id mismatch")
    if have.contract_id != wanted.contract_id:
        raise EulerProjectConflict("contract_id mismatch")
    if have.target_obligation_id != wanted.target_obligation_id:
        raise EulerProjectConflict("target_obligation_id mismatch")
    if have.policy_hash != wanted.policy_hash:
        raise EulerProjectConflict("policy_hash mismatch")
    if payload_hash(opened.policy.to_dict()) != payload_hash(policy.to_dict()):
        raise EulerProjectConflict("BudgetPolicy mismatch")
    found_contract = False
    found_obligation = False
    for _seq, envelope in opened.event_store.iter_project(opened.project_id):
        if envelope.event_type == "problem.contract.created" and envelope.idempotency_key.startswith(_INIT_CONTRACT_KEY + ":"):
            loaded = ProblemContract.from_dict(dict(envelope.payload)["contract"])
            if loaded.contract_id != wanted.contract_id:
                raise EulerProjectConflict("stored contract_id mismatch")
            found_contract = True
        if envelope.event_type == "obligation.created" and envelope.idempotency_key.startswith(_INIT_OBLIGATION_KEY + ":"):
            loaded_ob = Obligation.from_dict(dict(envelope.payload)["obligation"])
            if loaded_ob.obligation_id != wanted.target_obligation_id:
                raise EulerProjectConflict("stored target obligation mismatch")
            found_obligation = True
    if not found_contract or not found_obligation:
        raise EulerProjectIncomplete("init events missing from EventStore")
    account = opened.budget.ensure_account(opened.project_id, _budget_limit(policy))
    expected_limit = _budget_limit(policy)
    if account.limit.kind is not expected_limit.kind or account.limit.value != expected_limit.value:
        raise EulerProjectConflict("budget account does not match policy cost dimension")


def _append_init_events(
    store: EventStore,
    *,
    project_id: str,
    contract: ProblemContract,
    obligation: Obligation,
    actor: str,
    timestamp_utc: str,
) -> None:
    store.append(
        EventEnvelope.build(
            project_id=project_id,
            actor=actor,
            event_type="problem.contract.created",
            idempotency_key=f"{_INIT_CONTRACT_KEY}:{contract.contract_id}",
            payload={"contract": contract.to_dict()},
            timestamp_utc=timestamp_utc,
            correlation_id=project_id,
        )
    )
    store.append(
        EventEnvelope.build(
            project_id=project_id,
            actor=actor,
            event_type="obligation.created",
            idempotency_key=f"{_INIT_OBLIGATION_KEY}:{obligation.obligation_id}",
            payload={"obligation": obligation.to_dict()},
            timestamp_utc=timestamp_utc,
            correlation_id=project_id,
        )
    )


def _install_project_json_ref(
    root: Path, meta: dict[str, Any], manifest: ControlManifest
) -> None:
    updated = dict(meta)
    if updated.get("control_plane") not in (None, _CONTROL_PLANE):
        raise EulerProjectConflict("project.json control_plane is not euler")
    existing = updated.get("euler_control")
    ref = {
        "schema_version": _SCHEMA_VERSION,
        "manifest_hash": manifest.manifest_hash,
        "control_dir": _CONTROL_DIR,
    }
    if existing is not None and existing != ref:
        raise EulerProjectConflict("project.json already has a different euler_control")
    updated["control_plane"] = _CONTROL_PLANE
    updated["euler_control"] = ref
    _atomic_write_text(
        root / _PROJECT_JSON,
        json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
    )


def _read_project_json(root: Path) -> dict[str, Any]:
    path = root / _PROJECT_JSON
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EulerProjectError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EulerProjectError("project.json must be a JSON object")
    return payload


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EulerProjectError(f"cannot read {path}: {exc}") from exc


def _write_json(path: Path, payload: object) -> None:
    _atomic_write_text(
        path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _budget_limit(policy: BudgetPolicy) -> BudgetLimit:
    dim = policy.cost_microusd
    if dim.kind is ResourceLimitKind.UNLIMITED:
        return BudgetLimit(kind=BudgetLimitKind.UNLIMITED, value=None)
    return BudgetLimit(kind=BudgetLimitKind.LIMITED, value=dim.value)


def _require_nonempty(name: str, value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise EulerProjectError(f"{name} must be a nonempty str")
    return value


def _require_safe(name: str, value: object) -> str:
    text = _require_nonempty(name, value)
    if _SAFE_IDENTIFIER_RE.fullmatch(text) is None:
        raise EulerProjectError(f"{name} is not a safe identifier")
    return text


def _require_known_worker(root: Path, worker: str) -> None:
    meta = _read_project_json(root)
    workers = meta.get("workers")
    if workers is None:
        return
    if not isinstance(workers, list) or worker not in workers:
        raise EulerProjectError(f"unknown worker {worker!r}")
