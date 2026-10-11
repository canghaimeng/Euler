# Git research milestones and restored questions

The normal workbench opens real projects with their full graphs. The small
`euler-human-demo` projects test the intervention protocol; they are separate
fixtures and must not replace the real project catalog or its default project.

## Save a complete team workspace

New `euler new` workspaces enable local Git history automatically. A first
snapshot records team setup. Native worker exit checks for changed mathematical
state and saves it when the whole team is idle. To capture during a long run,
pause/drain the team first. A busy workspace is deferred, never advertised as a
consistent snapshot. Set `EULER_MILESTONES=0` only to disable automatic setup.

For an existing project, enable snapshots and register external research files:

```sh
euler-milestones enable --project /research/project \
  --store /research/history/project --attachment proofs=/research/proofs \
  --dashboard /research/dashboard.json
euler-milestones watch --project /research/project --interval 30
euler-milestones capture --project /research/project --label 'Factor lemma checked'
euler-milestones list --project /research/project
```

The watch command checks for changes in the public mathematical graphs. A manual
capture also records file-only milestones. History is a private local Git
repository outside the original project, without a remote. It does not stage or
commit the project's source repository. Snapshot roots are operator configured.

Every snapshot retains all regular project files, including untracked/ignored
proof files, all workers' assignments and opaque local notes, shared Facts,
research events, receipts and artifacts. SQLite uses its backup API; the file
inventory must stay unchanged during copying. Empty directories and file modes
are recorded. Linked dependencies within registered roots are restored as
relative links. A manifest records hashes, omitted entries and unresolved
external links. Provider credentials, provider caches, process handles, Git
metadata and reproducible dependency caches are excluded. Missing external
dependencies remain explicit; remote model sessions are not a file snapshot.

## Native team state

For registered persistent teams, each commit now also contains opaque native
session archives and the roster, goals and mailbox. See
[Team recovery](TEAM-RECOVERY.md). File-only historical imports remain explicitly
file-only; no old conversations are manufactured.

## Restore a time point and ask

In the standard workbench, choose **Research milestones**, select a timestamp
and commit, optionally enter a question, and restore. The result is a separate
workspace in the project selector. The original campaign is untouched; the
saved graph and proof reader remain available. A historical projection pins
its original source excerpts too, so later source edits cannot change the old
view. Historical team status is a view of that recorded state, not evidence of
newly running workers.

```sh
euler-milestones restore --project /research/project --commit FULL_COMMIT_HASH \
  --question 'Which assumptions are needed for this lemma?'
```

Restoration verifies every file hash, rejects active native leases, places the
restored native campaign in PAUSED, and writes `RESTORE_ORIGIN.json` and
`FOLLOWUP.md`. This prepares research; it does not consume model allowance.
On a native object, inspect/preview/start then uses the existing scheduler and
verification gates in the restored workspace. The new task context and outcome
retain the source Git commit. Completed old tasks are not replayed automatically.
Providers require current credentials and fresh execution authorization.

Historical presentation nodes can anchor a saved question to the exact snapshot,
but do not acquire native control identities. Legacy team continuation still
requires a coordinator handoff; it is not silently simulated by a new mock Fact.

## Deployment and evidence

`EULER_MILESTONE_CONFIG` is a server-owned JSON object whose `projects` map holds
catalog IDs, `capture: true` and `settings` (`store`, named `roots`, `dashboard`).
`EULER_MILESTONE_REGISTRY` names a separate file for restored project entries.
The personal workbench uses same-origin checks and an explicit project allowlist;
no separate browser control token is required. No endpoint serves worker-private files or Git blobs.
Native execution policy transfers only if the source already has one.

Historical imports can preserve records that are available in their supplied source bundle. They cannot reconstruct unavailable native conversations or missing linked files. This software distribution does not include private historical bundles.

Validation: `python -m pytest euler/workspace/tests -q` exercises Git recovery, multiple workers, ignored files, SQLite WAL, idempotency, active-work rejection, capture races, private-file boundaries, and retained graphs.
