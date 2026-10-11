# Restore a whole research team

A team checkpoint combines two things in one private Git commit:

- Project files, mathematical state, dependencies, assignments and receipts.
- Each registered member's native conversation archive, model/effort, role,
  goal metadata, pending requests and the lead/assistant message relationships.

The restored project is independent. Codex uses a native `thread/fork` from an
opaque frozen rollout copy; Grok uses its native session-fork RPC from the
captured session directory. These two providers receive new session IDs with explicit parent
IDs. Claude forks on the first Send; Gemini and Cursor resume full native stores
in branch-private directories, retaining provider IDs. See [CLI support](CLI-SUPPORT.md). Subsequent questions resume these forked sessions. No replacement agent
is given a model-generated digest of the old conversation.

## Start a persistent team

Use this host for new projects whose team conversations must be recoverable.
It supports Codex, Claude Code, Gemini CLI, native Grok and Cursor Agent on Linux and macOS. Existing file-only checkpoints do not gain
missing conversations retroactively. Unregistered external sessions require a supported import/fork adapter; they cannot silently
fall back to a summary.

Prepare a project directory and a definition such as:

```json
{
  "objective": "For every positive integer N, find a prime p > N.",
  "max_turns": 12,
  "turn_seconds": 300,
  "members": [
    {"id":"codex","role":"lead","provider":"codex",
     "model":"gpt-6.1-sol","effort":"high","assignment":"Coordinate the proof and consult Grok."},
    {"id":"grok","role":"assistant","provider":"grok",
     "model":"grok-4.6","effort":"high","assignment":"Check the construction independently."}
  ]
}
```

```sh
euler-team init --project /research/project --definition team.json
euler-team prepare --project /research/project
euler-team ask --project /research/project --member codex \
  --question 'Develop the proof and ask Grok to check its critical step.'
euler-team status --project /research/project
```

`prepare` connects native sessions or prepares their first-turn native creation/fork without starting inference. Native session
preparation and completed mailbox batches automatically create Git milestones
when the whole team is idle. The trigger includes conversation changes even
when the mathematical graph has not changed; checkpoint count is not a measure
of mathematical progress. The lead has
team-member, message, delegation and proposed-proof-step tools. A delegated
reply returns to the lead's existing conversation; the host preserves the
causal message IDs. Proposed obligations remain open until verified. Discussion
and peer review do not bypass the existing Fact-admission or final-audit gates.

`max_turns` optionally bounds new provider turns in a branch, and each member
can have its own `max_turns`. Limits are user-configured; omitted limits are
unrestricted. `turn_seconds` bounds non-Grok turns; **Grok has no fixed whole-turn
deadline**. Account-percentage monitoring is opt-in and is not enabled by release
defaults. Existing local monitoring policies are preserved. Credentials are
current runtime configuration, not archived team state.

## Save and restore

At a quiescent boundary, use the existing Git milestone workflow. Capture
refuses active member turns. Provider files are copied opaquely, checked for
concurrent modification, hashed and committed with the project. They are never
served by the browser or included in the public source package.

```sh
euler-milestones enable --project /research/project
euler-milestones capture --project /research/project --label 'Draft and peer review'
euler-milestones restore --project /research/project --commit FULL_COMMIT_HASH
euler-team prepare --project /path/printed/by/restore
```

Restoration preserves the roster and old mailbox, suspends queued work, and
records the original turn usage separately from the new branch's allowance.
All inherited conversations must recover successfully before new questions can run. Newly added members connect their native sessions on their first task. A missing
archive is a reported recovery gap, never a fresh worker pretending to be the
old member. Native goals remain recorded; recovery does not automatically start
unbounded autonomous goal continuation. Research resumes through explicit
questions or explicitly resumed queued work.

## Browser workflow

Use **Research milestones** to choose the time point and restore the workspace.
The restored branch opens in **Team interaction**. Recover its member sessions,
then select the lead or assistant on the left and continue in the conversation
on the right. The same sixth navigation entry opens the current project branch
without restoring or selecting a node. A proof node's **Continue from this step**
area offers **Ask the current team** and **Ask from a milestone**. The former
carries the selected node's identity into the current team's conversation;
the latter prepares an independent workspace from a saved point. Each entry
shows availability and explains when saved conversations are missing. The member list and expandable session details show provider/model/effort,
recovery state, source/fork IDs and goal metadata. Saved checkpoint history and
new messages in the current branch are marked separately. Selecting a different
branch changes the histories and the actual recipients of subsequent messages.

The conversation view shows public questions and replies, including delegated
consultations. It does not open a terminal and does not expose internal model
reasoning. **Inspect object record** opens version, scope and evidence details.
When single-task controls are enabled, **Inspect object · single task** remains
a separate bounded-action interface. Historical records without native identity
stay read-only; their proof reader and recorded dependencies remain available.

The personal workbench requires no separate control token. Send, save and
restore directly through their buttons. Native model accounts must still be
logged in on the execution host, and quota and turn limits remain enforced.
See the [user guide](DASHBOARD-GUIDE.md) for setup and the save/restore workflow.

Enable the registered team's controls with `EULER_TEAM_CONFIG`:

```json
{"projects":{"my-project":{"allow_prepare":true,"allow_launch":true,
  "max_turns":12,"turn_seconds":300}}}
```

Only server-configured catalog projects can launch. A restored project's policy
is inherited only when its source was explicitly configured. Pause suspends
queued work and interrupts the current turn; Stop also blocks further requests.
A cancellation is an operational event, not a mathematical failure.

## Isolation and evidence boundary

Execution uses Linux Landlock ABI 3+ or macOS Seatbelt through `sandbox-exec`. Provider processes can write
inside the new workspace; source file contents and writes are denied, including
symlink access. Ancestor directory listings remain available for package
imports. This is filesystem isolation, not a VM image or a general network
sandbox. Current credentials, executable versions and operating-system process
handles are not restored from Git.

The included protocol tests exercise checkpointing, branch isolation, and native-session restoration with local CLI doubles. Private collaboration transcripts and development captures are excluded.

## Start projects and grow a team from the browser

**Create project** beside **Research milestones** accepts a user-defined name,
a lead CLI/model/effort, the goal type and the first message. The backend creates
a separate managed workspace, records the initial request as an open target,
registers the project and starts the durable first conversation. Definitions and
quantifiers omitted from the request remain explicitly unspecified.

**＋ Add member** below the roster adds a named assistant or reviewer to the
selected branch. An active lead can use `euler_team_create_member` and then
`euler_team_ask`. The tool records who added the member and its parent message;
ordinary assistants cannot register teammates or replace the lead. New members
start fresh native conversations on their first task. Existing and restored
member conversations are preserved. Roster changes are included in checkpoints.

The starter configures `EULER_PROJECT_CREATION_CONFIG`, a server-owned JSON file
with `allow_create`, `root`, `registry` and optional CLI `defaults`. Team and
milestone policy files remain configured through their existing environment
variables. Browser requests cannot choose filesystem paths, binaries or credentials.
