# Euler control plane

This package turns research policy into an enforced runtime path. A managed
worker action moves through the following sequence:

1. register a `ProblemContract`, target `Obligation`, and `BudgetPolicy`;
2. persist a typed `CandidateAction`;
3. let `TransparentScheduler` select work and reserve its resources;
4. compile a bounded, content-addressed context packet;
5. issue a lease and atomically bind it to the worker task and dispatch sidecar;
6. reconcile the immutable start/launch/result/end receipt chain;
7. attach accepted Fact evidence to the exact target obligation;
8. run an independent, force-fresh final audit before recording `SOLVED`.

## Main modules

- `project.py`, `event_store.py`, `live_records.py`: durable project state and
  append-only events.
- `contracts.py`, `obligations.py`, `views.py`: research contracts and replayed
  obligation state.
- `budget.py`, `runtime.py`: multidimensional reservations and worker leases.
- `scheduler.py`, `context.py`, `controller.py`: selection, bounded context, and
  managed dispatch.
- `dispatch.py`, `hooks.py`, `reconcile.py`: fail-closed execution integration
  and idempotent round settlement.
- `final_audit.py`, `acceptance.py`, `evaluation.py`: independent final checking
  and outcome recording.
- `mcp.py`, `cli.py`: main-only control surfaces.

## Invariants

- The control plane cannot write mathematical Facts. Fact admission remains
  exclusively behind the verifier-gated gateway.
- A worker cannot start managed work without a selected plan, valid lease, and
  matching task/sidecar hashes.
- Infrastructure failure never becomes a mathematical refutation.
- Unknown usage remains unknown; it is never silently converted to zero.
- Replaying an event, receipt, reconciliation, or final audit is idempotent.
- Final acceptance requires exact contract, scope, claim, evidence, and fresh
  verifier alignment.

The public orchestration path is exposed by `euler init`, `euler propose`,
`euler schedule`, `euler reconcile`, and `euler checkpoint`.
