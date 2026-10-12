# Euler — Verification and access controls

<!-- language-nav -->
[English](security-and-trust.md) · [简体中文](security-and-trust.zh.md) · [Back to Euler](../README.md)
<!-- /language-nav -->

This guide describes how Euler checks mathematical results, controls access to shared records, and runs provider processes.

## 1. Verification evidence

The verification service reviews statements and proofs using an independent LLM session. A `correct` verdict records that reviewer's judgment. The service checks the response schema and consistency of the verdict, findings, and reported gaps. LLM judgments are fallible; use expert review when preparing a result for publication.

Formal workflows can supply Lean kernel checks and statement-alignment evidence. Each project's contract specifies the evidence required for a step and for final acceptance. The workbench displays the recorded evidence type and its source.

Switching verifier backends preserves active Facts and their original provenance. Request a new review to obtain fresh verification evidence. Use `fact_revoke` and its dependency cascade when a concrete defect invalidates an accepted result.

## 2. Gateway permissions

The MCP gateway exposes tools according to `euler/gateway/roles.py`:

| Role | Available tools |
| --- | --- |
| Worker | `gm_add`, `gm_search`, `fact_submit`, `fact_search`, `search_arxiv_theorems` |
| Main | `gm_add`, `gm_search`, `fact_search`, `fact_revoke`, `search_arxiv_theorems` |
| Verifier | `search_arxiv_theorems` |

Workers submit proposed Facts through the verification gate. The main agent coordinates research and can revoke accepted Facts. The verifier's gateway access is read-only. An unknown or unset role receives the restrictive verifier tool set; the complete development tool set requires `EULER_ROLE=all`.

## 3. Fact admission

The production admission path is:

1. A worker calls `fact_submit` with a statement and proof.
2. The gateway requests verification and admits the Fact after a valid `correct` verdict.
3. The gateway records the outcome in global memory, including rejections and write failures.

An unavailable verifier or malformed result produces an error and leaves the Fact unadmitted. Workers write shared mathematical records through the gateway. Missing MCP tools or broken worker configuration produce `PRECHECK_FAILED`. The `/health` endpoint identifies the verification service process; mathematical verdicts come from verification requests.

## 4. Provider processes and isolation

Treat agent contracts, skills, and provider configuration as trusted executable inputs. Run autonomous verification on a dedicated host or isolated environment with access limited to the intended research data.

The Codex verification launcher bypasses its CLI sandbox. The Cursor verification launcher uses forced tool approval and a separate agent home. Its child environment is allowlisted, and the home contains the selected authentication and verifier configuration. The verifier gateway uses `EULER_ROLE=verifier`.

Native research-team turns use Linux Landlock or macOS Seatbelt for filesystem isolation. Provider processes receive access to the configured branch workspace; network policy is managed on the host. See [CLI support](CLI-SUPPORT.md) and [team recovery](TEAM-RECOVERY.md) for these execution paths.

## 5. Network access and credentials

The default service addresses are `127.0.0.1:8091` for verification and `127.0.0.1:8099` for the workbench. Use SSH forwarding for access from another device. Bind external interfaces only within an explicitly configured, trusted deployment.

Store keys and service tokens in ignored `config/*.env` files or the provider's login store. Generated provider configuration references environment-variable names for keys. Before committing, check that account configuration and `runtime/` remain untracked.

## 6. Deterministic checks and evaluation

Pre-checks reject empty statements, unsupported conditional premises, and selected malformed proof patterns before LLM review. Review these predicates when adapting the verifier to a new domain. Passing them advances the request to the reviewer.

Software tests exercise role permissions, admission transitions, content-addressed records, recovery, and revocation. Mathematical acceptance follows the project's evidence requirements and final audit. The paper-writing workflow adds a review of the assembled manuscript.
