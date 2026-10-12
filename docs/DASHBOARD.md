# Visual research workbench

<!-- language-nav -->
[English](DASHBOARD.md) · [简体中文](DASHBOARD.zh.md) · [Back to Euler](../README.md)
<!-- /language-nav -->

The personal workbench presents projects, teams, proof dependencies and ongoing exploration.
Opening a page only reads records. Configured projects also support explicit team
questions, local checkpoints, independent restoration and focused tasks, without
a separate browser control token. Mathematical verification remains separate.
Start with the [user guide](DASHBOARD-GUIDE.md) for a complete first-project workflow.

## Start locally

```bash
python -m pip install -e '.[observability]'
euler-dashboard --project /absolute/path/to/runtime/projects/my-project --port 8099
```

Open http://127.0.0.1:8099. For a no-account tour install `.[demo,observability]` and
run `euler-dashboard-demo`. The demo exercises the integration workflow with deterministic model fixtures.

The interface defaults to English; the language switch remembers your preference.
Statements, proofs, task records and formulas retain their original language.
Bundled JavaScript, styles, math typesetting and fonts work without a CDN.

## Choose projects and group teams

Pass `--config /absolute/path/to/dashboard.json`. A minimal configuration is:

```json
{
  "name": "My research workspace",
  "project_catalog": "projects.json",
  "teams": [
    {"id": "research", "name": "Research", "workers": ["codex", "grok"]},
    {"id": "review", "name": "Review", "workers": ["reviewer"]}
  ]
}
```

`projects.json`, relative to that config, can select existing projects:

```json
{
  "projects": [
    {"id": "problem-a", "name": "Problem A", "project_dir": "../runtime/projects/a", "config": "a.json"},
    {"id": "problem-b", "name": "Problem B", "project_dir": "../runtime/projects/b"}
  ],
  "discover_project_roots": ["../runtime/projects"]
}
```

Catalog paths are relative to the catalog file. Discovery is shallow and confined
to explicitly configured roots; browser requests choose IDs, never filesystem paths.
Each team's `workers` names existing project workers. Create agents through the team setup or **Add member** action.
Use `config/dashboard.example.json` as a starting point.

Optional per-project settings include `monitor_file` for public operational metadata,
`acceptance_file` for a structured formal acceptance report, and `reading_order` for
curated graph node IDs. Without these, the UI falls back to the project's own records.
Do not point public viewers at confidential research data.

## Read the proof and frontier

- **Overview:** the target, proof structure and remaining obligations.
- **Proof reader:** statements, original proofs, source references and recorded dependencies.
- **Live exploration:** attempts, failures, counterexamples and new conclusions before a paper exists.
- **Relation map:** layered dependencies, focused ancestry/descendants and frontier highlighting.
- **Diagnostics:** team status, receipt metadata, data freshness and configured budget signals.
- **Team interaction:** select the current project branch and one of its members;
  read that member’s saved history and send new messages to its native conversation.

The graph displays recorded derivations and marks gaps in their evidence. Read goal closure and outstanding obligations to assess progress.
An old OPEN task record and a later final formal acceptance are shown as different
records, with their sources. Both are displayed in their original form.

Optional editorial reading guides are loaded from a server-owned JSON file set through
`EULER_DASHBOARD_PROOF_GUIDES`. The format is tested in
`euler/observability/tests/test_proof_guide.py`: project ID, Fact ID, exact proof SHA-256,
bilingual step descriptions and exact source offsets/quotes. Guides are withdrawn when
source hashes or anchors change. The release includes no private campaign guides.

## Remote viewing

The default bind address is loopback. From another machine, use an SSH tunnel:

```bash
ssh -N -L 8099:127.0.0.1:8099 user@research-host
```

Then open http://127.0.0.1:8099 on that machine. The browser and the team are
using the same research host. Provider login, project paths and execution limits
are configured on that host. The personal workflow does not require another
web control password. See [team recovery](TEAM-RECOVERY.md) for the configured
team and milestone controls.

## Validate and maintain

```bash
python -m pytest euler/observability/ -q
python scripts/build-reader-guide.py
```

See [Contributing](../CONTRIBUTING.md) for validation commands.
The guide/story sources are `docs/DASHBOARD-GUIDE{,.zh}.md` and
the build script copies the user guides into packaged static assets.

[Reader's guide](DASHBOARD-GUIDE.md)
