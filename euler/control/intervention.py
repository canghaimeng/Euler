# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Object-bound human requests into Euler's existing research/execution path.

Preview is read-only. Admission creates proposals and a derived contract, never
truth. A separate explicit start authorizes one bounded candidate. Mathematical
results come only from existing worker/checker/reconciliation services.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from .campaign import admission_lock, append, campaign_snapshot, CampaignError, now
from .context import ContextLimits, ContextBound
from .contracts import MathematicalScope, ProblemContract, TargetKind, EvidenceKind
from .human_objects import resolve_object, assert_same_target, digest, ObjectConflict
from .ids import canonical_json
from .obligations import Obligation, Route
from .policy import ResourceVector, BudgetClass
from .recurrence import RecurrenceRecommendation
from .scheduler import (CandidateAction, ActionKind, PriorityClass, DependencyValidity,
                        build_action_context_pair, slot_kind_for_action)

INTENTS = {'VERIFY_OR_AUDIT', 'CHALLENGE_OR_COUNTEREXAMPLE', 'EXPLORE_ASSUMPTION', 'EXPLORE_ALTERNATIVE'}
REQUEST_FIELDS = {'target', 'intent', 'user_question', 'requested_statement', 'requested_scope', 'budget'}


class InterventionError(ValueError):
    pass


