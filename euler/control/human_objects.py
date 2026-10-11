# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Exact public mathematical identity. No display-label/semantic-ID matching."""
from __future__ import annotations

from .adapters.project import snapshot_project
from .ids import canonical_json, sha256_hex


class ObjectConflict(ValueError):
    pass


def digest(value):
    return sha256_hex(canonical_json(value))


def resolve_object(project, kind: str, object_id: str) -> dict:
    view = project.research_view()
    methods = {'obligation': view.get_obligation, 'route': view.get_route,
               'blocker': view.get_failure, 'evidence': view.get_evidence,
               'relation': view.get_relation, 'contract': view.get_contract}
    if kind == 'fact':
        fact = next((f for f in snapshot_project(project.project_dir).active_facts if f.fact_id == object_id), None)
        if fact is None:
            raise ObjectConflict('unknown or revoked Fact')
        links = [dict(e.payload) for _, e in project.event_store.iter_project(project.project_id)
                 if e.event_type == 'euler.fact_linked' and e.payload.get('fact_id') == object_id]
        linked = next((view.get_obligation(l['obligation_id']) for l in reversed(links)
                       if view.get_obligation(l['obligation_id']) is not None and
                       l.get('dependency_hashes', {}).get(object_id) == fact.content_sha256), None)
        scope = linked.scope.to_dict() if linked else None
        raw = {'fact_id': fact.fact_id, 'statement': fact.statement, 'content_hash': fact.content_sha256,
               'predecessors': list(fact.predecessors), 'scope': scope,
               'contract_id': linked.contract_id if linked else None}
        statement, contract_id = fact.statement, raw['contract_id']
        status = 'ADMITTED'
        evidence = [ev.to_dict() for link in links if (ev := view.get_evidence(link.get('evidence_id'))) is not None]
        writable = linked is not None and bool(evidence) and all(e.get('validity') == 'VALID' for e in evidence)
        reason = '' if writable else 'Fact has no verified native scope/contract binding; inspect only'
    elif kind == 'assumption':
        # Assumptions are addressed by exact owner ID + index, never by their text.
        try:
            owner_id, index = object_id.rsplit(':', 1)
            owner = resolve_object(project, 'obligation', owner_id)
            statement = owner['scope']['assumptions'][int(index)]
            if str(int(index)) != index or int(index) < 0:
                raise ValueError()
        except (ValueError, TypeError, IndexError, KeyError):
            raise ObjectConflict('unknown assumption identity')
        raw = {'owner': owner['target'], 'assumption_index': int(index), 'statement': statement}
        scope, contract_id = owner['scope'], owner['target']['contract_id']
        status, evidence = owner['status'], owner['evidence']
        writable, reason = owner['writable'], owner['readonly_reason']
    else:
        if kind not in methods:
            raise ObjectConflict('unsupported mathematical object type')
        record = methods[kind](object_id)
        if record is None:
            raise ObjectConflict('no canonical backend identity; curated/historical projection is read-only')
        raw = record.to_dict()
        owner_id = raw.get('target_obligation_id')
        if kind == 'relation':
            owner_id = raw.get('source')
            owner_route = view.get_route(owner_id)
            if owner_route:
                owner_id = owner_route.target_obligation_id
        owner = view.get_obligation(owner_id) if owner_id else None
        contract_id = raw.get('contract_id') or (owner.contract_id if owner else None)
        scope = raw.get('scope') or (owner.scope.to_dict() if owner else None)
        statement = (raw.get('exact_statement') or raw.get('exact_claim') or raw.get('mechanism')
                     or raw.get('exact_failed_claim') or raw.get('obstruction') or raw.get('relation_type') or '')
        status = raw.get('status', raw.get('checker_result', 'RECORDED'))
        evidence = [e.to_dict() for ref in raw.get('evidence_refs', raw.get('witness_refs', []))
                    if (e := view.get_evidence(ref)) is not None]
        writable = scope is not None and contract_id is not None and raw.get('validity', 'VALID') == 'VALID'
        if kind == 'evidence':
            evidence = [raw]
        if any(e.get('validity') != 'VALID' for e in evidence):
            writable = False
        reason = '' if writable else 'Unknown scope/contract or invalidated supporting evidence; inspect only'
    routes = list(view.alternatives(object_id)) if kind == 'obligation' else []
    if kind == 'route':
        routes = [view.get_route(object_id)]
    premise_ids = {oid for route in routes for oid in route.required_obligation_ids}
    premises = [view.get_obligation(oid).to_dict() for oid in sorted(premise_ids)
                if view.get_obligation(oid) is not None]
    if any(p.get('validity') != 'VALID' for p in premises):
        writable, reason = False, 'A prerequisite has invalidated scope/evidence; review the current dependency state'
    linked_facts = []
    # Read-only validation of registered Fact dependency bindings. Do not wait
    # for a scheduler pass to discover an on-disk revocation or changed source.
    relevant = {e.get('artifact_id') for e in evidence}
    relevant.update(ref for premise in premises for ref in premise.get('evidence_refs', []))
    dependency_state = []
    public_hashes = None
    for _, event in project.event_store.iter_project(project.project_id):
        if event.event_type != 'euler.fact_linked':
            continue
        link = event.payload
        if link.get('evidence_id') not in relevant and link.get('obligation_id') not in premise_ids | {object_id} and not (kind == 'fact' and link.get('fact_id') == object_id):
            continue
        if public_hashes is None:
            public_hashes = {f.fact_id:f.content_sha256 for f in snapshot_project(project.project_dir).active_facts}
        valid = all(public_hashes.get(fid) == hashed for fid,hashed in link.get('dependency_hashes',{}).items())
        dependency_state.append({'evidence_id':link.get('evidence_id'), 'valid':valid})
        if valid and link.get('fact_id'):
            linked_facts.append(link['fact_id'])
        if not valid:
            writable, reason = False, 'A recorded Fact dependency changed or was revoked; a fresh scope/evidence review is required'
    contract = view.get_contract(contract_id) if contract_id else None
    links = [r.to_dict() for r in view.relations() if object_id in (r.source, r.target)]
    dependencies = {'relations': links, 'routes': [r.to_dict() for r in view.alternatives(owner_id if kind == 'route' else object_id)],
                    'evidence': evidence}
    identity = {'record': raw, 'dependencies': dependencies, 'dependency_state': dependency_state}
    if premises:
        identity['premises'] = premises
    version = digest(identity)
    target = {'project_id': project.project_id,
              'project_instance_hash': digest({'root': str(project.project_dir.resolve()), 'manifest': project.manifest.manifest_hash}),
              'object_type': kind, 'object_id': object_id,
              'object_version': raw.get('version'), 'object_hash': version,
              'statement_hash': digest(statement), 'scope_hash': digest(scope),
              'contract_id': contract_id, 'contract_version': contract.version if contract else None}
    return {'target': target, 'statement': statement, 'scope': scope, 'status': status,
            'record': raw, 'relations': links, 'evidence': evidence,
            'premises': premises, 'source_fact_ids': sorted(set(linked_facts)),
            'alternative_routes': dependencies['routes'], 'writable': writable,
            'readonly_reason': reason,
            'unknown_scope_fields': [k for k in ('assumptions', 'quantifiers', 'objects', 'parameter_domain') if not (scope or {}).get(k)],
            'source': {'kind': 'canonical_public_record', 'object_id': object_id},
            'assumptions': [{'object_type': 'assumption', 'object_id': f'{object_id}:{i}', 'statement': a}
                            for i, a in enumerate((scope or {}).get('assumptions', []))] if kind == 'obligation' else []}


def assert_same_target(current: dict, target: dict):
    if current['target'] != target:
        raise ObjectConflict('object identity, version, scope or evidence changed; review a new preview')
    if not current['writable']:
        raise ObjectConflict(current['readonly_reason'])
