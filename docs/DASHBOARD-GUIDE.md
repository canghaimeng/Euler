# Euler user guide: start research, understand progress, and continue exploring

<!-- language-nav -->
[English](DASHBOARD-GUIDE.md) · [简体中文](DASHBOARD-GUIDE.zh.md) · [Back to Euler](../README.md)
<!-- /language-nav -->

Euler is a mathematical research agent system designed to help researchers follow and understand the entire mathematical reasoning process. It preserves statements, assumptions, proof dependencies, candidate routes, failed approaches, and verification evidence in a shared, structured research state. Researchers can use that state to see what has been established, which premises remain unresolved, why a route is blocked, and how each step is supported. The same representation supplies the agent team with explicit dependencies, relevant context, and evidence boundaries, and is designed to strengthen its reasoning and coordination. Through the visual workbench, researchers can trace the agents’ research paths and key changes of direction, ask questions about specific mathematical objects, and restore a node’s associated saved team state into an independent workspace for further investigation. Even before the main problem is solved, partial results, failed attempts, and open questions remain available for understanding, inspection, and continued research.

This guide explains how to configure and start a team, understand research progress in the browser, and continue through conversation and milestone recovery. Its example uses Codex as the lead for planning and coordination, and Grok as an assistant for checking and exploration; this is one team configuration.

The instructions use one concrete deployment: a Mac or Linux machine runs Euler and the agents, with the workbench in a browser on that machine or another computer. Euler’s research cases include the Nikoghosyan conjecture, the Lewis–Overton conjecture, and the Jankiewicz–Norin–Wise (JNW) problem; the rest of this guide uses Euclid’s theorem on infinitely many primes as an introductory example for learning the workflow.

## 1. What you need before your first project

- **A Mac or Linux computer.** A Mac can configure and run the entire team locally. Linux uses Landlock ABI 3+; macOS uses `sandbox-exec` (Seatbelt). Execution reports an error if the required isolation backend is unavailable.
- **Python 3.10 or newer, Git**, and the runtime required by each selected CLI. npm-based CLI installations also need a compatible Node.js version.
- **Install and sign in to the CLIs you choose:** Codex, Claude Code, Gemini CLI, Grok, and Cursor Agent can be combined in one team. Installing all five is optional.
- **Model usage:** you can set usage limits for individual team members to avoid wasting tokens or exceeding your allowance.

On macOS, Homebrew can supply the basic tools:

```bash
brew install python git
```

After installing Euler, run `euler-team doctor` to check OS isolation and CLI availability without calling a model.

## 2. Install Euler

