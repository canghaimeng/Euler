# CLI and platform support

Euler's persistent teams accept `codex`, `claude-code` (alias `claude`), `gemini`,
`grok`, and `cursor` (alias `cursor-agent`). A team has one lead and any chosen
assistants or reviewers. A Codex member is optional. The independent worker
registry also includes all five providers.

## Run on Linux or macOS

Use Python 3.10+ and Git on the machine that runs the agents. On macOS, install
the selected CLIs there and open the workbench locally; a Linux server is optional.
Run `euler-team doctor` to inspect executable availability and OS isolation without
starting inference. The initializer is
`examples/personal-research/create_workspace.py`; `research.five-clis.json` is an
editable example, not a requirement to run five agents.

Linux uses Landlock ABI 3+. macOS uses `/usr/bin/sandbox-exec` (Seatbelt).
Both permit writes to the current branch's captured roots and deny reads and
writes through the original research roots, including symlink access. Each CLI
also gets a short private temporary directory for sockets, removed after it exits. Neither
backend is a network sandbox. Missing isolation is an error, with no automatic
unsandboxed fallback. The bootstrap script selects the appropriate Node archive
for Linux/macOS and Intel/ARM.

## Native conversations and recovery

| CLI | Team transport | Saved-state continuation |
| --- | --- | --- |
| Codex | Native app-server RPC | `thread/fork` from the frozen rollout; new native ID |
| Claude Code | Print-mode JSON stream | `--resume` and `--fork-session`; fork created on the first explicit Send |
| Gemini CLI | ACP | Full native session file copied to an independent provider store, then `session/load` |
| Grok | Native ACP | Native session-fork extension; new native ID |
| Cursor Agent | ACP | Full native session store copied to an independent provider store, then `session/load` |

Gemini and Cursor keep their provider session IDs inside distinct stores. The
Euler team instance, files, mailbox and subsequent history are independent;
the UI calls this **isolated native restoration**, not a provider-created fork.
Some CLIs do not persist an empty conversation before the first prompt. These
members show **CLI ready**; no missing conversation is fabricated. A Claude
snapshot waiting for its first fork shows **saved conversation ready**.

Credentials and provider configuration are excluded from conversation archives.
macOS credentials may use the provider's Keychain; Linux credentials are copied
only to the branch's ignored runtime home. No conversation digest is substituted
for an unavailable native archive. Only public replies reach the browser;
private reasoning and native session files are not exposed there.

Codex/Claude/Grok accept native effort settings. For Gemini/Cursor set
`effort: "model-defined"`; select the intended model and supported parameters
through its model ID. Cursor ACP accepts canonical variant IDs, including bracket
parameters offered by the provider; `auto` maps to its advertised Auto option.
Unsupported IDs fail rather than falling back to another model.

## Budgets and validation boundaries

Release defaults do not require a remaining-account percentage or a specific
Codex login method. Users can set optional `team.max_turns` and individual
`team.members[].max_turns` limits; omitting them leaves the corresponding limit
unset. These are turn budgets, not measurements of token spending. Token or
currency caps can be set through the provider's account controls.

An operator may opt into the existing included-account guard through
`quota_policy.codex_stop_percent` when creating a team with `euler-team init`.
Previously saved deployment policies remain in effect; new projects do not
inherit a fixed percentage threshold. Grok retains its unlimited whole-turn
duration; the other adapters honor `turn_seconds`.

Adapters were checked against Codex 0.159.0, Claude Code 2.1.198, Gemini CLI
0.63.0, Grok 1.0.50 and Cursor Agent 2026.10.01-e373342 interfaces. The Gemini
adapter relies on the 0.63 ACP and project-registry layout. An older CLI without
those interfaces must be upgraded.

`euler/workspace/tests/test_cli_adapters.py` exercises actual subprocesses,
OS isolation, public-output filtering, native-store preservation, branch
independence, and recovery using **local protocol doubles**. These tests do not
establish compatibility with every provider account or prove mathematical claims.
Private real-provider transcripts and development validation archives are not part of this distribution.

Interface references: [Claude CLI](https://code.claude.com/docs/en/cli-reference),
[Gemini sessions](https://geminicli.com/docs/cli/session-management/),
[Gemini configuration](https://geminicli.com/docs/reference/configuration/),
[Cursor CLI](https://cursor.com/docs/cli/overview). Installed CLI help and local
provider code were also checked; Cursor's ACP surface may differ by release.
