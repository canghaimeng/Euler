# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Durable human run controls; dispatch and launch share an admission boundary.

A cancellation request is not a terminal lease. Updated native worker loops
terminate their local subprocess group and settle through normal receipts;
remote provider cancellation/billing cannot be guaranteed by that observation.
"""
from __future__ import annotations

import fcntl
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ids import canonical_json, sha256_hex
from .runtime import LeaseStatus
from .schema import EventEnvelope

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_LOCAL = threading.local()
ACTIVE = {LeaseStatus.ISSUED, LeaseStatus.RUNNING}


class CampaignError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


@contextmanager
def admission_lock(project_dir: Path):
    """Cross-process lock plus a reentrant in-process lock (never private state)."""
    key = str(Path(project_dir).resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    with lock:
        held = getattr(_LOCAL, 'held', {})
        if key in held:
            yield
            return
        path = Path(key) / 'control' / 'human-admission.lock'
        with path.open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            _LOCAL.held = {**held, key: True}
            try:
                yield
            finally:
                _LOCAL.held = held
                fcntl.flock(handle, fcntl.LOCK_UN)


def append(project, actor: str, kind: str, key: str, payload: dict,
           *, cause: str | None = None):
    return project.event_store.append(EventEnvelope.build(
        project_id=project.project_id, actor=actor, event_type=kind,
        idempotency_key=key, payload=payload, timestamp_utc=now(),
        correlation_id=payload.get('intervention_id', project.project_id),
        causation_id=cause,
    ))


def campaign_snapshot(project) -> dict[str, Any]:
    events = list(project.event_store.iter_project(project.project_id))
    last = next((e for _, e in reversed(events) if e.event_type == 'euler.campaign_command'), None)
    command = dict(last.payload) if last else {'command': 'resume', 'mode': 'drain'}
    active = [l for l in project.runtime_snapshot().leases if l.status in ACTIVE]
    focused = {e.payload['candidate_id'] for _, e in events if e.event_type == 'euler.focused_created'}
    relevant = active if command['command'] == 'stop' else [l for l in active if l.candidate_id not in focused]
    state = {'resume': 'RUNNING', 'pause': 'PAUSING' if relevant else 'PAUSED',
             'stop': 'STOP_REQUESTED' if relevant else 'STOPPED'}[command['command']]
    return {'state': state, 'command_event_id': last.event_id if last else None,
            'revision': last.event_id if last else 'initial',
            'contract_id': project.manifest.contract_id, 'mode': command['mode'],
            'active_leases': [{'lease_id': l.lease_id, 'candidate_id': l.candidate_id,
                               'status': l.status.value, 'focused': l.candidate_id in focused} for l in active],
            'cancellation_capability': 'cooperative local process-group termination; remote execution unknown',
            'remote_stop_confirmed': False}


def focused_record(project, candidate_id: str) -> dict | None:
    return next((dict(e.payload) for _, e in project.event_store.iter_project(project.project_id)
                 if e.event_type == 'euler.focused_created' and e.payload['candidate_id'] == candidate_id), None)


def assert_dispatch_allowed(project, candidate_id: str):
    state = campaign_snapshot(project)['state']
    if state in {'STOP_REQUESTED', 'STOPPED'}:
        raise CampaignError('campaign stopped; explicit resume required')
    focused = focused_record(project, candidate_id)
    if not focused:
        proposal = next((p for p in project.load_proposed_candidates()
                         if p.candidate.candidate_id == candidate_id), None)
        if proposal:
            owned_contracts = {e.payload['child_contract_id']
                               for _, e in project.event_store.iter_project(project.project_id)
                               if e.event_type == 'euler.focused_created'}
            if proposal.candidate.context_request.contract_id in owned_contracts:
                raise CampaignError('focused contract requires its explicitly authorized candidate')
    if focused:
        started = any(e.event_type == 'euler.focused_started' and
                      e.payload['intervention_id'] == focused['intervention_id']
                      for _, e in project.event_store.iter_project(project.project_id))
        if not started:
            raise CampaignError('focused investigation requires explicit start')
        # Recheck exact source identity at admission, including revoked dependencies.
        from .human_objects import resolve_object, assert_same_target
        current = resolve_object(project, focused['target']['object_type'], focused['target']['object_id'])
        assert_same_target(current, focused['target'])
        prior = [l for l in project.runtime_snapshot().leases if l.candidate_id == candidate_id]
        if any(l.status not in ACTIVE for l in prior):
            raise CampaignError('focused allowance already consumed; create a new authorized investigation')
    elif state != 'RUNNING':
        raise CampaignError('campaign paused; ordinary dispatch is disabled')


def cancellation_requested(project, lease_id: str) -> bool:
    return any(e.event_type == 'euler.cancel_requested' and e.payload['lease_id'] == lease_id
               for _, e in project.event_store.iter_project(project.project_id))


class CampaignControl:
    def __init__(self, project, *, actor: str):
        self.project, self.actor = project, actor

    def command(self, command: str, *, mode: str, key: str, expected_revision: str,
                contract_id: str) -> dict:
        if command not in {'pause', 'resume', 'stop'} or mode not in {'drain', 'cancel'}:
            raise CampaignError('invalid run command or pause mode')
        if not key or not self.actor:
            raise CampaignError('actor and idempotency key required')
        payload = {'command': command, 'mode': mode, 'expected_revision': expected_revision,
                   'contract_id': contract_id, 'actor': self.actor}
        p = self.project
        with admission_lock(p.project_dir), p.event_store.transaction():
            previous = next((e for _, e in p.event_store.iter_project(p.project_id)
                             if e.event_type == 'euler.campaign_command' and e.idempotency_key == key), None)
            if previous:
                if dict(previous.payload) != payload:
                    raise CampaignError('idempotency key has different command content')
                return campaign_snapshot(p)
            current = campaign_snapshot(p)
            if current['revision'] != expected_revision or contract_id != p.manifest.contract_id:
                raise CampaignError('stale campaign/contract version; review the current state')
            if command == 'resume':
                if current['state'] in {'PAUSING', 'STOP_REQUESTED'}:
                    raise CampaignError('running leases must settle before resume')
                from .research_service import ResearchService
                ResearchService(p, actor=self.actor).sync_fact_dependencies()
                resources = p.runtime_snapshot().resources
                for name, used in resources.consumed.to_dict().items():
                    limit = getattr(p.policy, name)
                    if limit.value is not None and limit.value > 0 and used >= limit.value:
                        raise CampaignError('resource allowance exhausted: ' + name)
                # Dispatch rechecks reservations, slots, dependency validity and readiness.
            result = append(p, self.actor, 'euler.campaign_command', key, payload)
            focused = {e.payload['candidate_id'] for _, e in p.event_store.iter_project(p.project_id)
                       if e.event_type == 'euler.focused_created'}
            if command != 'resume':
                for lease in p.runtime_snapshot().leases:
                    if lease.status not in ACTIVE or (command == 'pause' and lease.candidate_id in focused):
                        continue
                    if lease.status is LeaseStatus.ISSUED:
                        p.runtime.cancel(p.project_id, lease.lease_id, actor=self.actor)
                    elif mode == 'cancel' or command == 'stop':
                        append(p, self.actor, 'euler.cancel_requested', 'cancel:' + lease.lease_id,
                               {'lease_id': lease.lease_id, 'candidate_id': lease.candidate_id,
                                'remote_stop_confirmed': False}, cause=result.event.event_id)
            checkpoint = {'campaign': campaign_snapshot(p),
                          'research_revision': max((seq for seq, _ in p.event_store.iter_project(p.project_id)), default=0),
                          'contract_id': p.manifest.contract_id,
                          'leases': [l.to_dict() for l in p.runtime_snapshot().leases]}
            artifact = p.artifacts.put_bytes(canonical_json(checkpoint).encode(), media_type='application/json')
            append(p, self.actor, 'euler.human_checkpoint', key,
                   {'command_event_id': result.event.event_id, 'artifact_hash': artifact.content_hash},
                   cause=result.event.event_id)
            return campaign_snapshot(p)


def settle_human_checkpoint(project):
    snapshot = campaign_snapshot(project)
    if snapshot['state'] not in {'PAUSED', 'STOPPED'} or not snapshot['command_event_id']:
        return
    # One final checkpoint for each command, after actual runtime settlement.
    key = snapshot['command_event_id']
    if any(e.event_type == 'euler.human_checkpoint_settled' and e.idempotency_key == key
           for _, e in project.event_store.iter_project(project.project_id)):
        return
    artifact = project.artifacts.put_bytes(canonical_json(snapshot).encode(), media_type='application/json')
    append(project, 'reconciler', 'euler.human_checkpoint_settled', key,
           {'command_event_id': key, 'state': snapshot['state'], 'artifact_hash': artifact.content_hash}, cause=key)
