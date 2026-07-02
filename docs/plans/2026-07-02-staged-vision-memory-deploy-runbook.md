# Staged Gateway Deploy Runbook — Vision + Memory (2026-07-02)

**Author:** master · **Trigger:** FRE-666 merged (vision chain code-complete on the agent side)
**Class:** `seshat-gateway` rebuild = **ASK-class** (needs explicit owner OK; not standing).
**Deployed SHA today:** unknown-behind (deploy-hold in force since the chains began landing);
target = current `main` (`db23436` at draft time).

---

## What a `seshat-gateway` rebuild activates (everything on main)

### 1. Vision (the payoff) — LOCAL only in practice
- FRE-661 carrier · FRE-664 content-widening · FRE-665 capability routing · FRE-709 stringify hardening · **FRE-666 image resolution** (bytes → image block at turn assembly).
- Effect: an image attached to a turn is fetched (credentialed R2), guardrail-capped, and passed to a **vision-capable model** as a real image block. Default profile is **local Qwen** (vision-capable) — **no cloud cost incurred**.
- **Cloud vision is inert in practice:** it only fires on an explicit per-attachment `processing_target="cloud"`, and the PWA control that sets it (**FRE-692**) is **not built** — so today every image routes local. The missing cloud **cost control (FRE-691)** therefore does not bite yet, *but it MUST land before FRE-692 ships the cloud-override UI.*

### 2. Memory (the risk) — Claims write-path goes live
- FRE-637 extraction contract · FRE-638/712 living Claims + retire-FWW · **FRE-711 World-fact living descriptions**.
- Effect: consolidation begins writing the new Claims/description-gate write-path to Neo4j.
- **FRE-711 correction is DORMANT:** every production caller writes confidence 0.8 (= baseline), so the strict-`>` correction gate never fires — only *empty-fill* is live. Real correction needs **FRE-725** (Needs Approval). Shipping now = mechanism + FRE-375-migration + empty-fill; corrections start once FRE-725 lands.

### 3. Flag-dark (inert) — no behavior change
- FRE-707 structural recall arm · FRE-722 RRF fusion — both behind flags defaulting **off** (activate only when FRE-724 flips them). Land as dead code.

---

## NOT in this deploy (deferred — ADR-0101 control spine unbuilt)
- **FRE-691** cloud image cost/pre-flight/reservation/metering — Approved, unbuilt. (Not needed while vision is local-only.)
- **FRE-692** PWA per-attachment cloud/local override — Approved, unbuilt. (Its absence is what keeps vision local-only.)
- **FRE-693** joinability threading on image byte-fetch/cost/resolution events — Approved, unbuilt.
- **FRE-669** end-to-end live smoke (agent sees uploaded image) — Approved, **master-run**; this is the post-deploy verification below.

---

## Pre-deploy
1. `main` green; confirm target SHA. All chain PRs merged (661/664/665/709/666 · 637/638/712/711 · 707/722).
2. **Resolve FRE-725 decision** (owner): ship memory as mechanism+empty-fill (corrections deferred), or pull FRE-725 first.
3. **Neo4j snapshot for rollback** (the memory Claims write-path is the highest-risk change).

## Deploy
- `ENV=cloud make rebuild SERVICE=seshat-gateway` (VPS; `make deploy` is Mac-only).

## Staged post-deploy verification (do NOT claim done on "exited 0")
1. **Health:** `curl -s http://localhost:9001/health` → all components connected.
2. **Memory path (highest risk):**
   - `scripts/monitors/joinability_probe.py` against prod — no new orphans.
   - Trigger/await one consolidation over a turn re-mentioning an existing World entity → inspect the first Claims written; confirm existing facts intact (no corruption); confirm empty-fill works and no eval-write clobber (FRE-375 holds).
3. **Vision path (FRE-669 seam, master-run):** upload an image via PWA → confirm the agent's reply is conditioned on the image (it "sees" it); confirm an oversized image is rejected/downscaled with the disclosure in the reply. This closes FRE-669.
4. **Flag-dark inert:** confirm `structural_arm_enabled=false` and multipath off — recall behavior unchanged.

## Rollback
- Redeploy the prior gateway SHA. If the Claims write-path corrupted data, restore the Neo4j snapshot. Vision is additive (only fires on image turns) — low rollback risk.

---

## Risk summary
- **Memory Claims write-path** = the real risk (new write behavior on the hot consolidation path). Snapshot + probe + inspect-first-Claims mitigate.
- **Vision** = additive, local-only, low risk; end-to-end unproven until FRE-669 (run as step 3).
- **Cloud vision cost** = a *latent* risk gated behind unbuilt FRE-692; must build FRE-691 before FRE-692.
