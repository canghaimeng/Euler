<div align="center">

# <img src="docs/assets/euler-logo.svg" width="36" height="36" align="absmiddle" alt="Euler logo"> Euler

Euler is a mathematical research agent system built to help researchers follow and understand the entire reasoning process. Its agent teams share a structured research state that makes derivations inspectable and is designed to strengthen model reasoning and team coordination.

[![Project Page](https://img.shields.io/badge/Project%20Page-Euler-285f58)](https://canghaimeng.site/euler/?lang=en)
[![arXiv:2610.19864](https://img.shields.io/badge/arXiv-2610.19864-B31B1B)](https://arxiv.org/abs/2610.19864)
[![Linux | macOS](https://img.shields.io/badge/platform-Linux%20%7C%20macOS-57606A)](docs/CLI-SUPPORT.md)
[![License: PolyForm Noncommercial 1.0.0](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-8250DF)](LICENSING.md)

**English** · [简体中文](README.zh-CN.md)

[Contributions](#core-contributions) · [Citation](#citation) · [Results](#mathematical-results) · [Quickstart](#quickstart) · [User guide](docs/DASHBOARD-GUIDE.md) · [Architecture](ARCHITECTURE.md) · [CLI support](docs/CLI-SUPPORT.md) · [License](#license) · [Community](#community)

</div>

## 🚀 News

- 🎉 **[OpenAI Math Improvements with Euler](https://github.com/canghaimeng/openai-math-improvement)** — From OpenAI’s mathematics release, we selected 20 problems whose published results left room for improvement. Working through Euler, Astra and GPT-6.1 Sol improved the original results on **8 problems**, with **3 surpassing the best community results checked in our study**, averaging just **86 minutes of reasoning per improved result**.
- 🎉 **[Lewis–Overton (2008)](examples/lewis-overton/README.md)** — a conjecture proposed by NYU Silver Professor Michael Overton and Cornell Professor Adrian Lewis. Both are SIAM Fellows and have reviewed our argument concerning their conjecture, confirming its correctness.
- 🎉 **[Nikoghosyan (2013)](examples/nikoghosyan/README.md)** — resolving a 13-year-old open problem in Hamiltonian graph theory and completing the single-forbidden-subgraph classification for 1-tough graphs.
- 🎉 **[Jankiewicz–Norin–Wise (2017)](examples/jnw/README.md)** — a geometric group theory problem co-proposed by James McGill Professor Daniel Wise, a Fellow of the Royal Society.

<p align="center">
  <img src="docs/assets/euler-frontier-loop.png" width="100%" alt="Euler’s frontier-driven research loop: the problem contract and research memory inform the active frontier; agents receive bounded context to prove, refute, retrieve, or compute; verification and reconciliation update the frontier and Fact Graph.">
</p>

A mathematical investigation is much more than its final proof. Statements are sharpened, routes are proposed and abandoned, lemmas pick up dependencies, and checks pass or fail. Euler organizes these records into a **shared research state**: exact statements and scope, goals and steps, proof routes, dependencies, failed attempts, and the evidence registered for each record. That one structure serves two purposes.

- **It lets researchers grasp the entire reasoning process.** Follow an investigation from the main goal down to a single step, see what each claim rests on and why a route stalled, and find where work remains open, while the team is running and even when the problem stays unsolved.
- **It gives the agent team a common working structure.** Agents plan against explicit open records, refer to exact statements instead of paraphrasing a conversation, and coordinate around the same goals and routes. The representation is designed to improve how agents reason and coordinate across a long investigation.

Researchers and agents work on the same records, making the evolving investigation available for inspection as it happens. The **frontier** (open goals, live routes, blockers, and their evidence) is the active projection of that state, showing both sides where the work stands now.

<a id="core-contributions"></a>
## 🧩 Core contributions

### 1. 🕸️ Hierarchical graphs that make reasoning easy to follow

Euler uses **hierarchical graphs** to show how premises lead to conclusions. Select any statement in the graph to trace its recorded relationships backward to **the premises it depends on** and forward to **the later conclusions it supports**, then open the corresponding proof steps and evidence. The graph highlights the route to the main conclusion and marks completed, open, and blocked steps. It updates as research progresses, giving readers an overview of the proof structure and current progress before they explore the details that interest them. [Explore the three public cases →](https://canghaimeng.site/euler/cases/?lang=en)

### 2. 🚩 Team milestones for returning, questioning, and branching

Euler lets researchers **return to any saved team milestone**. Each milestone preserves project files, the research graph, evidence, and supported native member conversations. From a graph node, researchers can select its associated milestone and restore the captured team state into an independent workspace. They can then ask members to explain the recorded approach and its mathematical rationale, examine a questionable step, or pursue a new research direction from that point. The original investigation remains available alongside the new branch. [How team recovery works →](docs/TEAM-RECOVERY.md)

### 3. 🧱 Structured representation for sustained mathematical reasoning

Euler's **structured research-state representation** gives the team a persistent foundation for planning, context retrieval, collaboration, and verification. The full research history survives across sessions, while each task receives relevant statements, dependencies, prior failures, and evidence. This allows investigations to continue across successive context windows and resumable sessions, with explicit obligations guiding the next step. The design aims to strengthen mathematical reasoning through accumulated knowledge, coordinated exploration, and independent checks—supporting problems that demand sustained work beyond a single model response. Separating unverified ideas from admitted conclusions, checking premise dependencies, and requiring independent verification also provide safeguards against hallucinations and accumulating errors in long-running single-agent reasoning, reducing the risk of carrying unsupported assertions forward as premises. [Read the architecture →](ARCHITECTURE.md)

## ✨ Highlights

| | |
| --- | --- |
| [**Six workbench views**](docs/DASHBOARD-GUIDE.md#9-the-six-views) | Overview, proof reader, live research, relation map, diagnostics, and team interaction, with an English or Chinese interface. |
| [**Live Research Graph and Fact Graph**](docs/DASHBOARD-GUIDE.md#relation-map-how-is-everything-connected) | Goals, routes, and steps form a working graph that updates while the team works; admitted results form a separate Fact Graph. Switch between the main line, the frontier, and all records. |
| [**Precise statements, scope, and evidence**](docs/DASHBOARD-GUIDE.md#11-follow-one-step-and-its-proof-path) | Each record keeps its exact wording, scope, direct premises, and registered evidence, displayed in its original language. |
| [**Evidence-gated admission**](docs/DASHBOARD-GUIDE.md#10-how-a-step-becomes-closed) | Steps close when registered evidence meets the level their contract requires, such as an independent proof check or a Lean kernel check. |
| [**Scoped intervention**](docs/HUMAN-INTERVENTION.md) | Ask the team about one step with its statement and scope attached or, when enabled, prepare a single bounded task that starts only when you confirm it. |
| [**Whole-team milestones**](docs/TEAM-RECOVERY.md) | Save project files, graphs, evidence, and supported native team conversations in private Git history, then restore a saved milestone into an independent branch. |
| [**Your CLIs, one browser**](docs/CLI-SUPPORT.md) | Combine Codex, Claude Code, Gemini CLI, Grok, and Cursor Agent on Linux or macOS. Create projects, add teammates, and read or continue each member's conversation from the workbench. |

<a id="quickstart"></a>
## ⚡ Quickstart

> [!NOTE]
> You need **Python 3.10+** and **Git**; running a team also needs **Linux** with Landlock ABI 3+ or **macOS**. Install from a source checkout, which includes the agent contracts and skills that teams use.

**Local workflow demo (no model account needed).** Built-in scripts use the integer identity `n + 0 = n`, with a preset proof and checker responses, to run Euler's task dispatch, submission, and acceptance-record workflow and display the resulting example in the workbench. An empty exploration project is also included. This makes no model calls and uses no model quota; it helps you check the installation and learn the interface. To read actual research cases, open the [three public visualizations](https://canghaimeng.site/euler/cases/?lang=en).

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

The initializer runs locally and prints the two commands that follow: `euler-team prepare` connects each member's native session, and `start-workbench.sh` serves the workbench at <http://127.0.0.1:8130>. Open **Team interaction**, select the lead, and send your first question.

The bundled definition uses Euclid's theorem on infinitely many primes as an onboarding exercise, with a Codex lead and a Grok assistant; [`research.five-clis.json`](examples/personal-research/research.five-clis.json) shows all five CLIs. New teams have no default turn or account-percentage cap; set `max_turns` for the team or a member if you want one. The [user guide](docs/DASHBOARD-GUIDE.md) covers every field, remote access over SSH, and troubleshooting, and you can hand it to a coding agent to set up your machine.

## 🔄 How a project runs

1. **Fix the problem.** A definition records the exact statement, assumptions, target kind, required evidence level, and team. The project stays bound to that contract.
2. **Ask.** Work starts from an explicit question, either yours or the lead's to a teammate. Proposed routes and steps enter the shared state as open records.
3. **Follow.** The views refresh while the team works: read the proof structure, open any step's premises and argument, and watch the frontier move.
4. **Admit.** A step closes when evidence of the required kind is registered for it.
5. **Branch.** Save a milestone, restore it into an independent workspace, recover the members' sessions, and pursue another direction while the original stays untouched.

To restore a team from a graph node, choose its associated saved milestone and recover the corresponding native session archives.

<a id="mathematical-results"></a>
## 📐 Mathematical results

**[Explore the three interactive case studies →](https://canghaimeng.site/euler/cases/?lang=en)**

Browse proof structure, research history, and dependency graphs; select a node to follow its premises, derivation, and sources. The public site displays historical research snapshots with team messaging, restoration, and model calls disabled.

### OpenAI Math: public models, stronger results with Euler

We selected 20 problems from OpenAI’s mathematics release whose published results left room for improvement. Using Astra and GPT-6.1 Sol within Euler, our teams improved the original results on **8 problems**, with **3 surpassing the best community bounds checked in our study**, averaging **86 minutes of reasoning per improved result**. These advances span Fourier transforms, geometric coverings, spectral gaps, graph mixing, and Boolean function complexity.

**[Explore the results →](https://github.com/canghaimeng/openai-math-improvement)** · [Papers and reasoning times](https://github.com/canghaimeng/openai-math-improvement#mathematical-results) · [Lean formalization](https://github.com/canghaimeng/openai-math-improvement)

### Lewis–Overton: initialization changes the asymptotic rate

BFGS is a widely used optimization method, yet its behavior on nonsmooth functions remains subtle. A 2008 conjecture of Adrian Lewis and Michael Overton predicted a rate determined by the problem parameter alone. Two executions on the same objective are shown to have different sharp trial-normalized rates throughout an open parameter interval, establishing that initialization can matter even in this simple one-dimensional model.

**[Explore the case →](examples/lewis-overton/README.md)** · [Visualization](https://canghaimeng.site/euler/cases/?project=lewis-overton&lang=en) · [Paper](examples/lewis-overton/paper.pdf) · [Lean formalization](https://github.com/canghaimeng/lewis-overton-bfgs-rate)

### Nikoghosyan: completing a Hamiltonicity classification

A Hamilton cycle visits every vertex exactly once. The relationship between toughness and Hamiltonicity has been studied since the 1970s. The Nikoghosyan result supplies the final missing case in the single-forbidden-subgraph classification: combined with Li, Broersma and Zhang's earlier theorem, it determines exactly which forbidden induced graphs make 1-toughness sufficient for Hamiltonicity. [Classification background](https://ris.utwente.nl/ws/files/253281133/Li2016forbidden.pdf).

**[Explore the case →](examples/nikoghosyan/README.md)** · [Visualization](https://canghaimeng.site/euler/cases/?project=nikoghosyan&lang=en) · [Paper](examples/nikoghosyan/paper.pdf) · [Lean formalization](https://github.com/canghaimeng/Nikoghosyan-conjecture)

### Jankiewicz–Norin–Wise: the limits of a bridge from graphs to geometry

Legal systems provide combinatorial certificates used in the study of right-angled Coxeter groups. Jankiewicz, Norin, and Wise asked whether a graph could satisfy strong connectivity and curvature conditions while excluding these certificates throughout its four-connected subgraphs. A 33-vertex construction answers that four-connected question, clarifying the limits of this certificate method. The result covers four-connected ordinary subgraphs; the three-connected variant is a separate question.

**[Explore the case →](examples/jnw/README.md)** · [Visualization](https://canghaimeng.site/euler/cases/?project=jnw&lang=en) · [Paper](examples/jnw/paper.pdf) · [Lean formalization](https://github.com/canghaimeng/JNWLegalSystemObstruction)

## 📚 Documentation

| Guide | Covers |
| --- | --- |
| [User guide](docs/DASHBOARD-GUIDE.md) | Setup, a first project, the six views, milestones, troubleshooting |
| [CLI support](docs/CLI-SUPPORT.md) | Providers, OS isolation, native session recovery |
| [Team recovery](docs/TEAM-RECOVERY.md) · [Milestones](docs/RESEARCH-MILESTONES.md) | What a checkpoint saves and how restoration works |
| [Human intervention](docs/HUMAN-INTERVENTION.md) | Optional bounded single tasks |
| [Architecture](ARCHITECTURE.md) · [Security and trust](docs/security-and-trust.md) | Layers, invariants, interfaces, and the trust model |

The [case index](examples/README.md) identifies the included paper versions and their separate Lean repositories. The case pages document investigations conducted across earlier research workflow versions.

## 🛠️ Development

```bash
python -m pip install -e '.[dev]'
python -m pytest euler/ -q
```

The suite runs offline, with local protocol doubles standing in for the provider CLIs. CI runs it on Python 3.10 and 3.13 and checks the packaged workbench in a real browser. Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

## 👥 Authors

| Author | Affiliation |
| --- | --- |
| [Qiuyu Chen](https://github.com/canghaimeng) | Shanghai Jiao Tong University |
| [Qi Wang](https://qiwang067.github.io/) | Institute of Automation, Chinese Academy of Sciences |
| [Liang Xu](https://liangxuy.github.io/) | Zhongguancun Academy, Beijing, China |
| [Xin Jin](https://jinx-ustc.github.io/jinxin.github.io/) | Eastern Institute of Technology, Zhejiang, China |
| [Baao Xie](https://openreview.net/profile?id=~Baao_Xie3) | Ningbo Institute of Digital Twin, Eastern Institute of Technology, Zhejiang, China |
| [Wenjun Zeng](https://www.eitech.edu.cn/en/2025/0721/c606a2753/page.htm) | Eastern Institute of Technology, Zhejiang, China |

<a id="citation"></a>
## 📚 Citation

Read the [Euler paper on arXiv](https://arxiv.org/abs/2610.19864). To cite Euler, use the paper citation below or [CITATION.cff](CITATION.cff).

```bibtex
@article{chen2026euler,
  title={Euler: Towards Human-Centered Multi-Agent Mathematical Discovery},
  author={Chen, Qiuyu and Wang, Qi and Xu, Liang and Jin, Xin and Xie, Baao and Zeng, Wenjun},
  journal={arXiv preprint arXiv:2610.19864},
  year={2026}
}
```

<a id="license"></a>
## 📄 License

Euler's original work and modifications use the **[PolyForm Noncommercial License 1.0.0](LICENSE)**, which permits noncommercial use, modification, and redistribution under its terms. See [Licensing](LICENSING.md) for the licenses of included code, libraries, and papers.

## 🙏 Acknowledgments

Euler’s graph design draws on [Danus](https://github.com/frenzymath/Danus), and we consulted the [Rethlas](https://github.com/frenzymath/Rethlas) implementation during development. We thank the authors and contributors of both projects for sharing their work.

We thank Professor Michael Overton and Professor Adrian Lewis for the time and care they devoted to reviewing our argument concerning the Lewis–Overton conjecture.

<a id="contact"></a>
## ✉️ Collaboration & Contact

We welcome collaborations in mathematical research, Euler development, and academic exchange.

**Email:** [canghaimeng@sjtu.edu.cn](mailto:canghaimeng@sjtu.edu.cn)

<a id="community"></a>
## 💬 Community

Join the discussion to ask questions, share research experiences, and suggest improvements to Euler.

<table>
  <tr>
    <th align="center">Discord</th>
    <th align="center">WeChat</th>
  </tr>
  <tr>
    <td align="center" valign="middle"><a href="https://discord.gg/gPmE4kzqC"><img src="docs/assets/discord-community.png" width="280" alt="QR code to join the Euler Discord community"></a></td>
    <td align="center" valign="middle"><a href="docs/assets/wechat-community.png"><img src="docs/assets/wechat-community.png" width="280" alt="QR code to join the Euler WeChat group"></a></td>
  </tr>
  <tr>
    <td align="center"><a href="https://discord.gg/gPmE4kzqC">Join Discord</a> · click or scan</td>
    <td align="center">Scan with WeChat · valid until October 27</td>
  </tr>
</table>
