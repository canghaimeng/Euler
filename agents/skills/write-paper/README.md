# write-paper — publication recipe, prompts, and style

This directory contains both the reusable `SKILL.md` workflow and the fixed files
consumed by the `write-paper` MCP service. The service reads the role, style, and
boilerplate assets at call time and embeds them into an isolated authoring prompt.

Located via `EULER_WRITE_PAPER_SKILL_DIR` (default `<repo>/agents/skills/write-paper`).

- `roles/`       — `PAPER_CONTRACT.md` (the PRIME DIRECTIVE) + the writer / reviser / auditor / verifier / style-distiller role prompts, plus the `PAPER_PLANNER` + `PAPER_SECTION_WRITER` prompts used only when a too-large closure is written section-by-section (see below)
- `style/`       — `STYLE_GUIDE.md` (voice) + `PAPER_STRUCTURE.md` (per-section plan) + `anchors/` (optional operator exemplars)
- `boilerplate/` — `acknowledgement.md`
- `examples/`    — a toy end-to-end project used as the offline-test fixture + a worked demo
- `driver/`      — compile, ledger, and optional publication helpers
- `templates/`   — project brief and revision-log templates

**These files are agent-facing runtime inputs, embedded verbatim into the codex
prompts** — keep developer/architecture orientation OUT of them and put it here
instead. The role map, for developers:

- `PAPER_WRITER_PROMPT.md` — writes the first complete `main.tex` from the brief,
  the ledger, and the fact-graph math.
- `PAPER_REVISER_PROMPT.md` — revises an existing `main.tex` (compile failures,
  citation fixes, operator annotations, gap-fill).
- `REFERENCE_AUDITOR_PROMPT.md` — offline; flags bibliography entries it cannot
  vouch for, never fabricates.
- `REFERENCE_VERIFIER_PROMPT.md` — online; verifies the auditor's flagged
  citations (arXiv + web); the tool writes the confirmed ledger rows. The online
  half of the auditor→verifier→reviser chain.
- `PAPER_MATH_VERIFIER_PROMPT.md` — the whole-paper math verifier. Deliberately a
  THIRD verifier, separate from the fact-submission verifier (`euler/verify`) and
  the reference verifier: it trusts confirmed citations and re-checks the paper's
  own reasoning as written. Its policy affects paper delivery only, nothing
  upstream.
- `STYLE_DISTILLER_PROMPT.md` — offline; distills style rules from
  `style/anchors/` into `style/STYLE_GUIDE.md`. Never auto-applies.

**Chunked (section-by-section) generation.** When a target closure's full-proof
writer prompt would exceed the model context window, the MCP auto-chunks (threshold
`EULER_PAPER_WRITE_CHUNK_CHARS`): a `PAPER_PLANNER` pass (closure STATEMENTS ONLY →
fixed preamble/front matter + a section plan covering every closure fact + the
bibliography), then one `PAPER_SECTION_WRITER` call per section (that section's full
proofs + the fixed macros/labels + other results' statements for `\ref`), then a
deterministic Python stitch. Each call is still a non-agentic isolated codex; the
single-pass path is unchanged when the closure fits. See
`agents/skills/write-paper/SKILL.md` Stage 2.
