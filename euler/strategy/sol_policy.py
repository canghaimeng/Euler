# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""Consult scheduler for GPT-5.6 Sol — quota and trigger policy.

Official Euler consults a top model on a timer. That does not match a scarce
Sol budget. This module is the only place that decides whether a consult is
allowed; the main agent / ``bin/consult`` should call ``decide()`` first.

V1 engineering budget (not a Euler upstream parameter):
  SOL_SOFT_CAP_PER_THEOREM = 8
  SOL_HARD_CAP_PER_THEOREM = 12
  SOL_TAKEOVER_CAP = 3
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

SOFT_CAP = int(os.environ.get("SOL_SOFT_CAP_PER_THEOREM", "8"))
HARD_CAP = int(os.environ.get("SOL_HARD_CAP_PER_THEOREM", "12"))
TAKEOVER_CAP = int(os.environ.get("SOL_TAKEOVER_CAP", "3"))


class ResearchState(str, Enum):
    NORMAL = "NORMAL"
    STAGNATED = "STAGNATED"
    CONTRADICTION = "CONTRADICTION"
    MILESTONE = "MILESTONE"
    CLOSURE = "CLOSURE"
    INITIAL = "INITIAL"
    LEAN_DEADLOCK = "LEAN_DEADLOCK"
    SEMANTIC_AUDIT = "SEMANTIC_AUDIT"


# Map research state → sol-gate trigger header.
TRIGGER = {
    ResearchState.INITIAL: "INITIAL",
    ResearchState.STAGNATED: "STAGNATION",
    ResearchState.CONTRADICTION: "CONTRADICTION",
    ResearchState.MILESTONE: "CHECKPOINT",
    ResearchState.CLOSURE: "CLOSURE",
    ResearchState.LEAN_DEADLOCK: "LEAN_DEADLOCK",
    ResearchState.SEMANTIC_AUDIT: "SEMANTIC_AUDIT",
    ResearchState.NORMAL: None,
}


@dataclass(frozen=True)
class Decision:
    consult: bool
    reason: str
    trigger: Optional[str]
    role: str = "strategy"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TheoremBudget:
    theorem: str
    calls: int = 0
    takeovers: int = 0
    rounds_since_sol: int = 0
    last_trigger: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TheoremBudget":
        return cls(
            theorem=str(data.get("theorem") or "unspecified"),
            calls=int(data.get("calls") or 0),
            takeovers=int(data.get("takeovers") or 0),
            rounds_since_sol=int(data.get("rounds_since_sol") or 0),
            last_trigger=data.get("last_trigger"),
        )


def decide(
    state: ResearchState | str,
    budget: TheoremBudget,
    *,
    major_progress: bool = False,
    takeover: bool = False,
) -> Decision:
    """Return whether Sol may be called for this theorem and why."""
    if not isinstance(state, ResearchState):
        state = ResearchState(str(state).upper())
    if budget.calls >= HARD_CAP:
        return Decision(False, f"hard cap {HARD_CAP} reached", None)
    if takeover:
        if budget.takeovers >= TAKEOVER_CAP:
            return Decision(False, f"takeover cap {TAKEOVER_CAP} reached", None, "takeover")
        if budget.calls >= HARD_CAP:
            return Decision(False, f"hard cap {HARD_CAP} reached", None, "takeover")
        return Decision(True, "local lemma takeover", "TAKEOVER", "takeover")

    mandatory = state in {
        ResearchState.CLOSURE,
        ResearchState.SEMANTIC_AUDIT,
        ResearchState.LEAN_DEADLOCK,
    }
    if budget.calls >= SOFT_CAP and not mandatory:
        return Decision(False, f"soft cap {SOFT_CAP}; wait for closure/deadlock/semantic", None)

    if state == ResearchState.INITIAL:
        return Decision(True, "initial strategy review", TRIGGER[state])
    if state == ResearchState.CLOSURE:
        return Decision(True, "proof closure audit is mandatory", TRIGGER[state])
    if state == ResearchState.SEMANTIC_AUDIT:
        return Decision(True, "final semantic/novelty audit", TRIGGER[state], "semantic")
    if state == ResearchState.CONTRADICTION:
        return Decision(True, "prover/falsifier contradiction", TRIGGER[state])
    if state == ResearchState.STAGNATED:
        return Decision(True, "critical path stagnated", TRIGGER[state])
    if state == ResearchState.LEAN_DEADLOCK:
        return Decision(True, "lean obligation exhausted grok attempts", TRIGGER[state], "takeover")
    if state == ResearchState.MILESTONE and budget.rounds_since_sol >= 2 and major_progress:
        return Decision(True, "compact checkpoint after major progress", TRIGGER[state])
    return Decision(False, "grok_main_self_manage", None)


def load_budget(path: Path, theorem: str) -> TheoremBudget:
    if not path.is_file():
        return TheoremBudget(theorem=theorem)
    data = json.loads(path.read_text())
    rec = (data.get("theorems") or {}).get(theorem)
    if not rec:
        return TheoremBudget(theorem=theorem)
    rec = dict(rec)
    rec["theorem"] = theorem
    return TheoremBudget.from_dict(rec)


def store_budget(path: Path, budget: TheoremBudget) -> None:
    data: dict[str, Any] = {"theorems": {}}
    if path.is_file():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            data = {"theorems": {}}
    data.setdefault("theorems", {})[budget.theorem] = asdict(budget)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)


def _cli(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Decide whether to consult Sol")
    p.add_argument("--state", required=True)
    p.add_argument("--theorem", default="unspecified")
    p.add_argument("--budget", default="")
    p.add_argument("--major-progress", action="store_true")
    p.add_argument("--takeover", action="store_true")
    args = p.parse_args(argv)
    path = Path(args.budget) if args.budget else None
    budget = load_budget(path, args.theorem) if path else TheoremBudget(theorem=args.theorem)
    decision = decide(args.state, budget, major_progress=args.major_progress, takeover=args.takeover)
    print(json.dumps(decision.as_dict(), ensure_ascii=False))
    return 0 if decision.consult else 2


if __name__ == "__main__":
    raise SystemExit(_cli())
