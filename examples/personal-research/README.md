# Personal research workspace (example)

> **Example, not core.** This folder turns one editable research definition into a
> complete starting workspace for a single researcher. It only calls Euler's public
> setup functions; it never contacts a model provider and never copies credentials.

```bash
python examples/personal-research/create_workspace.py --workspace ~/euler-research/euclid
```

Edit a copy of `research.json` first and pass it with `--definition`. The bundled
definition is Euclid's theorem on infinitely many primes: a classical result used to
learn the workflow, **not** a research result.

The script writes the same inputs that the command-line tools use, then runs the
equivalent of these steps:

```bash
EULER_AGENTS_ROOT=WORKSPACE/projects euler init ID --contract setup/contract.json --policy setup/policy.json
euler-milestones enable --project WORKSPACE/projects/ID --store WORKSPACE/history/ID
euler-team init --project WORKSPACE/projects/ID --definition setup/team.json
```

and generates `workbench/start-workbench.sh` with the local workbench configuration
(team policy, milestone policy, restored-workspace registry and project catalog).

What this establishes: a native Euler project with the exact statement and scope you
wrote, one OPEN target obligation, a registered team whose sessions are not connected
yet, milestone history outside the project, and a workbench that can save, restore and
continue that team. What it does not establish: provider sign-in, OS isolation support,
model availability, or any mathematics. `euler-team prepare` and your first question
are the steps that contact the providers. See `docs/DASHBOARD-GUIDE.md` for the whole
path.

It refuses to write into an existing non-empty directory. To start over, choose a new
`--workspace`.

Run locally on macOS or Linux. `research.five-clis.json` illustrates all five providers; choose the members and model IDs available to your accounts. `euler-team doctor` checks the host without inference.
