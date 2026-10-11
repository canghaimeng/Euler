#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""EXAMPLE, NOT CORE: create one personal Euler research workspace. No model is contacted.

From a research definition (see research.json next to this file) it writes the exact
inputs used by ``euler init`` and ``euler-team init`` and then runs those same steps:

  WORKSPACE/setup/       contract.json, policy.json, team.json and a copy of the definition
  WORKSPACE/projects/ID  native Euler project: problem contract, target obligation,
                         task budget and the registered (not yet connected) team
  WORKSPACE/history/ID   private Git milestone store, outside the project
  WORKSPACE/workbench/   local workbench configuration and start-workbench.sh

It refuses to write into a non-empty directory, never copies provider credentials and
never starts a provider session. Connecting sessions is the separate, explicit step
``euler-team prepare --project WORKSPACE/projects/ID``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path

from euler.control.cli import do_init
from euler.control.contracts import MathematicalScope, ObjectBinding, ProblemContract, QuantifierBinding
from euler.control.policy import BudgetPolicy
from euler.workspace.milestones import enable
from euler.workspace.team import initialize, public_state, validate_member, validate_turn_limit
from euler.workspace.isolation import host_support
from euler.workspace.team_acp import binary_for

HERE = Path(__file__).resolve().parent
TOP = {"id", "title", "note", "problem", "team", "task_budget", "workbench"}
PROBLEM = {"statement", "assumptions", "parameter_domain", "definitions", "quantifiers", "objects",
           "target_kind", "required_evidence_level", "completion_checks", "external_help_policy",
           "permitted_tools"}
TEAM = {"objective", "max_turns", "turn_seconds", "members"}
WORKBENCH = {"host", "port"}


class SetupError(ValueError):
    pass


