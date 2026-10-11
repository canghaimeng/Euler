# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Adapt source-bound historical projects to the standard workbench read models.

No EventStore, FactGraph or runtime mutations. Derived nodes retain their source
provenance and never impersonate kernel checks or controller admissions.
"""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
from .case_study import get_case, get_source


def attach(data: dict, config: dict, *, project=None) -> dict:
    identifier = config.get('case_study_id')
    if not identifier:
        return data
    case = get_case(identifier)
    if not case:
        return data
    public = {k: case.get(k) for k in ('id', 'result', 'verification', 'scope', 'frontier', 'edition', 'source_integrity', 'archive_coverage')}
    data['workspace']['historical_case'] = public
    if case['source_integrity'] != 'matched':
        data['workspace']['warnings'].append('Historical source bindings changed; derived structure is withdrawn.')
        return data
    for fact_node in data.get('facts_graph',{}).get('nodes',[]):
        fid=fact_node['id']; label=case.get('fact_labels',{}).get(fid)
        if label and data['facts'].get(fid,{}).get('source_sha256')==label['sha256']:
            fact_node['display_title']=label['title']['en']
            fact_node['display_title_i18n']=label['title']
    prefix = 'history:' + identifier + ':'
    nodes, edges, bodies = [], [], {}
    def node(key, title, statement, kind, sources, detail=None, status='DOCUMENTED', depth=0):
        ident=prefix+key
        title=title if isinstance(title,dict) else {'en':title,'zh':title}
        statement=statement if isinstance(statement,dict) else {'en':statement,'zh':statement}
        item={'id':ident,'kind':kind,'label':title['en'],'display_title':title['en'],
              'display_title_i18n':title,'statement':statement['en'],'status':status,'validity':'VALID',
              'depth':depth,'author':'Historical source records','team_id':None,'created_at':None,
              'predecessors':[],'projection':'historical_source','source_ids':sources}
        nodes.append(item)
        bodies[ident]={'sources':sources,'summary':detail or statement,'title':title}
        return ident
    def edge(a,b,kind='REQUIRES',reason=''):
        edges.append({'source':a,'target':b,'type':kind,'validity':'VALID','exact_claim':reason,
                      'projection':'historical_source'})
    # Replay all shared verification records, retaining transport/FAIL/PASS distinctions.
    verification_rows=[]
    if project:
        from .monitor import _tail_jsonl
        verification_rows=_tail_jsonl(Path(project)/'global_memory'/'verification.jsonl', Path(project), [], limit=8*1024*1024)
    fact_checks={}
    for record in verification_rows:
        if record.get('verdict')=='correct' and record.get('transport_status') in (None,'ok') and record.get('fact_id'):
            fact_checks.setdefault(record['fact_id'],[]).append(record)
    context_ids={e["source"] for e in case["edges"] if e["kind"] in {"scope","motivates"}}
    proof={}
    bindings={}
    for n in case['nodes']:
        binding=n.get('verification_binding',{})
        status=n.get('recorded_status','DOCUMENTED')
        kind=n.get('record_kind') or ('route' if n['kind']=='route' else 'obligation')
        review=None
        if binding.get('kind')=='accepted_facts':
            fids=binding.get('fact_ids',[])
            accepted=bool(fids) and all(data['facts'].get(fid,{}).get('status')=='accepted' and data['facts'].get(fid,{}).get('source_sha256')==binding.get('fact_sha256',{}).get(fid) and fact_checks.get(fid) for fid in fids)
            if accepted:
                status='REFUTED' if binding.get('refutes') else 'COMPLETED' if kind=='route' else 'DISCHARGED'
                review={**binding,'verified_fact_ids':fids,'verification_record_ids':[r.get('id') for fid in fids for r in fact_checks[fid]]}
        elif binding.get('kind')=='archived_kernel':
            audit_source=get_source(identifier,'n-audit')
            map_source=get_source(identifier,'n-declarations')
            if audit_source and map_source and audit_source['integrity']==map_source['integrity']=='matched':
                audit=json.loads(audit_source['text']); declaration_map=json.loads(map_source['text'])
                named={r['label']:r['row'] for r in declaration_map['rows']}
                covered=all(label in named and 'KERNEL-CHECKED' in named[label] for label in binding['labels'])
                req=audit.get('requirements',{})
                if covered and audit.get('mathematical_goal_achieved') and req.get('final_full_source_audit') and req.get('exact_target_kernel_closed') and req.get('default_whole_lake_build',{}).get('exit_code')==0:
                    status='COMPLETED' if kind=='route' else 'DISCHARGED'
                    review={**binding,'checked_at':audit.get('checked_at'),'transitive_axioms':req.get('transitive_axioms',[])}
        proof[n['id']]=node('proof:'+n['id'],n['title'],n['summary'],kind,
            list(dict.fromkeys(n['sources']+binding.get('sources',[]))),n['detail'],status=status,depth=n['column'])
        nodes[-1]['state_basis']=review or {'kind':'source_record','review_note':'No mathematical acceptance is inferred from a source link alone.'}
        if binding and not review:
            nodes[-1]['validity']='INVALID'
            nodes[-1]['state_basis']={'kind':'invalidated_binding','review_note':'The source hash, active Fact, or successful original check no longer matches this binding. Closure is withdrawn.'}
        if review:
            bindings[n['id']]=review
            if review['kind']=='accepted_facts':
                primary=data['facts'][review['verified_fact_ids'][0]]
                nodes[-1]['author']=primary.get('author','')
                dates=[record.get('timestamp_utc') for fid in review['verified_fact_ids'] for record in fact_checks[fid] if record.get('timestamp_utc')]
                nodes[-1]['status_changed_at']=max(dates) if dates else None
            else:
                nodes[-1]['author']='Lean'
                nodes[-1]['status_changed_at']=review.get('checked_at')
    # Use the actual admitted Fact nodes and their recorded predecessor edges.
    for fact_node in data.get('facts_graph',{}).get('nodes',[]):
        nodes.append(dict(fact_node))
    edges.extend(dict(e) for e in data.get('facts_graph',{}).get('edges',[]))
    for e in case['edges']:
        edge(proof[e['source']],proof[e['target']],
             'REQUIRES' if e['kind']=='requires' else 'HISTORICAL_CONTEXT',e['explanation']['en'])
    evidence={}
    for sid,source in case['sources'].items():
        evidence[sid]=node('source:'+sid,source['label'],source['label'],'evidence',[sid])
        nodes[-1].update(evidence_kind='HISTORICAL_SOURCE',checker_result=None,source_sha256=source['sha256'])
    for n in list(nodes):
        if n['kind']!='evidence':
            for sid in n.get('source_ids',[]):
                if sid in evidence:edge(evidence[sid],n['id'],'evidence','Frozen original source; not a new verification.')
    for record in verification_rows:
        rid=record.get('id')
        if not rid: continue
        eid=node('verification:'+rid, 'Verification · '+rid[:8],record.get('claim','Recorded verification'),
            'evidence',[],{'en':str(record.get('verification_report') or record.get('error') or record.get('verdict')),
                           'zh':str(record.get('verification_report') or record.get('error') or record.get('verdict'))},status='VALID')
        nodes[-1].update(checker_result=({'correct':'PASS','wrong':'FAIL'}.get(record.get('verdict'),'UNKNOWN') if record.get('transport_status') in (None,'ok') else 'UNKNOWN'),
            evidence_kind='LLM_CHECKED_PROOF',created_at=record.get('timestamp_utc'),
            original_record_id=rid,source_channel='verification',author=record.get('author',''))
        bodies[eid]['original_record']=record
        fid=record.get('fact_id')
        if fid in data['facts'] and record.get('verdict')=='correct':
            edge(eid,fid,'evidence','Original verifier record for this admitted Fact.')
        for key, binding in bindings.items():
            if rid in binding.get('verification_record_ids',[]):
                edge(eid,proof[key],'evidence','This original verification supports the reviewed component binding.')
    for key,binding in bindings.items():
        if binding['kind']=='archived_kernel':
            eid=node('kernel:'+key,{'en':'Lean check · '+key,'zh':'Lean 检查 · '+key},
                '\n'.join(binding['declarations']),'evidence',binding['sources'],status='VALID')
            nodes[-1].update(checker_result='PASS',evidence_kind='KERNEL_CHECKED_PROOF',
                created_at=binding.get('checked_at'),state_basis=binding)
            edge(eid,proof[key],'evidence','Existing full build and declaration-map audit; no new check is claimed.')
        else:
            for fid in binding['verified_fact_ids']:
                edge(fid,proof[key],'evidence','Exact component reviewed against the original accepted statement.')
    # A completed closing route makes the native main-line traversal explicit.
    if 'result' in bindings:
        target=proof['result']
        parents=[e for e in edges if e['target']==target and e['type']=='REQUIRES']
        closing=node('assembly',{'en':'Assemble the main theorem','zh':'主定理组装'},
            {'en':'Combine the checked prerequisites under the original scope.','zh':'在原始范围内组合已检查的前提。'},
            'route',case['outcome_sources'],status='COMPLETED',depth=max(n['depth'] for n in nodes)+1)
        for e in parents:e['target']=closing
        edge(closing,target,'targets','The source proof assembles these premises to establish the unchanged target.')
    stages=[]
    for index,s in enumerate(case.get('stages',[])):
        title={'en':f"{index+1}. {s['title']['en']}",'zh':f"{index+1}. {s['title']['zh']}"}
        description={lang:'\n\n'.join([s['date'], 'O · '+s['obligations'][lang], 'R · '+s['route'][lang],
            'B · '+s['blocker'][lang], 'E · '+s['evidence'][lang],
            ('Next action: ' if lang=='en' else '下一步：')+s['next_action'][lang]]) for lang in ('en','zh')}
        rid=node('stage:'+s['id']+':R',title,s['route'],'route',s['sources'],description,
                 status={'route_refuted':'REFUTED','redirected':'PARKED','recorded_complete':'COMPLETED'}.get(s.get('status'),'DOCUMENTED'),depth=index)
        nodes[-1]['historical_order'] = index
        oid=node('stage:'+s['id']+':O',{'en':'Obligation · '+s['title']['en'],'zh':'义务 · '+s['title']['zh']},s['obligations'],'obligation',s['sources'],description,depth=index)
        bid=node('stage:'+s['id']+':B',{'en':'Obstacle · '+s['title']['en'],'zh':'障碍 · '+s['title']['zh']},s['blocker'],'obligation',s['sources'],description,depth=index)
        edge(rid,oid,'targets','Historical route addressing the recorded obligation.')
        edge(bid,rid,'HISTORICAL_CONSTRAINT',s['blocker']['en'])
        for sid in s['sources']:
            if sid in evidence:edge(evidence[sid],rid,'evidence','Original evidence available at this historical stage.')
        if stages:edge(stages[-1]['node_id'],rid,'HISTORICAL_NEXT','Selected milestones; consult sources for the complete chronology.')
        stages.append({'node_id':rid,'title':title,'description':description,'date':s['date'],'index':index})
    # Two explicit formerly blocking interfaces have a later exact closing proof.
    # Keep the historical blocker and mark only that old blocking relation inactive.
    resolutions={'nikoghosyan':{'n3':'restore'},'jnw':{'j3':'restrict'}}
    node_lookup={n['id']:n for n in nodes}
    for sid, component in resolutions.get(identifier,{}).items():
        if component not in bindings: continue
        for suffix in (':O',':B'):
            record=node_lookup.get(prefix+'stage:'+sid+suffix)
            if record:
                record['status']='DISCHARGED'
                record['state_basis']={**bindings[component], 'review_note':'This recorded blocking interface is resolved by the later checked component '+component+'. The earlier source record remains preserved.'}
        for e in edges:
            if e['source']==prefix+'stage:'+sid+':B' and e['target']==prefix+'stage:'+sid+':R':
                e['type']='BLOCKED_BY';e['validity']='INVALID'
                e['exact_claim']='Historical blocking relation resolved by '+proof[component]+'. Inspect the linked later proof; this does not invalidate the historical observation.'
        edge(proof[component],prefix+'stage:'+sid+':O','evidence','Later accepted component closes the exact recorded interface.')
    if identifier=='jnw':
        for e in edges:
            if e['source']==prefix+'stage:j2:B' and e['target']==prefix+'stage:j2:R':
                e['type']='BLOCKED_BY'
                e['exact_claim']='The source explicitly records repeated positive candidate results as the obstacle to this retired search strategy.'
    if identifier=='nikoghosyan':
        edge(prefix+'stage:n2:R',prefix+'stage:n2:B','REFUTED_BY','The explicit seven-vertex counterexample rejects only the original terminal-cover candidate.')
    ids={n['id']:n for n in nodes}
    for e in edges:
        if e['type']=='REQUIRES':ids[e['target']]['predecessors'].append(e['source'])
    ids[proof['result']]['statement'] = case.get('target_statement') or case['question']['en'] + '\n\n' + case['scope']['en']
    raw=data['research_graph']
    # Original controller projection is retained separately and unmodified.
    data['original_research_graph']=raw
    data['research_graph']={'nodes':nodes,'edges':edges,'available':True,'warnings':[],
        'target_id':proof['result'],'max_depth':max(n['depth'] for n in nodes),
        'reading_order':[proof[k] for k in {'nikoghosyan':['factor','ports','cut','result'], 'lewis-overton':['orbit-a','orbit-b','rates','result'], 'jnw':['host','no-legal','collapse','result']}.get(identifier, [n['id'] for n in case['nodes'] if n['id'] not in context_ids])],
        'projection':'historical_source','case_id':identifier,'fact_target_id':case.get('fact_target_id'),
        'archive_coverage':case.get('archive_coverage',{})}
    fact_target = case.get('fact_target_id')
    if 'result' in bindings and fact_target in data['facts'] and data['facts'][fact_target].get('status') == 'accepted':
        data['facts_graph']['target_id'] = fact_target
        data['facts_graph']['reading_order'] = [fact_target]
    public['root_closed'] = 'result' in bindings
    public['root_state_basis'] = bindings.get('result')
    data['historical_documents']=bodies
    data['historical_stages']=stages
    return data


def derivation(data: dict, node_id: str) -> dict | None:
    document=data.get('historical_documents',{}).get(node_id)
    if not document:return None
    from .derivation import proof_sections
    case_id=data['research_graph']['case_id']
    graph=data['research_graph'];nodes={n['id']:n for n in graph['nodes']}
    def card(key):
        n=nodes[key]
        return {**n,'title':n.get('display_title') or n.get('label') or key,'summary':n.get('statement','')}
    fragments=[]
    for sid in document['sources']:
        source=(data['historical_sources'].get(sid) if 'historical_sources' in data else get_source(case_id,sid))
        if not source or source['integrity']!='matched':continue
        body=source['text']
        if source.get('format')!='md':body='````'+(source.get('format') or 'text')+'\n'+body+'\n````\n'
        fragments.append('# '+source['label']+'\n\n'+source['locator']+'\n\n'+body)
    if document.get('original_record'):
        fragments.append('# Original verification record\n\n````json\n'+json.dumps(document['original_record'],ensure_ascii=False,indent=2)+'\n````')
    proof='\n\n'.join(fragments)
    sections=proof_sections(proof)
    incoming=[e for e in graph['edges'] if e['target']==node_id]
    outgoing=[e for e in graph['edges'] if e['source']==node_id]
    sha=hashlib.sha256(proof.encode()).hexdigest()
    process=None
    if sections:
        first=sections[0]
        process={'status':'editorial','source_sha256':sha,'summary':document['summary'],
            'steps':[{'role':'argument','title':document['title'],'text':document['summary'],
                'cites':[{'section':first['index'],'start':first['start'],'end':first['end'],
                          'quote':first['text'][:100],'whole':True}],
                'refs':[e['source'] for e in incoming if e['type']=='REQUIRES']} ]}
    return {'node':card(node_id), 'premises':[dict(card(e['source']),relation=e['type'],relation_label=e['type'],rationale=e['exact_claim'],relation_validity='VALID') for e in incoming if e['type']=='REQUIRES'],
        'routes':[dict(card(e['source']),premises=[card(p['source']) for p in graph['edges'] if p['target']==e['source'] and p['type']=='REQUIRES'],mechanism=nodes[e['source']].get('statement','')) for e in incoming if e['type']=='targets'], 'evidence':[card(e['source']) for e in incoming if e['type']=='evidence'],
        'related':[dict(card(e['source'] if e['target']==node_id else e['target']),relation=e['type'],relation_label=e['type'],rationale=e['exact_claim']) for e in incoming+outgoing if e['type'] not in {'REQUIRES','evidence','targets'}],
        'next':[card(e['target']) for e in outgoing if e['type']=='REQUIRES'],
        'sections':sections,'proof':proof,'intuition':'','mechanism':'','source_kind':'historical_source',
        'proof_source_fact_id':None,'proof_sha256':sha,'process':process,'gap_code':None if proof else 'no_proof_text',
        'gap':None,'projection':'historical_source'}
