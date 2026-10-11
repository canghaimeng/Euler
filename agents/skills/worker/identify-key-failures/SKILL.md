---
name: identify-key-failures
description: Synthesize the common stuck points across failed decomposition plans. Use when the current batch of decomposition plans has failed — whether they failed already at direct proving or only after further attempts — and whenever the orchestrator prompt is FAILURE SYNTHESIS MODE.
---

# Identify Key Failures

Use this skill to turn many failed attempts into reusable guidance for the next planning round. The orchestrator may inject **FAILURE SYNTHESIS MODE** with exact record IDs; in that case those IDs are mandatory inputs. This skill does **not** decide that a mathematical route is dead.

## Input Contract

When the kickoff prompt contains `FAILURE SYNTHESIS MODE`, recall **exactly** the
listed `source_record_ids` / `attempt_ids` / `verification_ids` / `obstacle_ids`
via **`gm_search` only**. Do not invent IDs. Do not read `global_memory/*.jsonl`.
Do not `from euler.gateway.server import …`. Missing MCP is fail-closed. Do not
read other workers' private `local_memory/`.

Otherwise read:

- the failed decomposition plans
- direct-proving stuck points
- existing `failed_paths`
- relevant `counterexamples` and `toy_examples`

## Procedure

1. Gather the reports from the supplied IDs (orchestrated) or from all failed plans (voluntary). If only direct proving has run so far, work directly from the direct-proving failures.
2. List the key stuck points for each plan/attempt.
3. Identify common points across those failures:
   - recurring obstructions or counterexamples
   - decomposition patterns that keep breaking
   - search gaps or missing background facts
4. Summarize what the failures suggest for the next generation of decomposition plans.
5. Publish **one** matching-context summary via `gm_add` (below). Prefer `obstacle`. A `dead_end` is allowed only as `dead_end_status=candidate`. **Never auto-confirm.** A confirmed `dead_end` requires stage-3 `evidence_class` + nonempty `evidence_refs` that you supply as a mathematical judgment (`failure_synthesis` is an allowed class); the scheduler does not supply that conclusion.
6. Do not `fact_submit` from this skill. Do not write fact-graph files.
7. After recording the failure synthesis, return control to `$propose-subgoal-decomposition-plans` (unless the prompt is a dedicated synthesis round — then persist the summary and a stage-3 `proof_attempt` and stop).

## Output Contract

Publish the failure synthesis to global memory with `gm_add` (kind `obstacle` **or** `dead_end` candidate). `claim` = the common stuck points, `evidence` = the per-plan failures. Carry these metadata fields (and `cluster_id` when the prompt provided one):

```json
{
  "record_type": "key_failures_summary",
  "cluster_id": "<from FAILURE SYNTHESIS MODE, if present>",
  "attempt_id": "<this round's proof_attempt id (the gm_add link, not a cluster member)>",
  "source_record_ids": ["<exactly the prompt source_record_ids / request member_ids>"],
  "failed_plan_ids": ["<exactly the prompt attempt_ids when that list is nonempty>"],
  "failed_attempt_ids": ["<same exact attempt_ids; either this or failed_plan_ids>"],
  "plan_failures": [
    {
      "plan_id": "...",
      "stuck_points": ["..."]
    }
  ],
  "common_failures": ["..."],
  "implications_for_next_plans": ["..."],
  "recommended_actions": ["..."]
}
```

`source_record_ids` must equal the request `member_ids` **exactly** (same IDs, same
order, no extras/missing/fakes). When the request lists `attempt_ids`,
`failed_plan_ids` or `failed_attempt_ids` must equal those attempt IDs **exactly**.
Verification-only and obstacle-only clusters still copy `source_record_ids`
exactly; do not substitute this round's `attempt_id`.

For a `dead_end`, set `dead_end_status=candidate`. Do not set `confirmed`.

Also note in your local memory (`events`) that a new planning round is needed.

## Tools

- `gm_add` (publish the obstacle or candidate dead_end synthesis)
- `gm_search` (gather the failed plans and stuck points across the swarm)
- not `global_memory/*.jsonl` and not a Python gateway import; missing MCP is fail-closed

## Failure Logging

If the reports are too weak to identify meaningful common failures, note in local memory (`events`) `event_type="key_failures_inconclusive"` and state what information is still missing. In FAILURE SYNTHESIS MODE you must still persist a best-effort `key_failures_summary` with the given IDs (the round cannot close without it).
