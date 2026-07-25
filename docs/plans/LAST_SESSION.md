# Last session — 2026-07-25 (the bug wave: built, deployed, mid-verification)

## Doing / discussing (≤5 sentences)

The forcing-function budget turn (re-fired at session start) drove an entire **bug wave to completion**:
gated + merged **7 fixes** and shipped them in **one bundled gateway deploy** at ~06:35 UTC 2026-07-25
(migration 0022 → `ENV=cloud make rebuild SERVICE=seshat-gateway`, main `cc019fde`), verified healthy.
**Live verification is INCOMPLETE and is the top pickup:** FRE-974 (OVH cost) and FRE-973 (salvage) are
**confirmed live + closed Done**; FRE-970/971/972/969 are deployed + TDD-proven but their behavioural
ACs are **not yet live-confirmed** because every qwen budget turn 524s on the Cloudflare tunnel (~250s
cap) before completing — a **sonnet-primary** turn is needed to seal them. Also filed a whole
**pipeline-hardening family** (FRE-975/976/977) + three follow-ups (FRE-978/979/980) this session.

## Commits — the story behind the last ~10

- **PRs #657–#664** = the 7-ticket wave, gated + merged in order: FRE-965 (defaults_by_primary substrate,
  the parked config-chain step 1) · FRE-970 (ES skills cost-event fix) · FRE-969 (legacy digest tolerant
  read) · FRE-974 (OVH/Voyage cost metering **+ Postgres migration 0022** widening `api_costs.cost_usd`
  to DECIMAL(18,12)) · FRE-973 (524 wall-clock guard + salvage) · FRE-971 (Anthropic-primary prefill 400)
  · FRE-972 (compaction/consent gate resolves the session's model window — the "89% of 96K" popup).
- **`#660`** (chore, not a FRE ticket) = **budget cap right-size** — the three stale FRE-334 temp-bumps
  (main_inference daily $15→$10, weekly $40→$25, `_total` $50→$30) + captains_log $2.50→$5 (it was
  *breaching*), based on `budget_counter_snapshot` observed peaks. `.env` `AGENT_CLOUD_WEEKLY_BUDGET_USD`
  → $30 (live, gitignored). Owner-approved.
- **Two outside factors the messages don't carry:** (1) the **dispatch daemon wedged for hours** on
  build1/FRE-965 — it stall-looped because FRE-965 went Done-directly + label-removed (parked-chain edge)
  during a **GitHub PR-service major outage** (~16 min, blocked all PR creation/merge); I cleared
  `dispatch_state.json` manually and it recovered. (2) **FRE-975 fired three times live** — the watcher
  gates master on PR-open+CI-green *before* the build finishes its own Step-8 review.

## Worktrees — anything special

- **build1 / build2** — idle (wave complete; nothing Approved+labelled remains on either).
- **explore** — **the owner manually started a pipeline-architecture study** (do NOT re-dispatch or
  `/clear` it — the study is running). Master tried to send-keys it and failed (needs owner `/clear` +
  `/prime-explore`; that's FRE-977). Its output may reshape FRE-975/976/977 into a unified design.

## Plan position + drift

The **bug wave (MASTER_PLAN §0c) is COMPLETE and deployed** — §0c's FRE-958/963/964 all shipped; the
primary/sub pairing design is settled and the ADR-0121 Addendum merged. The **config chain FRE-966→967→
968 is parked** (labels off, relations wired, resumes on owner's word). One notable convergence:
**FRE-974 delivered ADR-0120's T0** (OVH/Voyage cost into `api_costs`) ahead of that ADR being accepted.
No unexpected drift — the wave was the plan.

## Answers for the fresh start

- **TOP PICKUP — finish wave verification + close 4 tickets.** Deploy is live/healthy (health green,
  migration applied, joinability green, embeddings re-stopped). Still **Awaiting Deploy pending
  behavioural confirmation:** FRE-970 (agent hits `model_call_completed` on a *completing* budget turn),
  FRE-971 (a **sonnet-primary** tool turn completes, no prefill-400), FRE-972 (sonnet near ~150–200K, no
  premature popup), FRE-969 (the two legacy sessions `ca8d0fa3` / `e9674e4d` render). Fire a **sonnet**
  budget turn to seal 970/971/972 at once, spot-check 969, then close them Done with evidence.
- **Qwen turns still 524 on heavy work** — the CF tunnel caps a single generation at ~250s; FRE-973
  salvages it gracefully but it can't complete. Root cause is **32K `thinking_budget_tokens` + 72K
  assembled context** on local qwen-35B. Two levers filed/discussed: **FRE-980** (raise the CF tunnel
  timeout to ≥900s — **a MAC-SIDE change in `infrastructure/terraform-cloudflare-mac`, applied via
  `terraform apply` on the Mac; NOT a VPS/build-pipeline task** — see FRE-980's pinned comment) and a
  **thinking-budget A/B eval** (not filed — owner deciding vs. just routing heavy work to sonnet).
- **Pipeline-hardening family (all Needs Approval, parked):** FRE-975 (review-complete gate signal),
  FRE-976 (Linear-reconciled dispatch — the daemon-wedge fix, Tier-1), FRE-977 (explore first-class
  dispatch). **Hold them unapproved until explore's study lands** — it may unify them. **Open owner
  decision:** dispatch 975+977 to build2 now (hold 976 for explore)? — unresolved at reset.
- **Follow-ups filed this session (Needs Approval):** FRE-978 (Stage-7's *separate* static-window trim —
  the actually-live truncation, sibling of 972), FRE-979 (ES skill must use `api_cost_recorded` for total
  spend so OVH/Voyage aren't dropped — sibling of 970/974), FRE-980 (CF tunnel timeout).
- **Stale Awaiting-Deploy trio** (FRE-943 config-endpoint window · 739 · 717) **rode the wave rebuild →
  now deployed**; verify their ACs + close next session.
- **Standing correction saved to memory:** master **owns** explore dispatch (don't hand it back to the
  owner); it needs the full `/clear`+`/prime`+inject sequence, never a task pushed onto a live context.
