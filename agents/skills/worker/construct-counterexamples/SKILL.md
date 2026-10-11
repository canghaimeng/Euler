---
name: construct-counterexamples
description: Construct candidate counterexamples to test a proposed conjecture, lemma, or intermediate claim by keeping the assumptions true while making the claimed conclusion fail. Use when a proposed conjecture/claim feels fragile or unproved, or when you are stuck in reasoning and want to see where the assumptions take effect and gain intuition.
---

# Construct Counterexamples

Actively falsify proposed conjectures or intermediate claims by finding examples that satisfy the assumptions but violate the claimed conclusion.

## Input Contract

Read:

- the specific conjecture/claim to test
- active branch assumptions
- candidate lemmas/proof steps
- current `immediate_conclusions` and `toy_examples`
- previously found counterexamples that can be reused against new claims

## Procedure

1. Identify the assumptions that must hold and the conclusion to fail.
2. Use reasoning, decomposition, and retrieval to search for standard obstructions, pathological constructions, or previously known counterexamples.
3. Decide status:
   - `refuted`: assumptions hold and the claim fails
   - `not_refuted`: no counterexample found yet
   - `inconclusive`: search space unclear or partially explored
4. If the search produces a concrete example that is informative but is not actually a counterexample, save that example as well in `toy_examples`.
5. If refuted, store the counterexample for reuse against future claims and mark impacted branches/lemmas as invalid.
6. If no counterexample is found, treat that only as evidence that the claim may be correct, not as a proof.

## Output Contract

First publish a companion `proof_attempt` for the falsification search. This is
the round-close artifact and supplies the stable ID linked by the
counterexample:

```json
{
  "attempt_id": "counterexample-<target-id>-<round-unique-suffix>",
  "approach": "construct a counterexample while preserving every hypothesis",
  "result": "success|partial|blocked",
  "reason": "refuted / not refuted / search inconclusive",
  "salvage": "repair the statement or next finite family to test",
  "inputs": ["facts and assumptions used"],
  "related_memory": ["prior examples/counterexamples/verification IDs"]
}
```

Then publish to global memory with `gm_add` (kind `counterexample`): `claim` =
what is refuted/tested, `evidence` = the candidate construction, and use the
same `attempt_id`. The gateway requires both `attempt_id` and `target`:

```json
{
  "attempt_id": "counterexample-<target-id>-<round-unique-suffix>",
  "target": "stable identifier or exact statement of the tested claim",
  "target_claim": "...",
  "candidate_counterexample": "...",
  "status": "refuted|not_refuted|inconclusive",
  "assumptions_satisfied": ["..."],
  "failed_conclusion": "...",
  "impact": "...",
  "branch_id": "optional",
  "subgoal_id": "optional"
}
```

If `status="refuted"` kills a branch, publish an `obstacle` by default. A
`dead_end` may be published only as `dead_end_status=candidate` unless you
deliberately confirm it with a valid `evidence_class` and existing
`evidence_refs`; ordinary search failure is never a confirmed dead end.

If the search produced a concrete non-refuting example, also publish an `example`
finding (`gm_add`, kind `example`):

```json
{
  "example": "...",
  "why_relevant": "constructed while testing the claim ...",
  "assumptions_satisfied": ["..."],
  "conclusion_verified": true,
  "where_assumptions_take_effect": "...",
  "observed_pattern": "...",
  "supports_branch_ids": ["optional"],
  "subgoal_id": "optional"
}
```

Do this whenever the constructed example is useful enough to test future claims or clarify the current branch, even if it did not refute the target claim.

## Tools

- `gm_add` (publish counterexample / dead_end / example findings)
- `gm_search` (recall stored counterexamples to reuse against new claims)
- Codex built-in web search and `search_arxiv_theorems` to find standard counterexample patterns

## Failure Logging

If no meaningful counterexample space is identified, append:

- `events.event_type="counterexample_space_unclear"`
