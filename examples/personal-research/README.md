# Research workspace setup example

<!-- language-nav -->
[English](README.md) · [简体中文](README.zh.md) · [Back to Euler](../../README.md)
<!-- /language-nav -->

This initializer turns an editable research definition into a ready-to-connect workspace using Euler's public setup functions. Setup runs locally; provider sessions are connected in a separate step.

```bash
python examples/personal-research/create_workspace.py --workspace ~/euler-research/euclid
```

Edit a copy of `research.json` and pass it with `--definition` to use your own problem and team. The bundled definition uses Euclid's theorem on infinitely many primes to teach the workflow.

The script writes the inputs and performs these setup steps:

```bash
EULER_AGENTS_ROOT=WORKSPACE/projects euler init ID --contract setup/contract.json --policy setup/policy.json
euler-milestones enable --project WORKSPACE/projects/ID --store WORKSPACE/history/ID
euler-team init --project WORKSPACE/projects/ID --definition setup/team.json
```

It creates the exact problem contract, an OPEN main goal, the registered team, a local milestone store, and `workbench/start-workbench.sh` with its project catalog and policies.

Choose an empty `--workspace` directory. Run `euler-team doctor` to check CLI availability and OS isolation, sign in to the selected providers, then use `euler-team prepare` to connect sessions. Research begins when you send the first question. See the [user guide](../../docs/DASHBOARD-GUIDE.md) for the full sequence.

The workspace runs on macOS or Linux. `research.five-clis.json` provides configurations for all five supported CLIs; select the members and models available to your accounts.
