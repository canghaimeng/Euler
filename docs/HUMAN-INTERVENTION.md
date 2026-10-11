# Human research over the same mathematical state

This development snapshot extends the standard Euler workbench. A researcher
can inspect a real object, preview an exact follow-up statement/scope, confirm
it, and explicitly start one bounded focused task. Original Facts and the root
contract retain their existing verification and acceptance rules.

## Whole-team conversations

The new [team recovery workflow](TEAM-RECOVERY.md) restores registered Codex and
native Grok conversations alongside the full workspace. The **Ask the current
team** action opens member-specific, persistent conversations, including lead
agent delegation and returned replies. This is distinct from the single-action
interface described below. File-only historical snapshots remain unable to
restore conversations that were never captured.

## What happens after Start in the single-task interface

The current executable path is a bounded background task. Selecting a native
object opens an inspector; submitting a reviewed question records a derived
contract and proposed task. Explicit Start checks the object version, worker
configuration, allowance and readiness, then compiles context and dispatches
to the deployment's configured worker on the server. The page shows the task's
state and the evidence returned through verification and reconciliation.

This does not open a terminal or attach a persistent chat to the team's lead
agent. Automatic lead-agent replanning of the entire team and interactive
lead-agent conversation are not connected to this button. Each request currently
authorizes one action. If provider launch is disabled, the interface prepares
dispatch only. A restored workspace retains its full files; a scoped task
created directly in the current workspace does not itself restore a milestone.

An unbound historical display node cannot directly dispatch. The error dialog
explains this, clears the loading message, and offers proof reading and the
milestone workflow. Restoring an archive and preparing a question alone does
not start a worker or resume the original lead-agent conversation.

## Start from your actual research project

Keep the ordinary project catalog and full proof/dependency graph as the default.
Use [Git research milestones](RESEARCH-MILESTONES.md) to save the whole team's
files at an idle boundary, restore a chosen commit into a separate workspace,
and ask from that state. A source object and its scoped action live inside this
full project; a one-object fixture is not a substitute for the research history.

## Separate no-account protocol demonstration

```sh
python -m pip install -e '.[dev]'
python -m euler.observability.human_demo --run-traces --serve --port 8124
```

The printed directory contains three newly initialized native projects, a
catalog, request fixtures, event traces and public outcomes. The UI project
names and proof/verification artifacts are marked MOCK. Actual HTTP APIs,
subprocess workers, scheduler/context/lease machinery, Fact gateway and
reconciliation are exercised with deterministic model/verifier fixtures.
A green child in this demo is **not evidence of a new mathematical result**.
The original goals remain OPEN and parent campaigns remain PAUSED.

Open the proof reader, select **Inspect object · single task**, review identity,
scope and evidence, and choose an intent. Enter your question and the exact
statement to investigate. For an assumption change, edit and review the scope.
Preview and confirm. The investigation is not launched until its separate Start
button is used. Results appear in the source-object dialog and the Live research
investigation list. Event traces can be exported from that dialog.

## Enable controls for an existing native project

1. Keep the normal server-owned project catalog. Never accept paths from clients.
2. Copy `config/human-control.example.json` to deployment-owned configuration.
3. Use the actual catalog ID and an existing worker whose provider/model/effort
   configuration is compatible with that policy. Set bounded allowances.
4. Set `EULER_HUMAN_CONFIG` to that file. The personal workbench requires no
   separate browser control token. `EULER_CONTROL_ACTOR` names the researcher
   in event provenance (default human:operator).
5. `allow_launch: false` permits preparation only. Enable launch explicitly after
   configuring the existing native provider and deployment quota/preflight rules.
6. Run the workbench on your own machine or private research host. Browser
   operations must come from the workbench's origin. The configured project
   scope, resource limits and verification gates apply to every request.

The default is readonly. Historical/curated nodes without exact native identity
remain readonly; names and semantic similarity are not identity mappings.
A Fact also needs an explicit verified native scope/contract binding for edits.
The worker truth gateway remains the only Fact-admission path. Request text
cannot inject a checker verdict or arbitrary actor/model configuration.

## Lifecycle semantics

- Pause stops new ordinary dispatch. Issued ordinary leases are cancelled before
  launch. Running work drains or receives a cancellation request, as selected.
- PAUSING and STOP_REQUESTED persist until relevant leases actually settle.
- Updated native loops can terminate the local subprocess group. Remote provider
  computation/billing is not guaranteed stopped; old launchers may require
  operator intervention. The worker observation and lease state are shown separately.
- A paused parent may host an explicitly started focused task. Pause does not
  implicitly grant that start, and starting the task does not resume the backlog.
- Stop also disables focused starts and requests their cancellation.
- Resume checks current identity, pending settlement, recorded budget and
  dependencies. Unstarted cancelled ordinary work can receive a fresh lease;
  completed work is not blindly replayed. Interrupted running work retains its
  artifacts and receipts and needs an appropriate new task/recovery decision.
- Focused MVP investigations authorize one action. Another attempt is a new
  reviewed request; it cannot silently obtain an unlimited continuation budget.

## API and invariants

Read endpoints: `/api/human/capabilities`, `/object`, `/state`, `/interventions`,
`/interventions/{id}`, `/changes`, `/compare` (all accept a catalog project ID).
Write endpoints are available for configured personal projects: `/control`,
`/preview`, `/interventions`, `/interventions/{id}/start`. They require no
separate control credential; same-origin and project-policy checks still apply.
Preview never writes.

A request pins source ID/type, workspace namespace, statement/scope/object hashes, source contract
and version, operator identity and execution policy. A different current object
requires another preview. Preview also conservatively binds the research revision;
concurrent updates can require reviewing it again. Moving the workspace changes
its instance identity and requires a fresh request. New generalization/refutation scope is a derived contract with a parent/source
link. A challenge must name its refutation target and use counterexample-capable
evidence; a positive proof of the old claim cannot close that challenge. It never rewrites the original problem. Identical retries return the
original operation; conflicting idempotency content is rejected.

Source context is compiled through the existing bounded compiler. Parent
checking artifacts remain source material until their applicability to the child
is checked. Reconciliation binds results to the exact lease/context/source and
returns accepted child evidence or an honest unresolved outcome. Existing Facts
need fresh current-attempt verification for a re-audit. Human opinions cannot
revoke or admit Facts through this API.

## Evidence and evaluation

Subsystem tests and deterministic mock demos exercise the control path. Private development logs and research traces are not included in this distribution.

No human comprehension or matched agent-efficiency gain has been measured.
Multi-user roles, automatic scope equivalence and unrestricted multi-action
focused campaigns remain future work. Account quotas are deployment
integrations, not a claim of a provider-side billing cap.

To resume the demo viewer on its preserved records after stopping it, use
`--reuse --output /path/printed/by/the/demo --serve`. Use a fresh output directory
for `--run-traces` so measured protocol runs have explicit starting states.
The native-round wall bound excludes scheduler/preflight overhead. Pending browser
submissions retain their idempotency identity across a page reload; control tokens
remain memory-only and must be supplied again.
