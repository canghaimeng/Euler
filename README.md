<div align="center">

# Euler

Euler is a mathematical research agent system built to help researchers follow and understand the entire reasoning process. Its agent teams share a structured research state that makes derivations inspectable and is designed to strengthen model reasoning and team coordination.

[![tests](https://github.com/canghaimeng/Euler/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/canghaimeng/Euler/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![Linux | macOS](https://img.shields.io/badge/platform-Linux%20%7C%20macOS-57606A)](docs/CLI-SUPPORT.md)
[![License: PolyForm Noncommercial 1.0.0](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-8250DF)](LICENSING.md)

[Results](#mathematical-results) · [Quickstart](#quickstart) · [User guide](docs/DASHBOARD-GUIDE.md) · [中文指南](docs/DASHBOARD-GUIDE.zh.md) · [Architecture](ARCHITECTURE.md) · [CLI support](docs/CLI-SUPPORT.md) · [License](#license)

</div>

## 🚀 News

- 🎉 **[Nikoghosyan (2013–2026)](examples/nikoghosyan/)** — resolving a 13-year-old open problem in Hamiltonian graph theory and completing the single-forbidden-subgraph classification for 1-tough graphs.
- ✨ **[Lewis–Overton](examples/lewis-overton/)** — a conjecture by NYU Silver Professor Overton and Cornell Professor Lewis, both SIAM Fellows, refuted.
- 🎉 **[JNW](examples/jnw/)** — the four-connected legal-system problem, resolved.

<p align="center">
  <img src="docs/assets/euler-frontier-loop.png" width="100%" alt="Euler’s frontier-driven research loop: the problem contract and research memory inform the active frontier; agents receive bounded context to prove, refute, retrieve, or compute; verification and reconciliation update the frontier and Fact Graph.">
</p>

A mathematical investigation is much more than its final proof. Statements are sharpened, routes are proposed and abandoned, lemmas pick up dependencies, and checks pass or fail. Euler organizes these records into a **shared research state**: exact statements and scope, goals and steps, proof routes, dependencies, failed attempts, and the evidence registered for each record. That one structure serves two purposes.

- **It lets researchers grasp the entire reasoning process.** Follow an investigation from the main goal down to a single step, see what each claim rests on and why a route stalled, and find where work remains open, while the team is running and even when the problem stays unsolved.
- **It gives the agent team a common working structure.** Agents plan against explicit open records, refer to exact statements instead of paraphrasing a conversation, and coordinate around the same goals and routes. The representation is designed to improve how agents reason and coordinate across a long investigation.

Researchers and agents work on the same records, making the evolving investigation available for inspection as it happens. The **frontier** (open goals, live routes, blockers, and their evidence) is the active projection of that state, showing both sides where the work stands now.

## Highlights

| | |
| --- | --- |
| [**Six workbench views**](docs/DASHBOARD-GUIDE.md#9-the-six-views) | Overview, proof reader, live research, relation map, diagnostics, and team interaction, with an English or Chinese interface. |
| [**Live Research Graph and Fact Graph**](docs/DASHBOARD-GUIDE.md#relation-map-how-is-everything-connected) | Goals, routes, and steps form a working graph that updates while the team works; admitted results form a separate Fact Graph. Switch between the main line, the frontier, and all records. |
| [**Precise statements, scope, and evidence**](docs/DASHBOARD-GUIDE.md#11-follow-one-step-and-its-proof-path) | Each record keeps its exact wording, scope, direct premises, and registered evidence, shown as recorded and never translated. |
| [**Evidence-gated admission**](docs/DASHBOARD-GUIDE.md#10-how-a-step-becomes-closed) | Drafts, peer reviews, and proposals never close a step. A step closes only with evidence at the level its contract requires, such as an independent proof check or a Lean kernel check. |
| [**Scoped intervention**](docs/HUMAN-INTERVENTION.md) | Ask the team about one step with its statement and scope attached or, when enabled, prepare a single bounded task that starts only when you confirm it. |
| [**Whole-team milestones**](docs/TEAM-RECOVERY.md) | Save project files, graphs, evidence, and supported native team conversations in private Git history, then restore a saved milestone into an independent branch. |
| [**Your CLIs, one browser**](docs/CLI-SUPPORT.md) | Combine Codex, Claude Code, Gemini CLI, Grok, and Cursor Agent on Linux or macOS. Create projects, add teammates, and read or continue each member's conversation from the workbench. |

## Quickstart

> [!NOTE]
> You need **Python 3.10+** and **Git**; running a team also needs **Linux** with Landlock ABI 3+ or **macOS**. Install from a source checkout, which includes the agent contracts and skills that teams use.

**Tour the workbench without model accounts.** The demo serves a small project produced offline by deterministic mock models.

```bash
git clone https://github.com/canghaimeng/Euler.git && cd Euler
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[demo,observability]'
euler-dashboard-demo --port 8099        # then open http://127.0.0.1:8099
```

**Start your own research team.** Install and sign in to the CLIs you want on the team, then:

```bash
python -m pip install -e '.[gateway,observability]'
euler-team doctor                       # checks CLIs and OS isolation; no model calls

mkdir -p ~/euler-research
cp examples/personal-research/research.json ~/euler-research/first.json
# edit first.json: the statement, its scope, the required evidence, and the team
python examples/personal-research/create_workspace.py \
  --definition ~/euler-research/first.json --workspace ~/euler-research/first
```

The initializer contacts no provider and prints the two commands that follow: `euler-team prepare` connects each member's native session, and `start-workbench.sh` serves the workbench at <http://127.0.0.1:8130>. Open **Team interaction**, select the lead, and send your first question.

The bundled definition uses Euclid's theorem on infinitely many primes as an onboarding exercise, with a Codex lead and a Grok assistant; [`research.five-clis.json`](examples/personal-research/research.five-clis.json) shows all five CLIs. New teams have no default turn or account-percentage cap; set `max_turns` for the team or a member if you want one. The [user guide](docs/DASHBOARD-GUIDE.md) covers every field, remote access over SSH, and troubleshooting, and you can hand it to a coding agent to set up your machine.

## How a project runs

1. **Fix the problem.** A definition records the exact statement, assumptions, target kind, required evidence level, and team. The project stays bound to that contract.
2. **Ask.** Work starts from an explicit question, either yours or the lead's to a teammate. Proposed routes and steps enter the shared state as open records.
3. **Follow.** The views refresh while the team works: read the proof structure, open any step's premises and argument, and watch the frontier move.
4. **Admit.** A step closes when evidence of the required kind is registered for it, and not before.
5. **Branch.** Save a milestone, restore it into an independent workspace, recover the members' sessions, and pursue another direction while the original stays untouched.

Restoration requires a saved milestone and the corresponding native session archives; a graph node alone cannot reconstruct missing team history.

## Mathematical results

### Nikoghosyan: completing a Hamiltonicity classification

A Hamilton cycle visits every vertex exactly once. The relationship between toughness and Hamiltonicity has been studied since the 1970s. The Nikoghosyan result supplies the final missing case in the single-forbidden-subgraph classification: combined with Li, Broersma and Zhang's earlier theorem, it determines exactly which forbidden induced graphs make 1-toughness sufficient for Hamiltonicity. [Classification background](https://ris.utwente.nl/ws/files/253281133/Li2016forbidden.pdf).

**[Explore the case →](examples/nikoghosyan/)** · [Paper](examples/nikoghosyan/paper.pdf) · [Lean formalization](https://github.com/canghaimeng/Nikoghosyan-conjecture)

### Lewis–Overton: initialization changes the asymptotic rate

BFGS is a widely used optimization method, yet its behavior on nonsmooth functions remains subtle. A 2008 conjecture of Adrian Lewis and Michael Overton predicted a rate determined by the problem parameter alone. Two executions on the same objective are shown to have different sharp trial-normalized rates throughout an open parameter interval, establishing that initialization can matter even in this simple one-dimensional model.

**[Explore the case →](examples/lewis-overton/)** · [Paper](examples/lewis-overton/paper.pdf) · [Lean formalization](https://github.com/canghaimeng/lewis-overton-bfgs-rate)

### JNW: the limits of a bridge from graphs to geometry

Legal systems provide combinatorial certificates used in the study of right-angled Coxeter groups. Jankiewicz, Norin, and Wise asked whether a graph could satisfy strong connectivity and curvature conditions while excluding these certificates throughout its four-connected subgraphs. A 33-vertex construction answers that four-connected question, clarifying the limits of this certificate method. The stronger three-connected-subgraph variant remains outside the result.

**[Explore the case →](examples/jnw/)** · [Paper](examples/jnw/paper.pdf) · [Lean formalization](https://github.com/canghaimeng/JNWLegalSystemObstruction)

## Documentation

| Guide | Covers |
| --- | --- |
| [User guide](docs/DASHBOARD-GUIDE.md) · [中文](docs/DASHBOARD-GUIDE.zh.md) | Setup, a first project, the six views, milestones, troubleshooting |
| [CLI support](docs/CLI-SUPPORT.md) | Providers, OS isolation, native session recovery |
| [Team recovery](docs/TEAM-RECOVERY.md) · [Milestones](docs/RESEARCH-MILESTONES.md) | What a checkpoint saves and how restoration works |
| [Human intervention](docs/HUMAN-INTERVENTION.md) | Optional bounded single tasks |
| [Architecture](ARCHITECTURE.md) · [Security and trust](docs/security-and-trust.md) | Layers, invariants, interfaces, and the trust model |

The [case index](examples/README.md) identifies the included paper versions and their separate Lean repositories. These historical investigations span earlier research workflows; they are not controlled end-to-end benchmarks of this software release.

## Development

```bash
python -m pip install -e '.[dev]'
python -m pytest euler/ -q
```

The suite runs offline, with local protocol doubles standing in for the provider CLIs. CI runs it on Python 3.10 and 3.13 and checks the packaged workbench in a real browser. Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

## License

Euler is **source-available for noncommercial use**. Euler-original work and modifications in this distribution are licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE), which is not an OSI-approved open-source license. Third-party portions retain their original licenses; see [LICENSING.md](LICENSING.md) and [NOTICE](NOTICE) for the applicable terms and attributions.

## Author and citation

**By canghaimeng** (Qiuyu Chen, 陈秋宇), Shanghai Jiao Tong University. To cite the software, use [CITATION.cff](CITATION.cff).

## Acknowledgments

Euler’s graph design draws on [Danus](https://github.com/frenzymath/Danus), and we consulted the [Rethlas](https://github.com/frenzymath/Rethlas) implementation during development. We thank the authors and contributors of both projects for sharing their work.
