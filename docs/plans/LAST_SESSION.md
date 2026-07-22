# Last session — 2026-07-22 (two config-mgmt ADRs delivered live; compaction + session-summary threads opened)

## Doing / discussing (≤5 sentences)

The big shipment: **ADR-0122 and ADR-0121 were both delivered and closed** — AC-7 (artifact-builder
card) and AC-9 (model picker replaces Path) each proven **live on the owner's phone**, and their whole
ticket trees (FRE-878/887 umbrellas + T1–T6 / T1–T5) flipped to Done with evidence. Along the way the
provider-ceiling caps bug (#616) was corrected and the **open cache question from the prior session was
settled by measurement** (see below). Two new threads opened from live findings: **session-summary is
degraded** (runs every turn on Sonnet, wholesale, off both KG paths — research note #621, explore
working it) and **compaction is degraded** (verified against ES, filed FRE-941/942, owner-approved and
fast-tracked to build2). The through-line worth carrying: **master mis-read telemetry several times
again and the owner caught each** — the fix every time was to query the authoritative source (config,
ES all-time, code) instead of inferring from adjacent signals.

## Commits — the story behind the last ~10

- **#617 (FRE-938)** — session-continuity server fallback. Bounced first (no handoff), build1
  re-presented with a full Step 9 handoff, then merged. **Deploy is ask-first (gateway + PWA) and NOT
  done — top pickup for next session.**
- **#621 / #622** — cc-explore research notes (session-summary KG opportunity; constructed-context +
  fact-verifier). Docs-only, merged. Deliberation deliverables, not builds.
- **#618 / #619 / #620 / #623** — MASTER_PLAN + ADR-0121/0122 status→Implemented doc-drift close-outs.
- **#613 + #616 deployed earlier today** (gateway rebuild, owner "Go"): #613 = FRE-928 reconnect-survival,
  #616 = provider-ceiling caps (`claude_sonnet` 32768→128000, `claude_haiku` 4096→64000). Both verified
  in-container, then AC-7/AC-9 proven live.

## Worktrees — anything special

- **build2 (cc-2build)** — **now the active seat**: FRE-941 (Urgent) building, FRE-942 (High, blockedBy
  941) queued. These are the compaction bugs. Its worktree branch may still show an old `fre-931`
  anchor — the dispatch daemon repoints it.
- **build1 (cc-1build)** — head is FRE-926 (Approved). Holds the merged `fre-938` branch (that's why
  #617's local-branch-delete warned; benign, don't touch it).
- **cc-explore** — was **wedged mid-session (RC unreachable); rescued via `cc-sessions restart cc-explore`**
  which restored Remote Control. It is actively working the **session-summary review** the owner drove
  over RC. Left primed + seeded (`telemetry/explore_task_session_summary_2026-07-22.md`).

## Plan position + drift

MASTER_PLAN was updated live and again at this reset (compaction repair is the new active §0). The two
delivered ADRs are fully closed — no seam left open. **One genuine open operational item: FRE-938's
gateway+PWA deploy (ask-first, owner-gated, not done).** Everything else this session terminated cleanly.

**Findings that live here (durable pointers, but the reasoning is worth carrying):**

1. **The cache question is answered — caching IS preserved on artifact-build turns.** Under
   `cache_frozen_layout_enabled=True` (default, and set in the container), the per-turn planning note /
   volatile block rides the **current user turn**, not the system head (`executor.py` frozen branch),
   so it does NOT enter the cached prefix. Measured live on trace 44dc9b90: the note fired
   (`artifact_builder_planning_note` in `prompt_component_ids`) while `static_prefix_hash` held at the
   baseline **`e6ddc4b50c52f2be`** across the whole primary loop, cache reads climbing 26K→55K — stable
   prefix AND real reuse (not the ADR-0081 stable-hash-zero-reuse trap). The prior session's untested
   claim is now confirmed true.
2. **The two "primary" callsites are different things** (telemetry read that tripped master twice):
   `orchestrator.primary` = the user's conversation loop (this reflects the model picker selection);
   `role.primary` = other code paths tagging role=primary. The per-turn `role.primary` calls on
   gpt-5.4-mini / claude-sonnet-5 are **background second-brain tasks** — gpt-5.4-mini = entity (ER)
   extraction (`entity_extraction` role), sonnet-5 = session-summary/reflection (`captains_log` role).
   They run on their own bindings regardless of the user's pick. The **model binding is correct** — the
   session-summary *cadence* (every turn, wholesale) is the bug, not the model.

## Answers for the fresh start

- **Is FRE-938 deployed?** No. Merged (#617), Awaiting Deploy. Gateway + PWA rebuild, **ask-first**,
  owner hasn't approved. The handoff comment on FRE-938 has the full runbook + cache-name bump (v35).
  This is the first operational thing to raise.
- **What is build2 doing?** FRE-941 then FRE-942 (compaction bugs, fast-tracked). PRs will come to the
  gate. Sequenced deliberately (same files) — do not parallelize them.
- **Did ADR-0121/0122 really close?** Yes — AC-9 and AC-7 both proven live on the owner's phone
  (sessions 0a68ec3b and bc03cb62), all children Done, ADR status headers = Implemented. Don't re-open.
- **Why does session-summary keep coming up?** It's degraded (every-turn Sonnet re-summary, off both KG
  paths, inverts ADR-0024). Research note is `docs/research/2026-07-22-session-summary-kg-opportunity.md`;
  explore is working the design; the owner's next gate is the §C measure-first diagnostic BEFORE any
  build. No ticket yet (decision-first).
- **The caps memory (`reference_catalog_max_tokens_are_policy...`) — is it still right?** No — updated
  this reset. The values are now the real provider ceilings; `settings.artifact_draft_max_tokens`
  (32768) is the single clamp.

## The thing worth carrying forward

Same shape as the prior session: **master reported inferred telemetry reads as established fact and the
owner corrected each** — the role.primary confusion, the "ER extraction is haiku" flip (it's
gpt-5.4-mini per config), the session-summary "model drift" (the model was correct). Every correction
came from the owner, not self-review; every recovery came from going to the **authoritative source**
(config/model_roles.yaml, ES all-time counts, the actual code) instead of reasoning from event
adjacency. When a number or attribution matters, query the source — don't narrate the mechanism that
fits.
