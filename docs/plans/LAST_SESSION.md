# Last session — 2026-07-24 (ADR-0124 Amendment B shipped; a live turn surfaced a bug wave)

## Doing / discussing (≤5 sentences)

Two arcs. **First:** gated and shipped the entire **ADR-0124 Amendment B chain** — #647 (the amendment ADR,
FRE-955), #649 (FRE-956 conversation-only producer), #650 (FRE-948 Phase-1 digest surface) — all merged,
deployed, verified, Done; the summariser is now conversation-only and the session browser renders
labels+digests. **Second:** the owner fired one real qwen35 turn (a 7-day budget analysis) that hung with
no UI activity, and investigating it cascaded into **five genuine bugs the test suite never caught** —
sub-agent routing, a misleading tool-error signal, UI streaming silence, silently-degraded recall, and a
config-design gap in how primary/sub models pair. All are ticketed and moving; dev is halted on that
thread (owner's call) until they're fixed.

## The thing worth carrying forward (read first)

**The primary/sub model-pairing design is SETTLED — do not re-litigate it (it took a long, correction-heavy
clarification).** The rule: `sub_agent` has a **per-primary default** set by hand as an explicit map
(qwen-thinking → qwen-instruct; sonnet → sonnet; a new primary must have its default intentionally set — no
auto-derivation). On top of that it's **open** — overridable to any model **by name**, via the **Config UI**
(the ADR-0121 capstone). The axis is **model selection by name, NOT location** — the owner explicitly accepts
the latency of a cloud/local mismatch in exchange for free pairing. This is [[project_primary_sub_companion_default]]
in memory and the subject of **FRE-964** (ADR-0121 amendment). Second carry-forward: **the forcing function
works** — one owner-fired turn found more real defects than the whole session's PR-gating did.

## Commits — the story behind the last ~10

- **#646 → #650** — the ADR-0124 Amendment B chain: design doc (#646), the amendment ADR (#647, FRE-955),
  the conversation-only producer (#649, FRE-956), the Phase-1 session-browser digest surface (#650, FRE-948).
  All shipped + deployed today. §0b of MASTER_PLAN tracks the workstream (now paused before Phase 2).
- **#648, #651** — MASTER_PLAN checkpoints; #648 also first gitignored `.remember/` (see below).
- **#652** — FRE-958, the sub-agent routing fix (one-line: enforced-expansion built a PRIMARY-role client
  to serve SUB_AGENT calls). **Merged, deploy deliberately HELD** — see Answers.
- Noise: recurring `auto: .remember` commits — the `remember` plugin auto-commits to main; the files were
  still *tracked* so the gitignore didn't take. This checkpoint PR `git rm --cached`s them for good.

## Worktrees — anything special

- **build1** — building **FRE-963** (the sub_agent re-bind stopgap). Its PR is the next gate.
- **adrs** — building **FRE-957** (ADR-0123 acceptance + impl-chain filing). FRE-964 is queued behind it on
  the adr stream.
- build2 idle.

## Plan position + drift

MASTER_PLAN §0b (ADR-0124 Phases 0–1 live, paused before Phase 2) is current. This checkpoint ADDS the new
**bug/config-design wave** (a new §) so the plan isn't behind Linear. No other drift.

## Answers for the fresh start

- **TOP PICKUP — the bundled deploy.** **FRE-958** (routing fix) is merged + **Awaiting Deploy, HELD ON
  PURPOSE** to ship in ONE gateway rebuild with **FRE-963** (the sub_agent → qwen3.6-35b-instruct re-bind,
  building on build1). When FRE-963's PR lands: gate → merge → **one `ENV=cloud make rebuild SERVICE=seshat-gateway`
  (ask-first)** deploys both, re-stop embeddings, verify. This restores sub-agent delegation AND the sane
  local-companion default.
- **In the adrs seat:** FRE-957 (ADR-0123 turn-progress surface — inference visibility + **event-stream
  replay on reconnect**; the mid-turn WS drop is why even tool activity went invisible). It will file impl
  tickets for owner approval. FRE-964 (the ADR-0121 primary/sub default-map amendment) queued behind it.
- **Needs Approval (owner greenlight to build):** FRE-959 (benign SIGPIPE reported to the model as a tool
  failure) · FRE-960 (query-paraphrasing has the same routing bug, fails-open → recall silently degraded to
  single-query since FRE-920; **re-scoped** — a routing bug, not an egress decision).
- **The qwen thread is killed** (I restarted the gateway to stop it; owner restarted the SLM server). Owner
  will re-fire it after the bugs deploy. Do NOT re-fire it unprompted.
- **Done this session:** FRE-942 (corrected to Done), FRE-955/956/948 (ADR-0124 chain, deployed). Two tools
  added: `claude-security` + `ast-grep` (CLI + skill).
