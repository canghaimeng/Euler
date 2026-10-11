# Human research over the same mathematical state

This development snapshot extends the standard Euler workbench. A researcher
can inspect a real object, preview an exact follow-up statement/scope, confirm
it, and explicitly start one bounded focused task. Original Facts and the root
contract retain their existing verification and acceptance rules.

## Whole-team conversations

The [team recovery workflow](TEAM-RECOVERY.md) restores registered members' native conversations alongside the full workspace. The **Ask the current
team** action opens member-specific, persistent conversations, including lead
agent delegation and returned replies. Use the single-action interface below for a focused task. Whole-team conversation recovery requires saved native session archives.

## What happens after Start in the single-task interface

The current executable path is a bounded background task. Selecting a native
object opens an inspector; submitting a reviewed question records a derived
contract and proposed task. Explicit Start checks the object version, worker
configuration, allowance and readiness, then compiles context and dispatches
to the deployment's configured worker on the server. The page shows the task's
state and the evidence returned through verification and reconciliation.

Each request authorizes one bounded action in the selected workspace. Use **Team interaction** for a persistent conversation with the lead, and **Research milestones** to restore a saved workspace. When provider launch is disabled, the single-task interface prepares a dispatch for later execution.

Direct dispatch requires a canonical backend object identity. Historical display nodes offer proof reading and milestone restoration; their dialog explains which actions are available. Start work by explicitly launching a task or sending a question after session recovery.

## Start from your actual research project

Keep the ordinary project catalog and full proof/dependency graph as the default.
Use [Git research milestones](RESEARCH-MILESTONES.md) to save the whole team's
files at an idle boundary, restore a chosen commit into a separate workspace,
and ask from that state. A source object and its scoped action live inside this
full project; its full graph and history remain available while the task runs.

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
The child outcomes are deterministic fixtures used to check the software protocol.
The original goals remain OPEN and parent campaigns remain PAUSED.

Open the proof reader, select **Inspect object · single task**, review identity,
scope and evidence, and choose an intent. Enter your question and the exact
statement to investigate. For an assumption change, edit and review the scope.
Preview and confirm. Use the separate Start button to launch the investigation. Results appear in the source-object dialog and the Live research
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
- A paused parent may host an explicitly started focused task while its ordinary backlog stays paused.
- Stop also disables focused starts and requests their cancellation.
- Resume checks current identity, pending settlement, recorded budget and
  dependencies. Unstarted cancelled ordinary work can receive a fresh lease;
  completed work is not blindly replayed. Interrupted running work retains its
  artifacts and receipts and needs an appropriate new task/recovery decision.
- Each focused investigation authorizes one action. Review and authorize a new request for another attempt.

## API and invariants

Read endpoints: `/api/human/capabilities`, `/object`, `/state`, `/interventions`,
`/interventions/{id}`, `/changes`, `/compare` (all accept a catalog project ID).
Write endpoints are available for configured personal projects: `/control`,
`/preview`, `/interventions`, `/interventions/{id}/start`. They require no
separate control credential; same-origin and project-policy checks still apply.
Preview is a read-only operation.

A request pins source ID/type, workspace namespace, statement/scope/object hashes, source contract
and version, operator identity and execution policy. A different current object
requires another preview. Preview also conservatively binds the research revision;
concurrent updates can require reviewing it again. Moving the workspace changes
its instance identity and requires a fresh request. New generalization/refutation scope is a derived contract with a parent/source
link. A challenge must name its refutation target and use counterexample-capable
evidence; the original problem keeps its statement and scope. Identical retries return the
original operation; conflicting idempotency content is rejected.

Source context is compiled through the existing bounded compiler. Parent
checking artifacts remain source material until their applicability to the child
is checked. Reconciliation binds results to the exact lease/context/source and
returns accepted child evidence or an unresolved outcome. Existing Facts
need fresh current-attempt verification for a re-audit. Fact admission and revocation follow their existing verification procedures.

## Evidence and evaluation

Subsystem tests and deterministic mock demos exercise the control path.

Controlled studies of human comprehension and agent efficiency are planned.
Multi-user roles, automatic scope equivalence and unrestricted multi-action
focused campaigns remain future work. Deployment quota monitoring tracks reported account usage. Set billing caps through the provider's account controls.

To resume the demo viewer on its preserved records after stopping it, use
`--reuse --output /path/printed/by/the/demo --serve`. Use a fresh output directory
for `--run-traces` so measured protocol runs have explicit starting states.
The native-round wall bound excludes scheduler/preflight overhead. Pending browser submissions retain their idempotency identity across a page reload.