You can give this page together with the [Euler GitHub repository](https://github.com/canghaimeng/Euler) to a coding agent such as Codex or Claude Code and ask it to install and configure Euler for your operating system and team. The sections below cover manual installation and common configuration options.

On the Mac or Linux computer that will run the team:

```bash
git clone https://github.com/canghaimeng/Euler.git
cd Euler
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[gateway,observability]'
```

This installs the commands used below: `euler`, `euler-team` and `euler-milestones`, plus the workbench server. In every new terminal, run `source .venv/bin/activate` inside the `Euler` folder before using them.

## 3. Sign in to the model command-line tools

Install and sign in to the CLIs you plan to use:

| Team `provider` | Executable | Sign-in |
| --- | --- | --- |
| `codex` | `codex` | `codex login`, or your configured API authentication |
| `claude-code` (`claude` also accepted) | `claude` | `claude auth login` |
| `gemini` | `gemini` | Run `gemini` and choose Google sign-in |
| `grok` | `grok` | `grok login` |
| `cursor` | `cursor-agent` | `cursor-agent login` |

Installation references: [Codex](https://github.com/openai/codex), [Claude Code](https://code.claude.com/docs/en/setup), [Gemini CLI](https://geminicli.com/docs/get-started/installation/), Grok, and [Cursor](https://cursor.com/docs/cli/installation). The Gemini team adapter targets the 0.63 ACP and session-storage interfaces; Cursor must expose ACP with `session/load`. See [CLI support](CLI-SUPPORT.md) for the implementation and validation matrix.

On a remote host without a browser, use the CLI's device sign-in where available, such as `codex login --device-auth`. Credentials stay in local provider configuration, private member runtime directories, or the OS Keychain; they are excluded from milestones and browser responses.

Run the workbench from the same terminal environment in which the selected CLIs can reach their provider. If your network requires a proxy, configure the CLI's HTTP/HTTPS proxy there; a browser proxy alone may not cover terminal processes. Keep `127.0.0.1` and `localhost` in `NO_PROXY` / `no_proxy` so local verification requests stay on this machine.

Use model IDs available to your account. Codex, Claude, and Grok use their native effort options. For Gemini and Cursor, set `effort` to `model-defined`: the declared model ID and its supported parameters determine those settings. Use `cursor-agent models` to inspect Cursor's model IDs.

## 4. Describe your problem and your team

Everything about a project starts from one definition file. From the `Euler` folder, copy the example and edit the copy:

```bash
mkdir -p ~/euler-research
cp examples/personal-research/research.json ~/euler-research/euclid.json
```

The fields you will change most often:

| Field | What it means |
| --- | --- |
| `id`, `title` | A short identifier (letters, digits, `-`, `_`) and the name shown in the workbench. |
| `problem.statement` | The exact statement the team works on. Write it carefully: it is fixed for this project. |
| `problem.assumptions`, `parameter_domain`, `definitions`, `quantifiers` | The scope: hypotheses, where the variables range, and which definitions are meant. |
| `problem.target_kind` | `PROOF`, `COUNTEREXAMPLE`, `CLASSIFICATION` or `FORMALIZATION`. |
| `problem.required_evidence_level` | The kind of checked evidence that would close the goal, for example `LLM_CHECKED_PROOF` (an independent proof check) or `KERNEL_CHECKED_PROOF` (a Lean kernel check). |
| `team.members` | Exactly one `lead`, plus assistants. Each selects `codex`, `claude-code`, `gemini`, `grok`, or `cursor`, with a model, effort and assignment. A Codex member is optional. |
| `team.max_turns` | Optional total turn limit for this workspace; omit it or use `null` for no limit. Your questions and the lead's consultations each count. |
| `team.members[].max_turns` | Optional turn limit for an individual member; omitted means no limit. |
| `team.turn_seconds` | Time limit for each non-Grok turn (10–3600 s). Grok turns have no fixed limit; they run until they finish or you pause the team. |
| `task_budget` | Resource limits for tasks Euler schedules itself, such as an optional single task. Set team conversation limits with `max_turns`. |
| `workbench` | Address and port of the workbench. Keep the host `127.0.0.1`. |

To change the statement or scope later, create a new project from an edited definition. The team and its records are tied to the exact statement they started from.

## 5. Create the workspace

```bash
python examples/personal-research/create_workspace.py \
  --definition ~/euler-research/euclid.json \
  --workspace ~/euler-research/euclid
```

This takes a second and contacts no model provider. It refuses to write into a folder that already contains files, so existing work cannot be overwritten. It creates:

```
~/euler-research/euclid/
  projects/euclid-primes/   the project: statement, scope, target goal, team roster
  history/euclid-primes/    private Git history for milestones (outside the project)
  workbench/                workbench settings and start-workbench.sh
  setup/                    the exact inputs used (contract.json, policy.json, team.json)
```

The script initializes the exact problem contract, creates the main OPEN goal, registers the team and writes the workbench configuration. You do not need to repeat these initialization steps. It reports missing CLIs, sign-ins or Landlock support without contacting a provider.

## 6. Connect the team and open the workbench

Connect each member's native session. Preparation connects the configured providers without asking a mathematical question. Claude prepares its runtime first and creates or forks the native conversation on the first Send:

```bash
EULER_RESEARCH_PROJECT="$HOME/euler-research/euclid/projects/euclid-primes"
euler-team prepare --project "$EULER_RESEARCH_PROJECT"
```

When it succeeds, the team state becomes **Team ready**, and Euler saves an automatic milestone. If it fails, the message names the member and the reason (see section 20).

Start the workbench in a terminal you can leave open, for example in `tmux`:

```bash
bash ~/euler-research/euclid/workbench/start-workbench.sh
```

Open `http://127.0.0.1:8130`. The page only reads and displays; starting it does not start the team.

**Running locally.** If the team runs on this Mac or Linux machine, open the local address directly; no SSH forwarding is needed.

**Connecting from another computer.** Keep the workbench on `127.0.0.1` and forward the port over SSH. On the Mac, run:

```bash
ssh -N -L 8130:127.0.0.1:8130 you@your-linux-computer
```

Then open `http://127.0.0.1:8130` on the Mac. This uses your own research host through the SSH connection, with no additional workbench password. An existing workbench on your private network can also remain your entry point.

You can also connect the sessions from the browser instead of the command line: **Team interaction → Restore every member's native session** does the same as `euler-team prepare`.

### Create a project from the workbench

After starting the workbench, choose **Create project** beside **Research milestones**. Enter a name, choose the lead’s CLI, model and effort, and write the first research request. **Create and start conversation** switches to the new project’s **Team interaction**, where session preparation and the lead’s reply appear. Each project has its own directory, research records and conversations; subsequent projects need no repeated manual initialization files.

## 7. Ask your first question

1. Click **Team interaction** at the top of the page.
2. Choose the lead (`codex · Lead`) in the member list on the left.
3. Type a question in **Your question**. For the example:

   > Prove the main goal with the team. Keep the research graph up to date as you work, record obstacles, submit complete proofs for independent checking, and audit the original conclusion before reporting success.

4. Press **Send**.

The work runs on your Mac or Linux research host. The lead takes a turn; when it consults the assistant, the assistant takes a turn, and its reply is delivered back to the lead, who takes another turn. A bounded consultation typically uses three turns. When you request sustained research, the lead can enable continued work from the current frontier until the goal is accepted, input is needed, or a run limit is reached. Replies appear in that member’s conversation on the right. Active work is checked every three seconds while this page is open. You can close the browser; the work continues.

Each turn can take several minutes. While the lead works, the member list says **Working** and the newest message is **Working** or **Queued**.

### Live research and checking

The native team receives research tools automatically. The lead records proof routes and can refine individual steps; members record scoped obstacles as they arise. An assistant submits a self-contained proof through the worker MCP gateway. Only a real checker acceptance creates a Fact and links evidence to its exact research obligation. A rejected argument leaves the obligation open and records repair information. The lead then requests a separate, fresh audit of the original goal.

A configured `EULER_VERIFY_URL` selects your verification service. Otherwise the runner starts a private loopback service, preferring a Grok, Codex or Cursor verifier member, then an assistant, then the lead's provider. Each verification uses a fresh verifier process. This service uses the selected CLI's local login and closes when the runner exits. Starting it does not call a model. Teams using only Claude Code or Gemini need a configured verification service or a supported verifier member. A missing service is reported as a blocker, never as a passing proof.

The sample's `task_budget` limits also apply to the final audit. The lead supplies an explicit audit reservation for limited dimensions, or the operator sets `EULER_FINAL_AUDIT_RESOURCES` using the resource-vector fields in [configuration](configuration.md). Native conversation limits count turns, including continued research; they are not provider token or monetary caps.

When sustained research is enabled, each turn receives current scoped context from the research state. Three lead turns without a frontier change pause the campaign for review. The notice appears in Team interaction. An unchanged frontier is an operational signal, not a mathematical impossibility claim. Short explanations do not automatically start a continuing campaign. **Pause team** and **Stop team** remain available.

## 8. What you will see first

A new project looks almost empty, and that is correct.

- **Before the first plan:** the Overview shows a single **Main goal** card marked **Open**, the summary **Main goal not yet closed**, and every evidence level at zero. Diagnostics lists the events that created the project: the problem contract, the main-goal obligation and the task budget.
- **After checked work returns:** admitted evidence closes matching steps. The original goal still requires its independent final audit; a discussion reply alone does not close it.
- **After the lead records a plan:** the main goal gets a **Proof route** (marked **Proposed**) and the route's steps appear as **Proof module** cards marked **Open**. Lines connect each step to the route and the route to the main goal. Live research shows "New research route registered".

These records begin as proposals. Section 10 explains how registered verification evidence changes a step to **Closed**.

## 9. The six views

The tabs under the project name cover proof reading, research progress and team conversations. The research views refresh every 15 seconds while **Live sync** is on; active conversations are checked every three seconds.

### Overview: where does the proof stand?

- The header shows the project title and **Main goal · original statement**; **Show original** expands your exact statement.
- **Main-goal acceptance status** shows whether acceptance has been recorded and links to the main goal. A completed proof, a positive teammate review, and recorded goal acceptance are separate stages; without recorded acceptance the goal still reads “not yet closed”.
- **Proof structure** draws the recorded proof architecture from left to right: foundational steps, routes, and the main goal. Each card shows its kind, its state and how many records lie below it (for example "3 below · 3 open"). Lines are recorded dependencies, from a premise to the result that uses it. Click a card to open its summary panel on the right.
- **Evidence levels** counts five separate kinds of record: proposed routes, self-reports, accepted evidence, checked components and main-goal closure. Each level reflects its own evidence and acceptance requirements.
- **Proof routes** lists the methods proposed for reaching a goal and how many premises each still needs. Main-goal closure requires its own final acceptance record.
- **Live research · happening now** shows the members, their assignments and the latest public records.
- **Start reading here** suggests an order for reading the records.

### Proof reader: what exactly does this step say, and why?

The reader shows one record at a time. The contents panel on the left groups records (main goal, open steps, routes, closed steps) and has a filter. For the selected record you see:

- **Statement · original record**: the exact text that was recorded.
- **① What it uses · direct premises**
- **② How it follows · the argument**: the original argument, when one is recorded. For a proposal it says so plainly ("This record has no original proof text") and lists the recorded routes and their steps.
- **③ How it is checked · registered evidence**
- **④ What it supports next**

**Back to the main goal**, **Previous step** and **Copy link to this step** help you move around and share your place.

### Live research: what is happening now?

This view lists the members with their assignments, the open routes and problems, and research activity such as new routes, attempts, obstacles and results. Member conversations are in **Team interaction**, the sixth tab.

### Relation map: how is everything connected?

The map draws every recorded node and relation.

- **Main line** shows the dependencies leading to the main goal. **Frontier** shows where work is still open. **All records** shows everything.
- **Research records** is the working graph of goals, steps and routes. **Admitted facts** shows only results that passed Euler's checks.
- The left panel has a **Guide**, a list of **Relations** and a list of **Nodes**. Press `/` to search.

### Diagnostics: is the machinery healthy?

Diagnostics lists the configured members and the log of control events, such as the contract, new obligations and routes. Here members may show "Unknown status"; the live state of the team is in **Team interaction**. Use Diagnostics to inspect execution and the Proof reader to inspect mathematical evidence.

### Team interaction: project → branch → member

This is a full conversation page alongside the other five views. The left side identifies the project and selected branch, then lists that branch's members. Expand **Branch** to switch between the original workspace and its saved branches. Choose a member to see that member's questions, public replies and consultations on the right; write your next message in the bottom input.

Use **＋ Add member** below the member list to choose a name, CLI, model, effort and assignment. The member joins only the selected branch and connects its native session on its first task. You can also ask the lead to add a configured teammate and delegate work through the team tools. Roster changes and native conversations are retained in subsequent milestones.

**Saved history** marks messages inherited from the checkpoint; **This branch starts here** marks subsequent work. Later conversations in a parent or sibling branch never enter this branch's history. Use **Show earlier messages** to inspect older exchanges. Drafts are kept separately by branch and member in the current browser tab, including when you switch views or reload. A new-reply button appears when you are reading older text, so polling does not pull you away.

### What the state labels mean

| Label | Meaning |
| --- | --- |
| **Open** | Needs to be established; nothing accepted yet. |
| **Proposed** | A route or step someone suggested. |
| **In progress** | Someone is working on it. |
| **Blocked** | It waits on something else, which may be mathematical, operational or budgetary. |
| **Closed** | Accepted with registered evidence of the required kind. |
| **Parked**, **Refuted**, **Stale record** | Set aside; shown false; or no longer valid because something it relied on changed. |
| **Check failed**, **Check unconfirmed** | Evidence was registered, but its check did not pass or has no result. |
| **Recorded** | Appears in imported historical archives that are documented from original sources. |

Assess progress through the main goal and its outstanding prerequisites. A single unresolved step can determine whether the goal closes.

## 10. How a step becomes Closed

Team members write drafts, review arguments, and record proposed steps. A step becomes **Closed** when Euler registers evidence meeting your contract's required level. For `LLM_CHECKED_PROOF` that means an independent proof check through Euler's verification service; for `KERNEL_CHECKED_PROOF`, a Lean kernel check. These verification tools are set up separately from this guide's first path; [CLI and tools](cli-and-tools.md) lists the commands (`euler kernel-check`, `euler admit-fact`).

Until then, read the team's drafts yourself. Members write files inside the project folder; for example, the lead's working folder is `projects/euclid-primes/workers/codex/`. Milestones save these files. The workbench displays registered Euler records and their verification results. Open the project folder to read other files.

## 11. Follow one step and its proof path

1. On the Overview, click a card in **Proof structure**. The right panel shows the step's statement, what lies below it, whether an original argument exists, and the latest record.
2. Choose **Read premises, argument and evidence** to open it in the Proof reader, or **Locate in the relation map** to see it in context.
3. In the Relation map, clicking a node opens **Node details** and highlights its recorded chain: upstream premises in blue, downstream conclusions in green. **Full dependency path** isolates that chain. **Neighbours**, **All premises**, **All downstream** and **Whole graph** change how much you see. **Copy ID** copies the record's identifier.
4. Click a line between two nodes to see the relation's type and its recorded rationale.

Highlighted paths show recorded dependencies. Open each step's argument and registered evidence to inspect the derivation and its verification status.

## 12. Original wording and interface language

The interface starts in English. **中文** switches the buttons, headings and explanations to Chinese and remembers your choice; `?lang=en` or `?lang=zh` in the address sets it directly. Your statement, the team's steps and replies, and your own questions always appear exactly as written. Mathematical content keeps its original language and wording.

## 13. Live view versus running work

The page reads the records every 15 seconds (**Live sync**; turning it off only stops refreshing). Team interaction checks active work every three seconds while visible. The reading views refresh independently of research execution.

Work starts in exactly three ways:

1. you send a question;
2. the lead consults another member during one of its turns;
3. you resume queued work.

Research runs in a background process on the Mac or Linux host and continues when the browser or workbench closes. Use **Pause team** or **Stop team** to control execution (section 17).

## 14. Talking with the team

**Who receives your question.** The member selected on the left, in the branch shown above the member list; normally the lead. Assistants answer you directly when you ask them. Only the lead can consult other members and record proposed steps, so plans that involve the whole team should go to the lead.

**Asking about a particular step.** **Ask the current team** in a node's action group opens Team interaction with that step attached. Its recorded statement, scope when available and ID are included in the native member's input. You can remove or change the attachment, or attach a step directly from the composer. No terminal is needed.

**Where replies appear.** On the right, in the selected member's conversation. Your message first shows **Queued**, then **Working**, and the public reply appears in place when ready. Consultations show which member was asked and let you open that member's conversation. Switch members on the left to inspect their respective histories. A transport failure offers an explicit retry with the same request identity, preserving the original message's idempotency.

**What members remember.** Each member continues its own native conversation, including earlier questions and its own replies. Members share information through explicit consultations.

## 15. Save a milestone

A milestone saves, together, the project files, the research graph, tasks, evidence, and each member's native conversation. It goes into local private Git history in `history/`, outside the project. This history has no remote and records the team's saved state. Mathematical acceptance stays with each goal's evidence records.

- **Automatic milestones.** After sessions are connected and after each settled team turn, Euler saves a "Team state checkpoint" if anything changed.
- **Named milestones.** Open **Research milestones**, expand **Save the current team state**, enter a **Milestone label** such as "Plan reviewed by grok", and choose **Create Git milestone**. The team must be idle; if a turn is still running, wait and try again.

The milestone list marks each entry **team state** or **files only**. Only milestones with team state can resume the members' conversations.

## 16. Continue from an earlier point

Use this when you want to try a different direction without disturbing current work.

1. Select a step (or none), then choose **Ask from a milestone**, or open **Research milestones**.
2. Pick a milestone. The note under the list says whether it includes the members' native conversations.
3. Optionally write the question you have in mind, then choose **Restore full workspace and prepare question**.
4. The page opens **Team interaction** in the new independent branch. Its project, branch and source milestone appear on the left, with inherited conversation history on the right. Your original workspace is unchanged.
5. The branch shows **Sessions awaiting recovery** and Send stays disabled. Choose **Recover this branch’s member sessions** in the conversation area. Codex and Grok create native forks; Claude forks on the first Send. Gemini and Cursor resume complete native stores copied into independent branch directories, retaining their provider session IDs. The UI identifies the recovery method. The member resumes the native conversation state saved at the milestone; later messages from the original workspace are not copied into it.
6. Your saved question is placed in the lead’s composer as an unsent draft. Review it, choose the appropriate member, and press **Send**. The saved-question note can put it back in the composer if needed. Pressing **Send** starts the question.

Restoration preserves the mathematical state at the selected milestone: open goals remain open, and existing acceptance records are retained. Proof acceptance remains tied to its registered evidence throughout pause and recovery.

Continued research is disabled on restoration until the lead explicitly enables it for the new request. A restored workspace starts with a fresh turn allowance and keeps its own milestones. It stays in the project selector after you restart the workbench.

If restoring with a selected step fails with "selected object does not exist in this exact milestone", the step was created after that milestone. Choose a later milestone, or restore without selecting a step.

**Three ways to continue, compared.**

| | Ask the current team | Ask from a milestone | Inspect object record |
| --- | --- | --- | --- |
| Where | This workspace, as it is now | A new, independent copy of a saved point | This workspace |
| Who answers | The member you choose, continuing its conversation | The restored members, continuing their conversations from the milestone | Nobody; it shows the record's exact version, scope and evidence |
| What starts work | **Send** | Recover sessions, then **Send** | Read-only inspection; configured single tasks have a separate Start action |
| Use it to | Push the current line of work forward | Explore an alternative, or keep going after the turn allowance is used | Check precisely what a step says and depends on |

When single tasks are configured, the third entry reads **Inspect object · single task**. It can then prepare one bounded task for a separately configured worker; that task is previewed and starts only when you press its own Start button. It is off in the example workspace; see [Human intervention](HUMAN-INTERVENTION.md) to set it up.

## 17. Pausing, quotas and limits

Once sessions are connected, **Team interaction → Run controls** offers three buttons:

- **Pause team** interrupts the current turn and holds queued questions. A new question you send afterwards still runs; the held ones wait until you resume.
- **Resume queued work** releases held questions and continues.
- **Stop team** interrupts the current turn and refuses new questions until you resume.

Pausing or stopping is an operational event; it changes no mathematical record.

**Goals and execution.** Team state records each member’s objective. Codex additionally keeps a native goal, paused in conversation mode. No member continues on its own: every turn starts from a question, either yours or the lead's consultation of another member.

**Custom usage limits.** If you set `max_turns`, reaching it stops new tasks for the corresponding team or member: the dialog reports "team turn allowance exhausted", or the team shows **Turn allowance exhausted**. To keep going, restore the latest milestone (section 16): the restored workspace starts with a fresh allowance.

**Time limit.** A non-Grok turn longer than `turn_seconds` is interrupted and marked **Failed**. Grok turns have no fixed time limit.

## 18. Coming back the next day

Start the workbench again with `start-workbench.sh` and open the page. The team's sessions are saved on disk; you do not need to prepare them again. Open **Team interaction** and continue asking. If a provider has signed you out, run its login command again; Euler copies the renewed sign-in before the next turn.

If the computer stopped in the middle of a turn, that member can stay marked **Working**, and the queue cannot continue. Restore the most recent milestone, which was saved while the team was idle, and continue in the restored workspace.

Keep `projects/`, `history/` and `workbench/` together where the script created them. The settings refer to these folders by absolute path, so moving them breaks the links.

## 19. Older projects without saved conversations

Some projects in a workbench may be imported archives: finished research documented from original sources, with no registered team. You can read them completely in the research views, but there is no team to ask. Their milestones are **files only**. Restoring one gives you the files and graph in a new workspace, without the old conversations.

Conversation recovery requires a saved native archive for the member. Records with only historical source material show "Historical record · read-only here".

## 20. When something does not work

| You see | What it means | What to do |
| --- | --- | --- |
| `native provider binary unavailable` | The `codex` or `grok` command is not on the PATH of the process. | Install the CLI, or start the workbench from a terminal where it works. |
| `Landlock ABI 3 or later is required` | The kernel cannot isolate agent turns. | Use a newer Linux kernel with Landlock enabled. |
| `model drift while restoring`, `Grok configuration did not match` | The member's model name is not available to your account. | Use a model your CLI offers (section 3) in a new project definition. |
| `restore every team session before asking` | Some members' sessions are not connected yet. | **Recover this branch’s member sessions** or `euler-team prepare`. |
| `team turn allowance exhausted` | `max_turns` is used up in this workspace. | Restore the latest milestone and continue there. |
| `team must be explicitly resumed before new work` | The team was stopped or paused by quota. | **Resume queued work**. |
| `team agent turn still running` when saving | A turn is in progress. | Wait until replies arrive, then save again. |
| `This project is read-only in this deployment` | The workbench was started without this project's settings. | Start it with `start-workbench.sh`. |
| Team interaction says “No team conversations in this workspace” | No resumable member sessions are registered here. | The research records remain readable; select a branch with a registered team to converse. |
| The page does not open | The local service is not running, or a remote port is not forwarded. | Start the local workbench script, or check SSH forwarding for remote access. |
| `macOS sandbox-exec unavailable` | OS isolation is unavailable. | Run `euler-team doctor`; Euler does not fall back to unsandboxed execution. |
| Cursor reports a locked login Keychain over SSH | macOS has not made the login credentials available. | Unlock the login Keychain locally on the Mac or complete CLI sign-in, then retry; the workbench does not request the Keychain password. |

## 21. The same actions from the command line

The browser is the recommended way to work. These commands do the same on the Mac or Linux research host. In a new terminal, activate the virtual environment and set `EULER_RESEARCH_PROJECT` to your project path as in section 6:

| Purpose | Command |
| --- | --- |
| Connect member sessions (no questions asked) | `euler-team prepare --project "$EULER_RESEARCH_PROJECT"` |
| Show the team, its messages and replies | `euler-team status --project "$EULER_RESEARCH_PROJECT"` |
| Ask a member (starts work) | `euler-team ask --project "$EULER_RESEARCH_PROJECT" --member codex --question "…"` |
| Pause, stop, resume | `euler-team pause --project "$EULER_RESEARCH_PROJECT"` (likewise `stop`, `resume`) |
| Run held questions after a command-line resume | `euler-team run --project "$EULER_RESEARCH_PROJECT"` |
| Save or list milestones | `euler-milestones capture --project "$EULER_RESEARCH_PROJECT" --label "…"` / `euler-milestones list --project "$EULER_RESEARCH_PROJECT"` |
| Show open steps, routes and evidence | `EULER_AGENTS_ROOT=~/euler-research/euclid/projects euler frontier euclid-primes` |

Restore milestones from the browser: it registers the new workspace in the project selector and carries the team settings over.

## Further reading

- [Team recovery](TEAM-RECOVERY.md): how native sessions are saved, forked and isolated.
- [Research milestones](RESEARCH-MILESTONES.md): what a milestone contains and excludes.
- [CLI and tools](cli-and-tools.md): verification commands that register checked evidence.
- [Human intervention](HUMAN-INTERVENTION.md): optional single tasks.
