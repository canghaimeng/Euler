# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Restored-team mailbox; no admission of mathematical facts or checker verdicts."""
import hashlib,json,os
from pathlib import Path
from euler._mcp import FastMCP
from .team import load,public_state,queue_question,add_member

app=FastMCP('euler-team')


def identity():
    root=Path(os.environ['EULER_TEAM_PROJECT']).resolve();member=os.environ['EULER_TEAM_MEMBER'];s=load(root)
    if not s or member not in {m['id'] for m in s['members']}:raise ValueError('unregistered team identity')
    return root,member,s


@app.tool()
def euler_team_members() -> dict:
    """List this team's roles, assignments and restored session identities."""
    root,_,_=identity();s=public_state(root)
    from .team_acp import BINARIES,binary_for
    available=[]
    for provider in BINARIES:
        try:binary_for(provider);available.append(provider)
        except ValueError:pass
    return {'lead':s['lead'],'members':s['members'],'objective':s['objective'],'source':s['source'],
            'available_clis':available}


@app.tool()
def euler_team_create_member(name: str, provider: str, model: str, effort: str,
                             assignment: str, role: str = 'assistant', max_turns: int | None = None) -> dict:
    """Lead only: add a configured member to this branch, then use euler_team_ask.

    Providers: codex, claude-code, gemini, grok, cursor. Gemini/Cursor use
    effort=model-defined. No arbitrary binaries, credentials or paths are accepted.
    This creates a real member workspace and preserves its native session in future milestones.
    """
    root,sender,state=identity()
    active=next((m for m in state['messages'] if m['state']=='RUNNING' and m['request']['recipient']==sender),None)
    if sender!=state['lead'] or not active:raise ValueError('only the active lead may add members')
    from .team_acp import binary_for
    normalized={'claude':'claude-code','cursor-agent':'cursor'}.get(provider,provider)
    if normalized not in {'codex','claude-code','gemini','grok','cursor'}:raise ValueError('unknown CLI provider')
    binary_for(normalized)
    definition={'name':name,'provider':provider,'model':model,'effort':effort,'assignment':assignment,'role':role,'max_turns':max_turns}
    key=hashlib.sha256(json.dumps([active['id'],definition],sort_keys=True).encode()).hexdigest()
    return add_member(root,definition,key=key,actor='agent:'+sender)


@app.tool()
def euler_team_ask(member: str, question: str, object_id: str | None = None) -> dict:
    """Delegate a bounded question to another member. Its reply returns to you."""
    root,sender,s=identity()
    if sender!=s['lead']:raise ValueError('only the lead may schedule team consultations')
    if member==sender:raise ValueError('choose another member')
    active=next((m for m in s['messages'] if m['state']=='RUNNING' and m['request']['recipient']==sender),None)
    if not active:raise ValueError('delegation requires an active lead turn')
    object_id=object_id or active['request'].get('object_id')
    key=hashlib.sha256(json.dumps([active['id'],member,question,object_id]).encode()).hexdigest()
    result=queue_question(root,member,question,key=key,object_id=object_id,sender=sender,parent=active['id'])
    return {'message_id':result['id'],'state':result['state'],'delivery':'The runner will consult this member and return its reply to your existing conversation.'}


@app.tool()
def euler_team_messages() -> dict:
    """Read public team exchanges and pending requests; no private reasoning."""
    root,member,s=identity()
    return {'messages':[m for m in s['messages'] if member in [m['request']['recipient'],m['request']['sender']] or member==s['lead']]}


@app.tool()
def euler_team_propose_steps(statements: list[str], mechanism: str) -> dict:
    """Record proposed proof obligations and an AND route; never mark them proved."""
    root,member,s=identity()
    if member!=s['lead']:raise ValueError('only the lead records the research plan')
    if not 1<=len(statements)<=12 or any(not x.strip() or len(x)>4000 for x in statements):raise ValueError('bounded precise statements required')
    from euler.control.project import open_project
    from euler.control.obligations import Obligation,Route
    from euler.control.research_service import ResearchService
    with open_project(root) as project:
        contract=project.research_view().get_contract(project.manifest.contract_id)
        obligations=[Obligation.build(contract_id=contract.contract_id,exact_statement=x,scope=contract.scope,
            desired_output='A checked proof step',created_by=member,version=1) for x in statements]
        route=Route.build(target_obligation_id=project.manifest.target_obligation_id,mechanism=mechanism,
                          required_obligation_ids=[o.obligation_id for o in obligations])
        ResearchService(project,actor=member).apply([
            *[{'event_type':'obligation.created','payload':{'obligation':o.to_dict()}} for o in obligations],
            {'event_type':'route.created','payload':{'route':route.to_dict()}}])
        return {'target':project.manifest.target_obligation_id,'obligations':[o.obligation_id for o in obligations],
                'route':route.route_id,'status':'OPEN; proposals only, not Fact admission'}


if __name__=='__main__':app.run()
