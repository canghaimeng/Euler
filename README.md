# Euler

**Mathematical research that researchers can understand, inspect, and continue.**

Euler is a mathematical research agent system designed around mathematicians’ understanding, judgment, and continued exploration. It preserves statements, assumptions, proof dependencies, candidate routes, failed approaches, and verification evidence in a shared research state. Agents use that state to plan and collaborate; researchers use the same state to understand progress and decide where to investigate next.

[User guide](docs/DASHBOARD-GUIDE.md) · [中文使用指南](docs/DASHBOARD-GUIDE.zh.md) · [Architecture](ARCHITECTURE.md) · [Case examples](examples/README.md)

## What you can do

- Follow an investigation through its overview, proof reader, live research view, and dependency graph.
- Inspect what a statement assumes, what supports it, and what remains unresolved.
- Talk to individual team members and read their conversations in the selected project branch.
- Save team milestones and restore a node’s associated saved state into an independent workspace, including files and supported native conversations.
- Create projects and add members from the workbench, or let the lead register a configured teammate.
- Combine Codex, Claude Code, Gemini CLI, Grok, and Cursor Agent on Linux or macOS.

Restoration requires a recorded milestone and available native session archives; a graph node alone cannot recreate missing history. Proposals and conversations do not become accepted mathematical facts without the configured verification path.

## Try the workbench without model accounts

```bash
git clone https://github.com/canghaimeng/Euler.git
cd Euler
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[demo,observability]'
euler-dashboard-demo --port 8099
```

Open **http://127.0.0.1:8099**. This uses deterministic mock models to demonstrate the workflow; it does not reproduce a research discovery. English is the default, with a Chinese interface toggle.

## Start a research team

Install and sign in to the model CLIs you choose, then follow the [complete setup guide](docs/DASHBOARD-GUIDE.md). You may give that guide and this repository URL to a coding agent to configure your machine.

```bash
python -m pip install -e '.[gateway,observability]'
euler-team doctor
python examples/personal-research/create_workspace.py --workspace ~/euler-research/my-first-project
```

The initializer uses Euclid’s infinitude-of-primes theorem as an onboarding exercise and prints the next steps. Edit its research definition to specify your own mathematical statement and team. Creating the workspace does not start model calls.

New teams have no default conversation-turn or remaining-account-percentage limit. Configure optional usage controls for your accounts and team; explicit single-task resource policies remain separate. Provider availability and native restoration support depend on the installed CLI version; see [CLI support](docs/CLI-SUPPORT.md).

## Research examples

Nikoghosyan, Lewis–Overton, and Jankiewicz–Norin–Wise (JNW) are the three selected research cases. Their release contents are being prepared separately and are **not included in this software snapshot**. Private proof manuscripts, Lean projects, historical team conversations, and private source bundles are excluded.

The workspace initializer uses a classical theorem for onboarding. Internal deterministic mocks and elementary test fixtures test software behavior and are not research claims.

## Development

```bash
python -m pip install -e '.[dev]'
python -m pytest euler/ -q
```

Tests exercise software behavior, including evidence gates, project isolation, native-session protocol doubles, and graph views. Passing them does not establish mathematical discovery performance or improvements in human understanding. See [Contributing](CONTRIBUTING.md).

## Distribution and attribution

This repository starts with a clean software snapshot. It includes source, bundled workbench assets, agent contracts, configuration templates, installation tools, tests, guides, and onboarding configuration. It excludes development reports, research snapshots, technical-report versions, account configuration, runtime state, and private transcripts.

**By canghaimeng**. Euler is by **Qiuyu Chen (陈秋宇), Shanghai Jiao Tong University**. It builds on **Danus and Rethlas**; see [Credits](CREDITS.md). Euler is licensed under [Apache-2.0](LICENSE). Bundled frontend dependencies retain their original licenses in `euler/observability/static/vendor/`.
