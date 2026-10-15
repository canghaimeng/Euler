# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Restored-team mailbox; no admission of mathematical facts or checker verdicts."""
import hashlib,json,os
import functools
import inspect
from pathlib import Path
from euler._mcp import FastMCP, ToolError
from euler.control.views import ResearchViewError
from .team import load,public_state,queue_question,add_member

app=FastMCP('euler-team')

def public_tool(fn):
    """Return actionable validation failures across both MCP SDK generations."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except (ValueError, KeyError, TypeError, ResearchViewError) as exc:
                raise ToolError(str(exc)) from exc
    else:
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (ValueError, KeyError, TypeError, ResearchViewError) as exc:
                raise ToolError(str(exc)) from exc
    wrapped.__signature__ = inspect.signature(fn, eval_str=True)
    return wrapped



def identity():
    root=Path(os.environ['EULER_TEAM_PROJECT']).resolve();member=os.environ['EULER_TEAM_MEMBER'];s=load(root)
    if not s or member not in {m['id'] for m in s['members']}:raise ValueError('unregistered team identity')
    return root,member,s


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


def euler_team_messages() -> dict:
    """Read public team exchanges and pending requests; no private reasoning."""
    root,member,s=identity()
    return {'messages':[m for m in s['messages'] if member in [m['request']['recipient'],m['request']['sender']] or member==s['lead']]}


def euler_team_propose_steps(statements: list[str], mechanism: str,
                             parent_obligation_id: str | None = None) -> dict:
    """Lead: record precise prerequisites and their proposed closing argument.

    Use parent_obligation_id to refine an existing step; omit it for the root.
    Each step inherits that parent's scope. These are open proposals, not proofs.
    """
    root, member, state = identity()
    if member != state['lead']:
        raise ValueError('only the lead records the research plan')
    from .team_research import propose_steps
    return propose_steps(root, member, statements, mechanism, parent_obligation_id)


from .team_research import TOOLS as RESEARCH_TOOLS
for tool in [euler_team_members, euler_team_create_member, euler_team_ask,
             euler_team_messages, euler_team_propose_steps, *RESEARCH_TOOLS]:
    app.tool()(public_tool(tool))

if __name__ == '__main__':
    app.run()