def _keys(label, value, allowed, required):
    if not isinstance(value, dict):
        raise SetupError(f"{label} must be a JSON object")
    unknown, missing = set(value) - allowed, set(required) - set(value)
    if unknown:
        raise SetupError(f"{label}: unknown field(s) {sorted(unknown)}")
    if missing:
        raise SetupError(f"{label}: missing field(s) {sorted(missing)}")


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_inputs(definition):
    _keys("definition", definition, TOP, TOP - {"note", "workbench"})
    identifier = definition["id"]
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identifier):
        raise SetupError("id: use letters, digits, '-' or '_' (at most 64 characters)")
    problem, team = definition["problem"], definition["team"]
    _keys("problem", problem, PROBLEM, PROBLEM - {"quantifiers", "objects", "permitted_tools"})
    _keys("team", team, TEAM, TEAM - {"objective", "max_turns"})
    _keys("workbench", definition.get("workbench", {}), WORKBENCH, set())
    port = definition.get("workbench", {}).get("port", 8130)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise SetupError("workbench.port must be an integer between 1 and 65535")
    # Checked before anything is written; euler.workspace.team.initialize stays authoritative.
    members = team["members"]
    if not isinstance(members, list) or not members:
        raise SetupError("team.members must be a non-empty list")
    ids = [m.get("id") for m in members if isinstance(m, dict)]
    if len(ids) != len(members) or len(set(ids)) != len(ids) or not all(
            isinstance(i, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", i) for i in ids):
        raise SetupError("team.members: each member needs a unique id of letters, digits, '-' or '_'")
    for m in members:
        validate_member(m)
        if m.get("role") not in {"lead", "assistant", "verifier"}:
            raise SetupError(f"team member {m['id']}: role must be lead, assistant or verifier")
    if [m["role"] for m in members].count("lead") != 1:
        raise SetupError("team: exactly one member must have role 'lead'")
    validate_turn_limit(team.get("max_turns"))
    if not isinstance(team["turn_seconds"], int) or not 10 <= team["turn_seconds"] <= 3600:
        raise SetupError("team.turn_seconds must be an integer from 10 to 3600")
    scope = MathematicalScope.build(
        assumptions=problem["assumptions"],
        quantifiers=[QuantifierBinding.from_dict(q) for q in problem.get("quantifiers", [])],
        objects=[ObjectBinding.from_dict(o) for o in problem.get("objects", [])],
        parameter_domain=problem["parameter_domain"],
        definitions_hash=hashlib.sha256(problem["definitions"].encode("utf-8")).hexdigest(),
    )
    contract = ProblemContract.build(
        problem_id=identifier, version=1, exact_statement=problem["statement"], scope=scope,
        permitted_tools=problem.get("permitted_tools", []),
        external_help_policy=problem["external_help_policy"],
        target_kind=problem["target_kind"], required_evidence_level=problem["required_evidence_level"],
        completion_checks=problem["completion_checks"], budget_policy_ref=identifier + "-task-budget",
    )
    policy = BudgetPolicy.from_dict(definition["task_budget"])
    team_definition = {
        "objective": team.get("objective") or problem["statement"],
        "max_turns": team.get("max_turns"), "turn_seconds": team["turn_seconds"], "members": team["members"],
    }
    return identifier, contract, policy, team_definition


def host_warnings(members):
    """Read-only readiness hints; nothing here starts a provider or reads a credential."""
    notes = []
    support = host_support()
    if not support['supported']:
        notes.append(support['detail'])
    for provider in sorted({m["provider"] for m in members}):
        try:
            binary_for(provider)
        except ValueError as exc:
            notes.append(str(exc))
    return notes


def create(definition, workspace):
    identifier, contract, policy, team_definition = build_inputs(definition)
    workspace = workspace.expanduser().resolve()
    if workspace.exists() and (not workspace.is_dir() or any(workspace.iterdir())):
        raise SetupError(f"{workspace} already exists and is not empty; choose a new directory")
    setup, projects, workbench = workspace / "setup", workspace / "projects", workspace / "workbench"
    project, history = projects / identifier, workspace / "history" / identifier
    for folder in (setup, project, history.parent, workbench):
        folder.mkdir(parents=True, exist_ok=True)

    _write(setup / "research.json", definition)
    _write(setup / "contract.json", contract.to_dict())
    _write(setup / "policy.json", policy.to_dict())
    _write(setup / "team.json", team_definition)
    _write(project / "project.json", {"name": identifier})
    problem = definition["problem"]
    (project / "PROBLEM.md").write_text(
        f"# {definition['title']}\n\n" + (f"> {definition['note']}\n\n" if definition.get("note") else "")
        + f"## Exact statement\n\n{problem['statement']}\n\n## Parameter domain\n\n{problem['parameter_domain']}\n\n"
        + "## Assumptions\n\n" + ("".join(f"- {a}\n" for a in problem["assumptions"]) or "None stated.\n")
        + f"\n## Definitions\n\n{problem['definitions']}\n", encoding="utf-8")

    # Equivalent to: EULER_AGENTS_ROOT=WORKSPACE/projects euler init ID --contract ... --policy ...
    previous = os.environ.get("EULER_AGENTS_ROOT")
    os.environ["EULER_AGENTS_ROOT"] = str(projects)
    try:
        native = do_init(identifier, contract=setup / "contract.json", policy=setup / "policy.json")
    finally:
        if previous is None:
            os.environ.pop("EULER_AGENTS_ROOT", None)
        else:
            os.environ["EULER_AGENTS_ROOT"] = previous
    # Equivalent to: euler-milestones enable --project P --store H; euler-team init --project P --definition team.json
    milestones = enable(project, store=history)
    initialize(project, team_definition["members"], objective=team_definition["objective"],
               max_turns=team_definition["max_turns"], turn_seconds=team_definition["turn_seconds"])

    host = definition.get("workbench", {}).get("host", "127.0.0.1")
    port = definition.get("workbench", {}).get("port", 8130)
    _write(workbench / "dashboard.json", {"name": definition["title"], "description": definition.get("note", ""),
                                          "project_catalog": "projects.json"})
    _write(workbench / "projects.json", {"projects": [{"id": identifier, "name": definition["title"],
                                                       "project_dir": str(project),
                                                       "description": "Personal research workspace"}]})
    _write(workbench / "team-policy.json", {"projects": {identifier: {
        "allow_prepare": True, "allow_launch": True,
        "max_turns": team_definition["max_turns"], "turn_seconds": team_definition["turn_seconds"]}}})
    _write(workbench / "milestone-policy.json", {"projects": {identifier: {"settings": milestones, "capture": True}}})
    _write(workbench / "restored-workspaces.json", {"projects": []})
    _write(workbench / "created-projects.json", {"projects": []})
    defaults={m['provider']:{'model':m['model'],'effort':m['effort']} for m in team_definition['members']}
    _write(workbench / "project-creation.json", {'allow_create':True,'root':str(workspace/'created'),
        'registry':str(workbench/'created-projects.json'),'defaults':defaults,'turn_seconds':team_definition['turn_seconds']})
    q = shlex.quote
    script = workbench / "start-workbench.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "# Generated by examples/personal-research/create_workspace.py.\n"
        "# Starts the local workbench. It does not start the team; questions start work.\n"
        "set -euo pipefail\n"
        f"export EULER_TEAM_CONFIG={q(str(workbench / 'team-policy.json'))}\n"
        f"export EULER_MILESTONE_CONFIG={q(str(workbench / 'milestone-policy.json'))}\n"
        f"export EULER_MILESTONE_REGISTRY={q(str(workbench / 'restored-workspaces.json'))}\n"
        f"export EULER_PROJECT_CREATION_CONFIG={q(str(workbench / 'project-creation.json'))}\n"
        f"exec {q(sys.executable)} -m euler.observability --project {q(str(project))} \\\n"
        f"  --config {q(str(workbench / 'dashboard.json'))} --host {q(host)} --port {port}\n",
        encoding="utf-8")
    script.chmod(0o755)
    team = public_state(project)
    return {
        "workspace": str(workspace), "project": str(project), "history": milestones["store"],
        "workbench": str(script), "url": f"http://{host}:{port}/",
        "contract_id": native["contract_id"], "target_obligation_id": native["target_obligation_id"],
        "team": {"state": team["state"], "lead": team["lead"],
                 "members": [f"{m['id']} ({m['role']}, {m['provider']} {m['model']} {m['effort']})" for m in team["members"]]},
        "next": [f"euler-team prepare --project {q(str(project))}", f"bash {q(str(script))}"],
        "host_warnings": host_warnings(team_definition["members"]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--definition", type=Path, default=HERE / "research.json", help="research definition JSON")
    parser.add_argument("--workspace", type=Path, required=True, help="new, empty directory for this research")
    args = parser.parse_args()
    try:
        definition = json.loads(args.definition.read_text(encoding="utf-8"))
        result = create(definition, args.workspace)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"setup refused: {exc}") from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
