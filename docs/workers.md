# Worker launch examples

Portable relative paths only. From the repo root:

```bash
source ./scripts/env.sh
# or, from scripts/:
source ./env.sh
```

Native providers (`codex`, `claude-code`, `grok`, `cursor`) are selected by
`PROVIDER=` in each worker `.role` file.
Create one worker of each native provider:

```bash
euler new DEMO --roles codex:1,claude-code:1,grok:1,cursor:1
```

Custom Codex-only roster:

```bash
euler new DEMO --roles high:3,xhigh:6
```

Default `euler new` scaffolds a six-worker native roster: Codex×2, Grok×2,
Claude Code×1, and Cursor×1.
