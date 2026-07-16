# Last session — 2026-07-15 → 16 (very long; two /master gates + cost-gov design)

## ▶ BEGIN HERE (owner's explicit go-forward, 2026-07-16) — resume in this order
1. **FIRST ACTION after prime: route the cost-governance note to the adr seat.** `send-keys` cc-adrs a brief: *author a new ADR superseding ADR-0065*, design input = `docs/research/2026-07-16-cost-governance-rework-adr-0065.md` (on main). Carry the **§7 open questions** + explore's flag: **§6.5 Section-A granularity (per-vendor thresholds + per-role visibility) is a standing rec the owner never ruled on — surface it for the owner to confirm at ADR**, don't smuggle it. The ADR is authored *with the owner* in the adr seat (owner-hubbed); master routes, owner drives.
2. **Then converge / revise as needed, and BUILD the config-UI (ADR-0119)** — with the cost-governance project **waiting in the wings** (co-design the shared seams: T5 splits artifact_builder+vision out of main_inference; T6 renders the cost surface *inside* the ADR-0119 config UI). Config-UI prerequisites to approve+dispatch: **FRE-895 (scrub) first**, then **FRE-879 → 880 → 888 → 892**.
3. **Then: attack the Seshat inference backlog.**

## Doing / discussing (≤5 sentences)
Shipped **FRE-893 config-audit redo** (PR #548 — now reads the deployed `/opt/seshat/.env` keys-only as an override source; master independently verified reads-env + no-value-leak; the first attempt's false-positive class is gone). Merged the **ADR-0119 ExecutionProfile amendment** (PR #550) — resolves the FRE-879 regression (open roles resolve via ExecutionProfile, matrix only pinned writers; AC-8/9/10 guards; vision stays pinned) → **config-UI step-0 UNBLOCKED**. Filed **FRE-895** (scrub `frenchforet.com` from the PUBLIC repo — HEAD+guard, Needs Approval). Committed **explore2+owner's cost-governance rework note** (supersedes ADR-0065; PR #552, renamed). **Stopped the dispatch orchestrator** (owner-requested, temporary) — then the owner set the sequence above and asked for this reset.

## Commits — story behind the last ~10
#548 FRE-893 config-audit redo (reads deployed .env; **Done** w/ evidence) · #549/#551 MASTER_PLAN bumps · **#550 ADR-0119 amendment** (ExecutionProfile fix — the unblock) · #552 cost-gov note (renamed from `provider-pool-deliberation` → `cost-governance-rework-adr-0065`). Earlier: #547 reset checkpoint. All docs PRs used the auto-merge flow.

## Worktrees — anything special
- **cc-build** on `fre-879-artifact-builder-role-cost-lane` — **PAUSED, working impl UNCOMMITTED (do NOT discard).** The amendment settled the seam: `artifact_builder` now resolves via the **ExecutionProfile** (a binding in `config/profiles/{local,cloud}.yaml`, via `resolve_model_key`), **not** the matrix. The cost-lane/telemetry/registry work is reusable — **only the resolution seam changes**. Resumes when FRE-879 re-approved.
- **cc-build2** — **IDLE** (FRE-893 done). Ready for FRE-895 then the config-UI chain.
- **cc-adrs** — **free** (just delivered the ADR-0119 amendment). Its NEXT is the cost-gov ADR (go-forward #1).
- **cc-explore** — multi-parent deliberation (owner's).
- **cc-explore2 (EPHEMERAL)** — cost-gov deliberation **DONE**; note committed to main (renamed). **Tear down when the owner's finished** (`tmux kill-session -t cc-explore2` + `git worktree remove .claude/worktrees/explore2`).

## ⚠ Actuation posture (READ before prime-master step 7b)
**`seshat-dispatch-orchestrator.service` is INTENTIONALLY STOPPED** (owner-requested, temporary, 2026-07-16). `seshat-gating-watcher.service` is **still active** (PR-gating live; `/master <PR#>` works). This is **NOT a silent failure** — do not alarm. **Resume:** `sudo systemctl start seshat-dispatch-orchestrator.service`. Consequence: build tickets won't auto-dispatch while stopped — when starting the config-UI build, either resume the orchestrator or launch FRE-895/879 manually.

## Plan position + drift
- **Config-UI epic (ADR-0119) UNBLOCKED** (amendment landed). Not started. **FRE-895 (frenchforet scrub) + FRE-879/880 are pending owner approval** — recommend approving all three and running **FRE-895 first** (self-contained, clears an active public-repo policy gap, generous margin before ADR-0119 sign-off; near-zero file overlap with 879).
- **Cost-governance rework (supersedes ADR-0065):** note on main; **ADR authoring is the next design step** (route first). **T0** (instrument OVH/Voyage/Perplexity into `api_costs` — three metered vendors off the books today) is the foundation-first first-buildable; **NOT filed yet** (waits for the ADR / owner go). Everything cost/budget = **ask-first + standing "ask before budget changes."**
- **Awaiting Deploy holds (unchanged):** FRE-884 (batched), 739/866/717.
- **Follow-up filed:** FRE-885 (cost-estimator token-counter document-type gap, Needs Approval).
- **Owner corrections → memory this session:** frenchforet public-repo exposure (repo is PUBLIC; 101 tracked files + 113 commits; no secrets — identifiers only) → [[feedback_no_identifiers_in_public_repo]] updated; cost-gov direction → [[project_cost_governance_rework]].

## Answers for the fresh start
- **Very first action?** Route the cost-gov note to adr (go-forward #1). Don't re-litigate the design — §6 decisions are settled owner calls; adr formalizes + surfaces §7 open questions.
- **Is the config-UI building?** No — unblocked, not started. Approve FRE-895 → FRE-879/880; the dispatcher is stopped, so resume it or launch manually.
- **Is the stopped dispatcher a problem?** No — owner-requested; resume command above.
- **cc-explore2** is disposable — its note is durable on main; tear the seat down when the owner's done with cost-gov.
- **File T0 now?** Owner didn't take it up — it waits for the ADR; note it as the cost-gov first-buildable.
