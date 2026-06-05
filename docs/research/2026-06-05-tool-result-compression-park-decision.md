# Intra-Turn Tool-Result Compression — Park Decision

**Status:** Complete (decision recorded)
**Date:** 2026-06-05
**Tickets:** FRE-486 (this check) · FRE-475 (parent) · ADR-0085 (parked by this) · FRE-476 (forward stream)
**Evidence:** FRE-475 forensics (traces `a0a07227`, `54362e56`, `950386d6`); offline mechanism test (PR #164); `docs/research/2026-06-04-artifact-turn-cost-latency-forensics.md`
**Decision:** **Park ADR-0085 dormant (flag-off). Do not ship intra-turn compression. Pivot to FRE-476 (decomposition + sub-agents).**

---

## Executive summary

ADR-0085 proposed cutting the ~40 % of input tokens that are full-price re-billing of the accreting
tool-output tail in long artifact-build turns. We took it to a final correctness check (FRE-486) and
**parked it** — on an *architectural* finding, not just a cost number:

The digest's only lossy primitive for unstructured streams is **head/tail truncation that deletes the
middle** (`tool_result_digest.py:_headtail`). When the model reads a source file via **bash**
(`cat`/`grep`/`sed`) — which it overwhelmingly does — that file content flows through the bash digest path
and **gets its middle deleted**. The model then reasons over mutilated source, abandons its
`cat`/`grep`/`sed` technique (live-observed, trace `950386d6`), and ships an incomplete artifact.

The deeper point is the load-bearing one: **the harness already solves file-read context-bounding**, and
it does so *through the very technique the digest corrupts*. The `read` tool head-caps reads (FRE-410,
~200 lines / 8 KB) and the always-injected `bash` skill + the `read` tool's own description instruct the
model to **`grep -n` to locate → read narrow ranges**. The digest is therefore a *second* truncation layer
fighting the harness's own read contract. For the artifact-build workload the re-billing tail *is* those
file reads — so a correct fix means "don't compress them," which leaves little to compress, and the cost
win evaporates. That is a clean park, not a failure.

---

## How we got here (the compression journey)

ADR-0085 / FRE-475 goal: cut the re-billed tool-output tail (origin trace `a0a07227`: 23 rounds, ~$1.14,
768 k full-price input — ~40 % of input). Behind `tool_result_compression_enabled` (default OFF):

| Step | What | Outcome |
| -- | -- | -- |
| PR-A (#160) | Pure byte-stable digest infra (`tool_result_digest.py`) | unwired |
| PR-B (#161) | Wired keep-deferred (case-b) | **+34.9 %** fresh input (cache churn) → reverted |
| Redesign (#162) | Birth-time (case-a) placement | digest before `ctx.messages.extend` (no churn) |
| FRE-484 (#163) | Forced-synthesis `tools=None` Anthropic crash | independent bug, fixed + deployed |
| Offline test (PR #164) | Real digest over a fixed tape, no model | mechanical ceiling ≈58.7 %; **flagged the file-truncation risk in its own caveat** |

Measured live (cloud/sonnet, owner identity):

| Trace | Flag | Fresh input | Artifact | Note |
| -- | -- | -- | -- | -- |
| `54362e56` | off (current build) | **757k** | ✅ built | clean control |
| `950386d6` | on (birth-time) | 1,237k | ❌ | failing artifact ground to the round limit |

FRE-484 did **not** cause the `950386d6` blow-up (it fired last, after all tokens were spent). Flag-off
cost is stable ~757 k regardless of `cat` vs `grep`. The cost driver with the flag on was the turn
grinding to the tool-iteration limit on a **failing** artifact — caused by truncated source.

## The root cause, precisely

- The `read` **tool** survives compression because reads are **pinned** (ADR-0085 §D4 / `_update_pins`):
  a `read` with a `path` is recorded as a pin and skipped by `_digest_candidate_entries` → kept verbatim.
- A file read via **bash** (`cat`/`grep`/`sed -n`/`head`/`tail`) is **not** pinned. It flows through
  `digest_tool_content("bash", …)` → `_digest_bash` → `_stream_digest(stdout)` → `_headtail`, which keeps
  the first 40 + last 20 lines and **deletes the middle** (`tool_result_digest.py:138`).
- ADR-0085 §D2 *anticipated* range-preserving reads ("`read` already encourages grep-then-range, FRE-410,
  which narrows the blast radius") — but it accounted only for the **`read` tool path**. It never
  accounted for those reads arriving through **bash**, where there is no extractor and no pin, only
  head/tail. That is the gap.

## The architectural finding (why this parks)

`docs/skills/` is the **live, operative** skill store (`orchestrator/skills.py:35` globs `docs/skills/*.md`
and injects the bodies into the system prompt every turn). The `bash` skill is **always** injected; the
`read-write` skill injects on file questions. The `read` tool's own description
(`primitives/read.py:50-58`) spells out the canonical technique verbatim:

> "(1) FIND what you need with grep via the bash tool (`grep -n "<keyword>" <path>`) to get line numbers;
> (2) READ each line number with `offset=<line> limit=60`; (3) REPEAT."

So the deliberate, in-prompt-every-turn file-reading contract is **locate-with-grep → read narrow
ranges**. Consequences:

1. The **most load-bearing** artifact in that contract is `grep -n` output — the model's *map* of line
   numbers. It is also the **most damaged** by head/tail elision: a grep with many hits is sparse
   line-numbered matches, and `_headtail` deletes the middle matches → the map is destroyed → the
   grep→read loop collapses. This is exactly the observed "model switched off its `cat`/`grep`/`sed`
   technique."
2. The harness **already bounds read size** (read tool head-cap; technique uses targeted greps + 60-line
   windows). Well-behaved reads are frequently *below* the 1,500-token digest threshold. The outputs that
   actually trip the digest are grep maps with many hits and whole-file `cat`s — **both load-bearing,
   both corrupted by middle-elision.**
3. Therefore the digest is a **second truncation layer fighting the harness's own read contract.** A
   correct fix (don't middle-elide file/grep content) removes most of what there was to compress in the
   artifact workload → the cost win largely evaporates. The ship-vs-park question answers itself from the
   architecture.

We considered two fixes (FRE-486 design) and concluded both converge on "don't compress file content":
- **Blocklist** (detect file-read commands, exempt) — fail-*dangerous*: a missed pattern still corrupts.
- **Allowlist / default-verbatim** (only compress recognized disposable noise) — fail-*safe*, but
  compresses little for this workload.

A measured A/B (master's step) would confirm the cost number, but the *correctness* conclusion and the
*architectural redundancy* are already established. We park on that basis and record it here so the park
is a conclusion, not an abandonment. (Master may still run one confirmatory flag-on A/B vs the 757 k
control if a number is wanted for the file — it is not required to justify the park.)

---

## Decision & disposition

**Park ADR-0085 dormant (flag stays `tool_result_compression_enabled=false`). Pivot to FRE-476.**

### Do we remove the compression code? — No (park dormant)
- It genuinely helps for large **non-file** outputs (JSON / `psql` / `curl` dumps) where head/tail or
  JSON-by-keys is lossless-enough and corrupts nothing — a real future use case the artifact workload
  doesn't represent.
- It carries two standalone-valuable pieces worth keeping: **fix-a** (`_NEVER_DIGEST_TOOLS` —
  never re-digest `expand_tool_result`, a correct defect guard) and the **reusable offline harness**
  (`scripts/eval/fre475_compression_ab/`).
- Removal is its own non-trivial PR (unwire from `step_llm_call`, drop the module + tests + config +
  telemetry). Speculative removal now is unjustified.
- **Anti-rot:** the flag description and module docstring are marked **PARKED + why** so nobody enables it
  on a file-read-heavy workload without the allowlist redesign. Removal is a **deferred option** — file a
  cleanup ticket if FRE-476 fully obviates compression and no non-file use case materializes.

### Do we remove the `read` tool? — No
Empirically the model prefers bash `cat`/`grep`, and `read`'s own description delegates the primary
technique to bash grep — but `read` is **not** redundant:
- **Mode availability:** `read` is allowed in all modes incl. **LOCKDOWN/RECOVERY**; `bash` is
  **forbidden** there (`tools.yaml:448`). Remove `read` → **no file-read capability in degraded/locked
  states.**
- **Path governance:** `read` enforces per-path `allowed_paths`/`forbidden_paths`; `bash` uses
  command-prefix auto-approve, not per-path. `read` is the path-governed file door.
- **Context discipline:** `read` head-caps + ranges (FRE-410); `bash cat` is a 50 KiB firehose.

The real issue the investigation exposed is **the model bypasses the governed/disciplined `read` door for
the ungoverned bash `cat`/`grep` firehose.** That is a tooling/governance follow-up (route or discipline
model file-reads), **not** a reason to delete `read`. → suggested ticket below.

### Forward stream
**FRE-476 (HYBRID/DECOMPOSE routing + unpin `TOOL_USE` complexity=SIMPLE) is the next stream** — it
attacks the cost at the structural source (one giant 23-round turn → decomposed sub-agent work) rather
than papering over the re-billed tail. FRE-478 (artifact output-cap) remains orthogonal/queued.

---

## Suggested follow-up tickets (Needs Approval; master/owner to file)
1. **Govern/discipline model file-reads** — the model reads source via ungoverned bash `cat`/`grep`
   instead of the path-governed, head-capped `read` tool. Decide: strengthen `read`'s pull, port `read`'s
   per-path governance + head-cap onto the bash file-read path, or accept bash as primary with discipline
   added. (Surfaced by FRE-486.)
2. **(Deferred) Remove dormant compression infra** — trigger only if FRE-476 obviates the cost problem and
   no large-non-file-output use case for the digest materializes.

## Related
- ADR-0085 (parked by this decision) · `docs/research/2026-06-04-artifact-turn-cost-latency-forensics.md`
- FRE-475 (parent, parked) · FRE-476 (forward) · FRE-478 (orthogonal) · FRE-482/483/485 (ADR-0085
  enhancements, blocked-by-486 → park with the ADR)
- Code: `orchestrator/tool_result_digest.py`, `orchestrator/skills.py:35`, `tools/primitives/read.py`,
  `tools/primitives/bash.py`, `config/governance/tools.yaml`
