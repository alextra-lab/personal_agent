# Last session — 2026-07-12

## Doing / discussing  (≤5 sentences)
Took cc-explore's memory-ADR drift audit and drove it **end-to-end**: live-verified the finding (0/7992 entities classed; ADR-0104 mislabeled "Implemented"; recall structural arm coded-but-dark), corrected the MASTER_PLAN drift, then designed + shipped **ADR-0115** (consolidated knowledge-class axis) — Accepted → built the whole chain (FRE-863 emission → 864 persistence + 728 dispatch → 865 backfill → 868 eviction) → **deployed + proven LIVE** on a sanctioned owner turn (5 World entities classed, 4 System findings → `sysgraph.stat`, 0 leaked). Also gated+merged FRE-860 (session retention, deployed) and FRE-869 (a live cost-attribution bug caught in review), fixed the build-seat permission-stall class (PR #492 allowlist + FRE-867 for the robust watcher-surfacing half), and anchored FRE-852 (event-driven-dispatch ADR) into cc-adrs. Ended at a **clean stream pause** — build1/build2/adr all dry. The only open items are 3 owner-gated ops decisions (below).

## Commits — the story behind the last ~12
- **ADR-0115 chain (#490/494/495/496/499)** — 863/864/728 merged + **Done + deployed** (SHA `c51a7486`); 865/868 merged but **Awaiting the prod ops-run** (test-substrate scripts). FRE-864 used `Literal["World","Personal"]` per master's clarification — **Stance is a HAS_STANCE edge, NOT an entity class** (ADR-0115 prose reconciled in #491). FRE-728: codex caught the `create_conversation` key_entities MERGE-leak, code-review caught the relationship-splice — both fixed. FRE-865/868 are `--confirm-prod`-gated, snapshot/rollback-backed.
- **#493 FRE-860 retention** — soft-prune tombstone (hard DELETE blocked by FKs), 180d, first-sweep blast-radius verified **0**. Deployed (migration 0019 as admin role + gateway rebuild).
- **#498 FRE-869 cost bug** — `get_llm_client(role_name=<resolved model key>)` → `budget_role_for` miss → `main_inference` (cap unenforced). Found **3 more** identical sites; routed all through `get_llm_client_for_key`. Merged; **Awaiting gateway deploy**.
- **Docs #485/489/491/497** — MASTER_PLAN drift-correction · ADR-0115 supersession/Stance/Implemented. **#492** — build-seat test-DB permission allowlist.

## Worktrees — anything special
- **Gateway rebuilt this session (SHA `c51a7486`)** — ADR-0115 seam + retention live. The rebuild **revived `cloud-sim-embeddings`** → re-stopped (OVH-managed embedder). Verify the running container if in doubt.
- **Live prod KG now has classed entities** (the test turn's 5 World nodes; class axis live for new traffic). The **existing ~7992 corpus is still `class=None` / System-polluted** until the FRE-865 + FRE-868 prod runs.
- build/build2/adrs idle on old merged fre-branches (normal; reset on next dispatch).

## Plan position + drift
- ADR-0115 was the session spine — **Implemented + live**. MASTER_PLAN class-axis lines updated (#497 + this reset). **Board reconciled this checkpoint:** FRE-867 + FRE-852 were mis-moved to Awaiting-Deploy by the GitHub integration (PR-token/attachment match despite un-tokened titles) — reset to **Needs-Approval** (neither's deliverable shipped: 867's watcher-surfacing unbuilt, 852's ADR unwritten). Removing a token from a PR title AFTER the integration attaches does NOT detach it — reset the ticket state manually.
- Automation LIVE (watcher + dispatcher active).

## Answers for the fresh start
- **Top of queue = 3 owner-gated ops decisions** (all Awaiting-Deploy, none building, none urgent), in dependency order for the **ADR-0114 de-confound**: **(1) FRE-865 backfill prod-run** (classes existing knowledge + writes System markers; cost + prod-writes) → **(2) FRE-868 eviction prod-run** (deletes existing System incl. the **720-mention `Elasticsearch`** node; MUST follow #1; destructive, snapshot-backed — retain the snapshot file) → **(3) FRE-869 gateway deploy** (cost fix; ask-first, independent). Each ticket comment has a runbook; bring a blast-radius preview before any run.
- **Live-verification pattern (reusable):** to prove a memory-write change live without master firing a gateway turn (PII/identity risk), the **owner fires one turn from their client, master verifies the substrate** (Neo4j + `sysgraph.stat`). Used this session — trace `2564b7c5`.
- **FRE-843 (ADR-0114 verdict) still HELD** on **corpus adequacy** (46-episode plateau; cc-explore's inaugural topic, unfired). The 865+868 runs de-noise the corpus but are SEPARATE from the corpus-adequacy question — don't conflate.
- **FRE-866** (recall structural-arm wiring + class predicate, Low) parked-unlabeled. **FRE-867** (watcher permission-surfacing) + **FRE-852** (event-driven dispatch ADR — owner drives `/adr` in cc-adrs) both Needs-Approval.
- Owner reached a deliberate clean pause at context ~74%; this reset was owner-invoked.