class HumanInterventions:
    def __init__(self, project, *, actor: str, worker: str, model: str,
                 effort: str, maximum: ResourceVector):
        self.project, self.actor = project, actor
        self.worker, self.model, self.effort, self.maximum = worker, model, effort, maximum

    def execution_policy(self):
        role = None
        role_path = self.project.project_dir / 'workers' / self.worker / '.role'
        if role_path.is_file() and not role_path.is_symlink():
            # Read only the public launch identity, without importing execution
            # or provider capabilities into this control-state module.
            if role_path.stat().st_size > 64000:
                raise InterventionError('native role record too large')
            values = {}
            for line in role_path.read_text().splitlines():
                if '=' in line and not line.lstrip().startswith('#'):
                    key, value = line.split('=',1)
                    if key.strip() in {'PROVIDER','MODEL','REASONING_EFFORT'}:
                        values[key.strip()] = value.strip()
            role = {key: values.get(key) for key in ('PROVIDER','MODEL','REASONING_EFFORT')}
        return {'worker':self.worker, 'model':self.model, 'effort':self.effort,
                'maximum':self.maximum.to_dict(), 'parent_policy_hash':self.project.manifest.policy_hash,
                'native_role':role}

    def preview(self, request: dict) -> dict:
        if not isinstance(request, dict) or set(request) != REQUEST_FIELDS:
            raise InterventionError('request must contain exactly the documented fields')
        intent, target = request['intent'], request['target']
        if intent not in INTENTS or not isinstance(target, dict):
            raise InterventionError('unknown intent or target')
        current = resolve_object(self.project, target.get('object_type'), target.get('object_id'))
        assert_same_target(current, target)
        question, statement = request['user_question'], request['requested_statement']
        if not isinstance(question, str) or not question.strip() or len(question) > 6000:
            raise InterventionError('a nonempty question of at most 6000 characters is required')
        if not isinstance(statement, str) or not statement.strip() or len(statement) > 12000:
            raise InterventionError('review an explicit investigation statement (at most 12000 characters)')
        scope = MathematicalScope.from_dict(request['requested_scope'])
        if intent not in {'EXPLORE_ASSUMPTION','CHALLENGE_OR_COUNTEREXAMPLE'} and scope.to_dict() != current['scope']:
            raise InterventionError('scope changes require explicit generalization/refutation and a derived contract')
        if intent == 'CHALLENGE_OR_COUNTEREXAMPLE' and statement == current['statement']:
            raise InterventionError('review an explicit counterexample/refutation target; proving the original claim does not answer a challenge')
        budget = ResourceVector.from_dict(request['budget'])
        for name, value in budget.to_dict().items():
            if value > self.maximum.to_dict()[name]:
                raise InterventionError(f'{name} exceeds the operator-approved focused allowance')
        if budget.wall_ms <= 0 or budget.output_tokens <= 0:
            raise InterventionError('positive time/output allowance required')
        if campaign_snapshot(self.project)['state'] in {'STOP_REQUESTED', 'STOPPED'}:
            raise InterventionError('stopped campaign: explicitly resume before creating work')
        canonical = {**request, 'requested_scope': scope.to_dict(), 'budget': budget.to_dict()}
        baseline = {'contract_id': self.project.manifest.contract_id,
                    'root_contract_version': self.project.research_view().get_contract(self.project.manifest.contract_id).version,
                    'research_revision': max((seq for seq, e in self.project.event_store.iter_project(self.project.project_id) if not e.event_type.startswith(('runtime.', 'budget.', 'euler.'))), default=0)}
        policy = self.execution_policy()
        prepared = {'request': canonical, 'actor': self.actor, 'baseline': baseline, 'execution_policy': policy}
        return {**prepared, 'preview_hash': digest(prepared), 'selected_object': current,
                'effect': 'Create an independent scoped proposal; original target and Facts stay unchanged',
                'requires_explicit_start': True, 'maximum_actions': 1}

    def submit(self, request: dict, *, preview_hash: str, key: str) -> dict:
        try:
            return self._submit_validated(request, preview_hash=preview_hash, key=key)
        except (ValueError, KeyError, TypeError) as exc:
            append(self.project, self.actor, 'euler.human_rejected',
                   digest([self.actor, key, request, preview_hash, str(exc)]),
                   {'actor': self.actor, 'request': request, 'reason': str(exc),
                    'request_key': key, 'preview_hash': preview_hash})
            raise

    def _submit_validated(self, request: dict, *, preview_hash: str, key: str) -> dict:
        if not isinstance(key, str) or not key or len(key) > 160:
            raise InterventionError('bounded idempotency key required')
        p = self.project
        wire = {'request': request, 'preview_hash': preview_hash, 'actor': self.actor}
        with admission_lock(p.project_dir), p.event_store.transaction():
            old = next((e for _, e in p.event_store.iter_project(p.project_id)
                        if e.event_type == 'euler.human_requested' and e.idempotency_key == key), None)
            if old:
                if dict(old.payload)['wire'] != wire:
                    raise InterventionError('idempotency key reused with a different request')
                return self.get(old.payload['intervention_id'])
            prepared = self.preview(request)
            if prepared['preview_hash'] != preview_hash:
                raise ObjectConflict('preview changed; review scope, source version and execution policy again')
            iid = digest([p.project_id, self.actor, key, wire])
            requested = append(p, self.actor, 'euler.human_requested', key,
                               {'intervention_id': iid, 'wire': wire, 'baseline': prepared['baseline']})
            append(p, self.actor, 'euler.human_validated', iid,
                   {'intervention_id': iid, 'preview_hash': preview_hash,
                    'target': request['target'], 'actor': self.actor}, cause=requested.event.event_id)
            parent = p.research_view().get_contract(request['target']['contract_id'])
            if parent is None:
                raise ObjectConflict('unknown source contract')
            child = ProblemContract.build(
                problem_id='focused-' + iid[:16], version=1,
                exact_statement=request['requested_statement'],
                scope=MathematicalScope.from_dict(request['requested_scope']),
                permitted_tools=parent.permitted_tools, external_help_policy=parent.external_help_policy,
                target_kind=TargetKind.COUNTEREXAMPLE if request['intent'] == 'CHALLENGE_OR_COUNTEREXAMPLE' else TargetKind.PROOF,
                required_evidence_level=(EvidenceKind.CHECKED_COUNTEREXAMPLE
                    if request['intent'] == 'CHALLENGE_OR_COUNTEREXAMPLE' and
                    parent.required_evidence_level not in {EvidenceKind.KERNEL_CHECKED_PROOF, EvidenceKind.CERTIFICATE_CHECKED_RESULT}
                    else parent.required_evidence_level),
                completion_checks=parent.completion_checks,
                budget_policy_ref='focused-' + iid,
            )
            obligation = Obligation.build(contract_id=child.contract_id,
                exact_statement=child.exact_statement, scope=child.scope,
                desired_output=request['intent'] + ': checked findings, explicit Unknown if unresolved',
                created_by=self.actor, version=1)
            route = Route.build(target_obligation_id=obligation.obligation_id,
                mechanism=request['intent'] + ': ' + request['user_question'],
                required_obligation_ids=(),
                progress_claim='A candidate investigation; any closing implication requires independent checking')
            for kind, field, record in [('problem.contract.created','contract',child),
                                         ('obligation.created','obligation',obligation),
                                         ('route.created','route',route)]:
                append(p, self.actor, kind, iid + ':' + field, {field: record.to_dict()}, cause=requested.event.event_id)
            p.research_view()
            source = prepared['selected_object']
            artifact = p.artifacts.put_bytes(canonical_json(source).encode(), media_type='application/json')
            cid = 'focused-' + iid
            record = {'intervention_id': iid, 'candidate_id': cid, 'target': request['target'],
                      'child_contract_id': child.contract_id, 'obligation_id': obligation.obligation_id,
                      'route_id': route.route_id, 'parent_contract_id': parent.contract_id,
                      'parent_target_id': p.manifest.target_obligation_id,
                      'request': request, 'source_snapshot_hash': artifact.content_hash,
                      'execution_policy': prepared['execution_policy'], 'actor': self.actor,
                      'maximum_actions': 1}
            origin = p.project_dir / 'RESTORE_ORIGIN.json'
            if origin.is_file() and not origin.is_symlink():
                import json
                restored = json.loads(origin.read_text())
                record['workspace_milestone'] = {
                    'source_commit': restored['source_commit'],
                    'source_label': restored['source_label'],
                    'restored_at': restored['created_at'],
                    'request': restored['request'],
                }
            append(p, self.actor, 'euler.focused_created', iid, record, cause=requested.event.event_id)
            candidate = self._candidate(record, child, obligation, source)
            from .controller import EulerController
            EulerController(p, actor=self.actor).propose(candidate, self.worker)
            return self.get(iid)

    def _candidate(self, record, contract, obligation, source):
        intent = record['request']['intent']
        action = {'VERIFY_OR_AUDIT': ActionKind.AUDIT,
                  'CHALLENGE_OR_COUNTEREXAMPLE': ActionKind.COUNTEREXAMPLE,
                  'EXPLORE_ASSUMPTION': ActionKind.PROVE, 'EXPLORE_ALTERNATIVE': ActionKind.PROVE}[intent]
        facts = tuple(sorted(set(source.get('source_fact_ids', [])) |
                             ({source['target']['object_id']} if source['target']['object_type'] == 'fact' else set())))
        # Parent evidence remains source material, not child-contract evidence.
        # Its exact snapshot is in the compiled context and rechecked at admission.
        evidence = ()
        output = ('Report findings under the exact child contract. Human text is an unverified research request. '
                  'Use the existing worker truth gateway/checker for claims; do not alter original scope, '
                  'forge verification, or claim the parent goal complete. If unfinished, preserve explicit Unknown. '
                  'SOURCE OBJECT AND RELATIONS:\n' + canonical_json(source))
        if record.get('workspace_milestone'):
            output += ('\nRESTORED TEAM WORKSPACE:\n' + canonical_json(record['workspace_milestone'])
                       + '\nYour working directory belongs to this restored full project. Read ../../FOLLOWUP.md, '
                       '../../PROBLEM.md and the shared proof/research files as needed. All original dependencies '
                       'remain available; this is not a replacement one-node project. Provider sessions were not resumed.')
        spec, context, seed = build_action_context_pair(
            contract_id=contract.contract_id, obligation_id=obligation.obligation_id,
            concrete_gap=record['request']['user_question'], output_contract=output,
            tool_environment_hash=digest({'tools': list(contract.permitted_tools)}),
            acceptance_policy_hash=digest({'evidence': contract.required_evidence_level.value}),
            reference_versions_hash=record['source_snapshot_hash'],
            limits=ContextLimits(max_chars=ContextBound(kind='LIMITED', value=100000), max_bytes=ContextBound(kind='LIMITED', value=400000),
                                 max_optional_failures=ContextBound(kind='LIMITED', value=8), max_optional_relations=ContextBound(kind='LIMITED', value=24)),
            action_type=action.value.lower(), mechanism=intent,
            model_id=self.model, effort=self.effort, task_mode='worker',
            parameters_json=canonical_json({'intervention_id': record['intervention_id'], 'source': source['target']}),
            dependency_hash=digest(source['relations']), definitions_hash=contract.scope.definitions_hash,
            required_fact_ids=facts, required_evidence_ids=evidence,
        )
        return CandidateAction(candidate_id=record['candidate_id'], action_spec=spec,
            context_request=context, context_seed=seed, obligation_id=obligation.obligation_id,
            mechanism_family=intent + ':' + record['intervention_id'], action_kind=action,
            slot_kind=slot_kind_for_action(action), budget_class=BudgetClass.NORMAL,
            requested=ResourceVector.from_dict(record['request']['budget']),
            priority_class=PriorityClass.LOAD_BEARING, dependency_validity=DependencyValidity.VALID,
            recurrence_recommendation=RecurrenceRecommendation.CONTINUE,
            recurrence_audit_required=False, created_sequence=len(list(self.project.event_store.iter_project(self.project.project_id))))

    def start(self, iid: str, *, key: str, launch: bool = False) -> dict:
        p = self.project
        with admission_lock(p.project_dir):
            item = self.get(iid)
            if item['state'] in {'SETTLED', 'CANCELLED', 'FAILED'}:
                return item
            if item['execution_policy'] != self.execution_policy():
                raise InterventionError('execution policy or native worker configuration changed; review a new request')
            if launch:
                role = item['execution_policy']['native_role']
                if not role or role['MODEL'] != self.model or role['REASONING_EFFORT'] != self.effort:
                    raise InterventionError('configured model/effort does not match the actual native worker')
            current = resolve_object(p, item['target']['object_type'], item['target']['object_id'])
            assert_same_target(current, item['target'])
            if campaign_snapshot(p)['state'] in {'STOP_REQUESTED', 'STOPPED'}:
                raise CampaignError('campaign stopped')
            # A start is durably explicit; retry can recover a pre-start interruption.
            append(p, self.actor, 'euler.focused_started', 'start:' + iid,
                   {'intervention_id': iid, 'candidate_id': item['candidate_id'], 'actor': self.actor})
            from .controller import EulerController
            ctl = EulerController(p, actor=self.actor)
            ctl.schedule()
            binding = ctl.dispatch(item['candidate_id'], launch=launch)
            append(p, self.actor, 'euler.human_dispatched', 'dispatch:' + binding.lease_id,
                   {'intervention_id': iid, 'candidate_id': item['candidate_id'],
                    'lease_id': binding.lease_id, 'context_hash': binding.packet_hash,
                    'task_hash': binding.task_hash})
            return self.get(iid)

    def get(self, iid: str) -> dict:
        p = self.project
        events = list(p.event_store.iter_project(p.project_id))
        created = next((dict(e.payload) for _, e in events if e.event_type == 'euler.focused_created'
                        and e.payload['intervention_id'] == iid), None)
        if created is None:
            raise InterventionError('unknown investigation')
        leases = [l for l in p.runtime_snapshot().leases if l.candidate_id == created['candidate_id']]
        started = any(e.event_type == 'euler.focused_started' and e.payload['intervention_id'] == iid for _, e in events)
        state = 'AUTHORIZED' if started else 'AWAITING_START'
        if leases:
            status = leases[-1].status.value
            state = {'COMPLETED':'SETTLED','ISSUED':'READY','RUNNING':'RUNNING',
                     'CANCELLED':'CANCELLED','INFRA_FAILED':'FAILED','EXPIRED':'FAILED'}.get(status,status)
        ids = {l.lease_id for l in leases}
        timeline = [{'seq': seq, 'event_id': e.event_id, 'type': e.event_type,
                     'actor': e.actor, 'timestamp': e.timestamp_utc, 'payload': dict(e.payload)}
                    for seq, e in events if e.payload.get('intervention_id') == iid or
                    e.payload.get('lease_id') in ids or (e.payload.get('lease') or {}).get('lease_id') in ids or
                    (e.payload.get('binding') or {}).get('lease_id') in ids or
                    (e.payload.get('obligation') or {}).get('obligation_id') == created['obligation_id'] or
                    e.payload.get('obligation_id') == created['obligation_id'] or
                    (e.payload.get('contract') or {}).get('contract_id') == created['child_contract_id']]
        obligation = p.research_view().get_obligation(created['obligation_id'])
        observation = None
        status_path = p.project_dir / 'workers' / created['execution_policy']['worker'] / '.status.json'
        if leases and status_path.is_file() and not status_path.is_symlink() and status_path.stat().st_size < 64000:
            import json
            from datetime import datetime
            try:
                status_data = json.loads(status_path.read_text())
                issued = datetime.fromisoformat(leases[-1].issued_at.replace('Z','+00:00')).timestamp()
                if status_data.get('worker') == created['execution_policy']['worker'] and status_data.get('updated_at',0) >= issued:
                    observation = {'state':status_data.get('state','UNKNOWN'),
                                   'updated_at':status_data.get('updated_at'),
                                   'note':'Worker observation; lease settlement and mathematical status are separate'}
            except (ValueError, TypeError, OSError):
                pass
        return {**created, 'state': state, 'runtime_observation': observation, 'mathematical_status': obligation.status.value,
                'leases': [l.to_dict() for l in leases], 'evidence_refs': list(obligation.evidence_refs),
                'timeline': timeline, 'campaign': campaign_snapshot(p)}

    def list(self, *, object_id: str | None = None) -> list[dict]:
        ids = [e.payload['intervention_id'] for _, e in self.project.event_store.iter_project(self.project.project_id)
               if e.event_type == 'euler.focused_created' and
               (object_id is None or e.payload['target']['object_id'] == object_id)]
        return [self.get(i) for i in ids]


def reconcile_human_outcome(project, binding, reconciliation):
    """Called after normal settlement, without synthesizing checks or progress."""
    from .campaign import focused_record
    item = focused_record(project, binding.candidate_id)
    if not item:
        return
    obligation = project.research_view().get_obligation(item['obligation_id'])
    append(project, 'reconciler', 'euler.human_outcome_reconciled', reconciliation.reconciliation_id,
           {'intervention_id': item['intervention_id'], 'candidate_id': binding.candidate_id,
            'lease_id': binding.lease_id, 'source_target': item['target'],
            'reconciliation': reconciliation.to_dict(), 'obligation_id': item['obligation_id'],
            'mathematical_status': obligation.status.value, 'evidence_refs': list(obligation.evidence_refs)})
