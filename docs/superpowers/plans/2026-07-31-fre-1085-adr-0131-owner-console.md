# FRE-1085 — ADR-0131 T1: the atomic migration PR

**Ticket:** [FRE-1085](https://linear.app/frenchforest/issue/FRE-1085) (Approved, Urgent, `stream:build2`, Tier-1:Opus)
**ADR:** [ADR-0131](../../architecture_decisions/ADR-0131-retire-master-plan-owner-console.md) — D1/D2/D3/D5
**Risk tier:** Standard (new-ADR implementation, multi-file contract change, one coupled test) → codex plan-review required
**Chain:** FRE-1085 (this) → FRE-1086 (integration disable) → FRE-1087 (seam, due 2026-08-17)

---

## 1. What this ticket delivers

One atomic PR carrying four things that must land together (ADR-0131 Implementation Notes §3 —
atomicity removes both the "skill mandates writing a deleted file" window and the "two contradictory
contracts coexist" window):

1. `docs/plans/OWNER_CONSOLE.md` — new; D2 contract header, numeric size bound, owner-seeded standing
   directives, six-row trust ladder (D3).
2. The **owner-adjudicated disposition table** (§3 below) posted on FRE-1085, and the ticket comments /
   `Backlog` filings it prescribes, executed.
3. `docs/plans/MASTER_PLAN.md` — deleted.
4. Every D5 contract-document edit, plus the fold-ins in §5 that would otherwise dangle at a deleted path.

## 2. Blocking owner input (AC-c, D2, D3)

Three things are **owner data by construction** and cannot be authored by this session:

| # | What | Why it's the owner's |
|---|---|---|
| O1 | Adjudication of every **directive-shaped** row in §3 — keep (their instruction → console) or release (master's analysis → its ticket) | ADR-0131 Impl. Notes §2: the file carries no per-line attribution, so directive-shaped lines cannot be mechanically attributed |
| O2 | The **standing directives** seeded into the console, in the owner's voice | D2: "the owner writes; master transcribes." A build session never authors console content |
| O3 | The **level** on each of the six ladder rows | D3: "Initial rows are seeded by the owner at migration — the ADR establishes the mechanism and deliberately does not decide current levels" |

§4 carries *proposals* for O3 derived from grants already documented in the repo, so the owner
confirms rather than composes. **The PR does not merge until the owner's approval comment is on the
ticket, dated before the merge** (AC-c) — that comment is the deletion gate.

## 3. Disposition table — every block of MASTER_PLAN

**Coverage is mechanical and complete.** `docs/plans/MASTER_PLAN.md` is **340 lines**. A block map of
maximal non-blank runs yields **58 blocks**; the remaining **56 lines are blank separators** and carry
no content to dispose of. Every one of the 58 blocks has exactly one row below — no gaps, no overlaps.
*(Line ranges were re-derived directly from the file after a first draft carried a one-line offset in
three places; treat the ranges here as authoritative.)*

Classes: **D** derived state (authoritative in the resolver / Linear / git / health probe) · **A**
master's analysis (home = a ticket or a research doc) · **O** owner voice (home = the console) ·
**M** meta/structural (the file's own scaffolding). "Directive-shaped?" flags the `do not …` /
sequencing class that **O1** adjudicates.

| Src lines | Block | Class | Directive-shaped? | Destination | Retirement condition (console rows only) |
|---|---|---|---|---|---|
| 1 | `# Master Plan — Personal Agent` title | M | no | **delete** with the file | — |
| 3–7 | "Forward plans only" contract + pointer list + Last-updated | M | no | **delete** — the contract dies with its object | — |
| 9 | `## 0. In flight` heading | M | no | **delete** | — |
| 11–17 | build1 / build2 / adrs / explore seat status | D | no | **delete** — resolver busy-guard + `git worktree list` + Linear | — |
| 19–24 | Owner actions blocking: ADR-0123 AC-7 needs three owner turns (19–22); FRE-989 F9 needs a *cloud* primary (23–24) | A | no | comments → ADR-0123 seam ticket; FRE-989 | — |
| 26–31 | Master's queue: FRE-927 signal is the reason split not the alert (26–29); watch `main_inference` caps · **"do not raise the cap"** (30–31) | A + O? | **yes** (30–31) | 26–29 → comment on FRE-927; the `do not raise the cap` → **O1** | when the caps decision is taken |
| 33 | `## 0b. Recall` heading | M | no | **delete** | — |
| 35–37 | Entity path fixed (FRE-1041/1060/1061/1062) · **"Do not re-open the mechanism"** | O? | **yes** | **O1** — console directive, or delete (the four tickets carry the history) | when a measurement supersedes it |
| 39–47 | Open + unmeasured: header (39); "is the entity payload load-bearing?" (40–42); FRE-1053 re-scope small (43–44); FRE-1021 census re-run `--since` the 07-30 deploys (45–47) | A | no | 40–42 → **new `Backlog` ticket**; 43–44 → comment on FRE-1053; 45–47 → comment on FRE-1021 | — |
| 49 | `## 1. Elasticsearch structure` heading | M | no | **delete** | — |
| 51–56 | ADR-0128-vs-0129 pending decision · **"do not flip 0128 to Superseded on the merge alone"** | A + O? | **yes** | analysis → comment on the ADR-0129 ticket; the `do not` → **O1** | when the 0128-vs-0129 decision is taken |
| 58–62 | FRE-1036 unblocked either way; its state was a lie until 07-30 | A | no | comment → FRE-1036 | — |
| 64–69 | Shard math: 602 shards / 564 indices, ~8/day, ≈50 days headroom | A | no | comment → FRE-1036 (the measurement, with its date) | — |
| 71–77 | Correction to own record: FRE-1066 is **not** a shard lever | A | no | comment → FRE-1066 | — |
| 79–83 | FRE-1036 scope boundary — `slm_server` client-side index names · **"Do not block FRE-1036 on it"** | A + O? | **yes** | analysis → comment on FRE-1036; the `do not` → **O1** | when FRE-1036 merges |
| 85–87 | Six timestamp spellings, not four | A | no | comment → FRE-1036 | — |
| 89–93 | ADR-0128's chain + sequence · **"Do not approve it before the 0128-vs-0129 decision"** | A + O? | **yes** | sequence → comment on the ADR-0128 ticket; the `do not` → **O1** | when the decision is taken |
| 95–100 | ADR-0129's chain; FRE-1064 as a falsification gate | A | no | comment → FRE-1064 (the head) | — |
| 102–107 | FRE-1066 and FRE-1068 are independent of both ADRs | A | no | comments → FRE-1066, FRE-1068 | — |
| 109–110 | FRE-1035 — ES field-resolution technique | A | no | comment → FRE-1035 | — |
| 112 | `## 2. Awaiting an owner decision` heading | M | no | **delete** | — |
| 114–117 | **Accept ADR-0129, or hold ADR-0128?** — the largest open call | **O** | **yes** | **O1** — an open owner decision → console directive | when the decision is taken |
| 119–131 | FRE-1039 Grafana-over-Postgres, "do not decide the two separately" (119–123); ADR-0127's seven tickets (124); FRE-1013 premise measurably false (125–127); the ten-ticket awaiting-decision list (128–129); ADR-0120 (130); **"Backlog cull scope + gate"** (131) | O + D + A | **yes** (119–123, 131) | 119–123 → **O1** (console directive, coupled to the ADR-0129 call); 124/128–130 → **delete** (Linear `Needs Approval` is authoritative); 125–127 → comment on FRE-1013; 131 → **O1** | 119–123: when ADR-0129 is decided · 131: when the cull scope is set |
| 133 | `## 3. Master's verification backlog` heading | M | no | **delete** | — |
| 135–138 | "Eighteen in Awaiting Deploy" + UNVERIFIABLE-is-first-class | D + process rule | no | **delete** the count (Linear); the UNVERIFIABLE rule already lives in lifecycle-rules § Evidence contract | — |
| 140–144 | Writing this file corrupts the board · **"keep bare `FRE-XXXX` out of docs PR titles, bodies and branch names"** | O? | **yes** | **O1** — recommend console **with** `Retires: FRE-1086 merges`, since FRE-1086 retires it structurally | FRE-1086 merges |
| 146–150 | "Do not read this column as all-deployed"; `reconcile_board.py` cannot catch the class | A | no | comment → FRE-1036 (the specimen) | — |
| 152–154 | **"Verify from the substrate before asking for an owner turn"** (the FRE-970 precedent) | O? | **yes** | **O1** — a standing method rule; console, or lifecycle-rules § Evidence contract as mechanics | — |
| 156–170 | The verification table: header + separator (156–157) and 13 ticket rows (158–170) | D + A | no | **delete** the table (Linear `Awaiting Deploy` is authoritative); each row's *what it awaits* → a comment on that ticket (**13 comments**: FRE-989, 1021, 1037, 1066, 1016, 1018, 739, 998, 717, 986, 936, 972, 943) | — |
| 172–174 | FRE-739 is blocked, not merely unverified; ADR-0107 cannot close | A | no | comment → FRE-739 | — |
| 176–177 | `reconcile_board.py` reports 3 FAIL — FRE-432, FRE-875, FRE-983 | D | no | **delete** — the script recomputes it on demand | — |
| 179 | `## 4. ADR-0126` heading | M | no | **delete** | — |
| 181–184 | ADR-0126 chain state; FRE-1019 is the seam · **"do not re-set the hold"** | D + O? | **yes** | state → **delete** (Linear); the `do not` → **O1** | when ADR-0126 closes |
| 186–190 | FRE-1015 parked, draft PR #738, the tautological-precondition bounce | D | no | **delete** — superseded: FRE-1015 merged 2026-07-31 | — |
| 192–193 | **"Parking = remove the stream label"** — a `blockedBy` does not hold a ticket | process rule | **yes** | **O1**, with the recommendation that this is *mechanics* → **lifecycle-rules § Dispatch**, not the console (D3: skills may describe mechanics) | — |
| 195 | `## 5. ADR-0125` heading | M | no | **delete** | — |
| 197–200 | ADR-0125 residual: FRE-1005 unblocked, FRE-1006 closes the ADR, FRE-1014 docstring | D + A | no | comments → FRE-1006, FRE-1014 | — |
| 202–204 | Owed + unticketed: reasoning-trace feasibility; AST/import-boundary guard | A | no | **two new `Backlog` tickets** | — |
| 206 | `## 6. Cost and summarisation` heading | M | no | **delete** | — |
| 208–209 | The sweep is live and bounded; it bounds a *session*, not aggregate spend | A | no | comment → the ADR-0124 ticket | — |
| 211–214 | Unbounded *input* remains unowned; ADR-0127 D9 assigns the fork to ADR-0124's trigger | A | no | **new `Backlog` ticket** (the trigger fork has no owner) | — |
| 216–218 | "the cost incident was a bug, not an indictment of wholesale regeneration" | A | no | comment → the ADR-0124 ticket | — |
| 220–222 | `CONTEXT_INTELLIGENCE_SPEC.md` cited by no ADR · **"Two layers to reconcile; do not start a third"** | A + O? | **yes** | analysis → **new `Backlog` ticket**; the `do not` → **O1** | when the two layers are reconciled |
| 224–226 | **"Do not re-propagate two false figures"** (27 real ratings, not 1,943; 1,941 turns, not 8,880) | O? | **yes** | **O1** — recommend delete + comment on ADR-0127, which already carries the corrected figures | — |
| 228 | `## 7. Reduce the backlog` heading | M | no | **delete** | — |
| 230–232 | Backlog size (40+ / ~80) + the cull method | D + A | no | **delete** the counts (Linear); the method rides on the 131 cull directive | — |
| 234 | `## 8. Pipeline hardening` heading | M | no | **delete** | — |
| 236–237 | The convergence law — provenance sentence | A | no | **delete** — authoritative home is `docs/research/2026-07-29-why-pipeline-point-fixes-do-not-converge.md` | — |
| 239–240 | The law itself (blockquote) | A | no | **delete** — same authoritative home | — |
| 242–244 | **"Use it as an acceptance test on every proposed fix"**; master's framing was half wrong | O? | **yes** | **O1** — the instruction is directive-shaped; the analysis half → the research doc | — |
| 246–292 | §8 bullet list: FRE-1054 specimen (246–248); FRE-927's two findings (249–255); "the three reconcilers want consolidating" (256–260); FRE-1011 rescope-or-close (261–264); FRE-975 · FRE-977 (265); FRE-867 parked-seat + the three allowlist bypasses (266–284); **"master-authored `src`/security changes have no independent gate — route them through a build seat"** (285–287); four inconsistent readiness oracles + finishing the ADR-0116 migration (288–292) | A + O? | **yes** (285–287) | comments → FRE-1054, FRE-927, FRE-1011, FRE-867; **new `Backlog` tickets** for 256–260 (`SeatHealth`/`SeatHealthPolicy` shape) and 288–292 (oracle consolidation); 265 → **delete** (Linear); 285–287 → **O1** (a standing prohibition on master — strong console candidate) | when a gate for master-authored `src` changes exists |
| 294–297 | "Recommended ADR, two decisions" — D1 decision provenance; D2 transition ownership | A | no | **new `Backlog` ticket** (an ADR request), **noting D2 is already delivered by ADR-0131 D4** — so only D1 survives | — |
| 299 | `## 9. Then, in order` heading | M | no | **delete** | — |
| 301–302 | **Telemetry residuals · Configuration Management · Linear async feedback · Seshat Inference** | **O** | **yes** | **O1** — this is the sequence guidance D2 names as the console's primary content | when those four land |
| 304 | `---` divider | M | no | **delete** | — |
| 306 | `## To fix, unscheduled` heading | M | no | **delete** | — |
| 308–340 | The unscheduled list: ES loses up to 83% of events — FRE-1051 · **"treat any agent-logs count as provisional until this closes"** (308–313); nothing watches a threshold approaching (314–316); the 2 GiB gateway limit is no longer load-bearing (317–318); a local-qwen primary fabricated a spend report · **"Owner's call whether to ticket"** (319–322); personal data in the public repo · **"Owner sets scope"** (323–326); cost-gate estimator runs a third light (327–328); D3's loss question unanswered (329–330); frozen-reset never fires on gateway turns (331); FRE-912 (332); worker seats strand on non-edit prompts (333); duplicate ADR-0067 (334–335); research index unmaintained (336); stale `master-914` worktree (337–338); 49 orphaned capture files · **"Owner's call to remove or ignore"** (339–340) | A + O? | **yes** (313, 319–322, 323–326, 339–340) | comments → FRE-1051, FRE-994 (329–330), FRE-954 (331); **new `Backlog` tickets** → 314–316, 317–318, 327–328, 334–335, 336, 337–338; **delete** → 332, 333 (Linear: FRE-912, FRE-911); **O1** → the `provisional` directive (313), and the three explicit *Owner's call* items (319–322, 323–326, 339–340) | 313: FRE-1051 closes · 323–326: when the redaction scope is set |

**Tally: 58 blocks (all 340 lines accounted for — 284 content, 56 blank) · 21 directive-shaped rows
awaiting O1 · ~28 ticket comments · 13 new `Backlog` tickets · the remainder deleted as derived state
or as content whose authoritative home already holds it.**

### 3a. Recommended adjudication of the 21 directive-shaped rows (O1)

The owner adjudicates each; this is the recommendation, not a menu. **Keep** = the owner's instruction,
to the console. **Release** = master's analysis wearing directive grammar, to its ticket.
**Mechanics** = a process rule, which D3 puts in lifecycle-rules, not the console.

| Verdict | Rows | Rationale |
|---|---|---|
| **KEEP → console** (8) | 301–302 (the sequence) · 114–117 (accept ADR-0129?) · 119–123 (FRE-1039, coupled to it) · 131 (backlog-cull scope) · 323–326 (personal data — "Owner sets scope") · 285–287 (route master-authored `src` changes through a build seat) · 140–144 (keep `FRE-XXXX` out of docs PRs; `Retires: FRE-1086 merges`) · 30–31 (do not raise the `main_inference` cap) | each is either an open decision only the owner can take, or a standing prohibition on master — the class with no other home |
| **RELEASE → its ticket / research doc** (11) | 35–37 · 51–56 · 79–83 · 89–93 · 181–184 · 220–222 · 224–226 · 242–244 · 313 | each is a conclusion master reached about a specific ticket or ADR; the grammar is imperative but the content is analysis, and the authoritative home already exists |
| **MECHANICS → lifecycle-rules** (2) | 192–193 (parking = remove the stream label, § Dispatch) · 152–154 (verify from the substrate before asking for an owner turn, § Evidence contract) | D3: skills and lifecycle-rules may describe *mechanics*; they simply may no longer *grant authority* |
| **OWNER'S EXPLICIT CALL** (2) | 319–322 (fabricated spend report — "Owner's call whether to ticket") · 339–340 (49 orphaned captures — "Owner's call to remove or ignore") | the line itself defers to the owner; default recommendation is a one-line `Backlog` ticket for each |

Kept rows enter the console as **`relayed`** records per AC-4 (`[2026-07-31 · relayed 2026-07-31]`),
naming this migration as their session of origin — which is precisely the set AC-4 has the seam pass
present back to the owner for confirmation.

## 4. `docs/plans/OWNER_CONSOLE.md` — structure, and the ladder levels proposed for O3

Header states the D2 contract (owner writes · master transcribes verbatim, attributed, dated · master
retires only on a met condition, citing it in the commit · master never authors) and the size bound.

**Proposed size bound: 60 lines.** Rationale: six ladder rows + header + a working set of directives
fits inside one screen, and the bound is a contract violation to *surface*, not a compaction chore
(D2), so it needs to be tight enough to bite.

Record schemas are fixed by ADR-0131 AC-4 and reproduced verbatim in the file:

- directive — `- [YYYY-MM-DD · owner | relayed <session-date>] <text> · Retires: <event>`, where
  `<event>` must be objectively decidable (a merge, a date, a deploy, a measured threshold).
- ladder row — class · level · dated grant · promotes-when · demotes-when.

**Six ladder rows (AC-1 manifest), with levels proposed from grants already documented in-repo.**
Every level below is a *proposal for the owner to confirm or change* (O3):

| # | Class | Proposed level | Provenance of the proposal |
|---|---|---|---|
| 1 | deploy / standing-approved (PWA-only rebuild · additive ES template · Kibana dashboard import) | `standing-approved` | lifecycle-rules § Deploy: owner granted standing approval 2026-06-26 |
| 2 | deploy / ask-first (gateway rebuild · ES type-change or reindex · Postgres schema/migration · cost/budget/governance) | `ask-first` | same 2026-06-26 decision — the complement it explicitly carves out |
| 3 | dispatch actuation posture (daemon on/off + mode) | **owner must set** | genuinely undetermined in-repo: the daemon is live, but a cost audit gated dispatch. No date-stamped grant to transcribe |
| 4 | on-merge board transition (→ `Awaiting Deploy`) | `standing-approved` | ADR-0131 D4 assigns it to master inside the advance-dispatch pass it already runs |
| 5 | merge-to-main authority | `standing-approved` | lifecycle-rules § Session boundary: "master alone merges to main"; no per-merge ask exists today |
| 6 | console write authority | `do-and-report` — transcription and mechanical retirement only; **authoring is forbidden at every level** | D2's authorship contract |

Standing directives: seeded from O1's keeps, in the owner's words, each with a retirement condition.

## 5. File-by-file edit plan

### D5 — the contract documents ADR-0131 names

| File | Edit |
|---|---|
| `.claude/skills/prime-master/SKILL.md` | Step 8 re-pointed: target = the dispatch resolver's eligible sets (computed) **+** `OWNER_CONSOLE.md` (directives + this session's exact standing authority). The "~1 screen or strip it" drift rule and its `prepare-reset` hand-off are deleted with their object. Step 7b reads intended posture from **ladder row 3**, not "#2 / MASTER_PLAN / memory". Frontmatter description updated. |
| `.claude/skills/prepare-reset/SKILL.md` | Step 3 becomes a single verification — *the console was not written by this session outside the D2 contract* — replacing the checkpoint-and-compact ritual (incl. the "no history file" and judgment-guard paragraphs, which exist to police the deleted file). Step 1's "MASTER_PLAN ↔ Linear in sync" gate dropped. Step 2 gains D1's rule for `LAST_SESSION.md` plus a **stated size bound of 90 lines**, checked at write time (current file: 82). Intro + frontmatter description updated. |
| `.claude/skills/master/SKILL.md` | Step 8's "Update MASTER_PLAN on main if strategy/sequencing changed" bullet deleted. Step 5's "the Linear integration auto-moves the ticket" becomes **master performs the transition**, pointing at advance-dispatch. Advance-dispatch (Step 8) gains the on-merge → `Awaiting Deploy` transition as an explicit binding rule. Step 3 doc-drift target loses MASTER_PLAN, gains the console + ADR status. Step 6 deploy prose references the ladder as the grant's home (D3 iff-rule) while keeping the mechanics. Frontmatter description updated. |
| `.claude/skills/lifecycle-rules.md` | § MASTER_PLAN → **§ Coordination stores**, stating D1 (no second copy), D2 (console authorship), D4 (the writer table, verbatim). Guardian "Plan owner" → **"Console reader / board writer."** § Ticket state gains the `Backlog` filing path for non-actionable findings (New-actionable == `Needs Approval`, unchanged) and drops the GitHub-integration auto-transition paragraph in favour of master's. § Dispatch gains "Parking = remove the stream label" (from MASTER_PLAN 192–193 — mechanics, per D3 not console content). |
| `.claude/skills/adr/SKILL.md` | Three boundary lines → "never write the console; never mutate Linear control-plane fields beyond moving your own ticket to In Progress" (filing tickets + posting comments stay open per D4). Step-6 doc-drift bullet drops MASTER_PLAN. |
| `.claude/skills/build/SKILL.md` | Step-9 boundary line → the same D4 wording. |
| `CLAUDE.md` (root) | Sources table: "What are we doing next, in order?" → the dispatch resolver + `docs/plans/OWNER_CONSOLE.md`. The `_(0102–0107 not enumerated here — MASTER_PLAN is authoritative.)_` parenthetical inside the historical `<details>` snapshot re-pointed to Linear (AC-b greps the whole file). |

### Fold-ins — live contracts that would otherwise point at a deleted path

Per build skill Step 5 these are supporting changes to make this build function, not separate work.

| File | Edit | Why it can't wait |
|---|---|---|
| `.claude/skills/prime-explore/SKILL.md` | Step 5 target → resolver + console; hands-off invariant → "never write the console" | a live priming skill that would read a 404 |
| `.claude/CLAUDE.md` | 4 refs (file-org table, two "before starting work" checklists, key-files table) | read every session as project instructions |
| `docs/plans/templates/LAST_SESSION.md` | 2 refs + the D1 rule and 90-line bound stated in the template | `prepare-reset` copies this structure |
| `.github/PULL_REQUEST_TEMPLATE.md` | "or in MASTER_PLAN.md follow-up" → "or a `Backlog` ticket" | every PR renders it |
| `.claude/hooks/block-direct-main-push.sh` + `test_block_direct_main_push.sh` | comment-only: "the docs/MASTER_PLAN direct-to-main path" → "the docs direct-to-main path" | live hook; comment names a dead file |
| `infrastructure/systemd/{seshat-dispatch-orchestrator,seshat-gating-watcher}.service` | comment-only boundary lines | shipped unit files |
| `docs/runbooks/dispatch-orchestrator.md` | 4 refs (2 boundary lines, 1 obsolete quoted nudge, 1 Phase-C step) | live runbook |
| `tests/scripts/test_dispatch_runbook.py` | **the coupled change**: asserts the literal `"never merges, deploys, closes tickets, or edits master_plan"` — updated in lockstep with the runbook or CI goes red | hard failure otherwise |
| `scripts/reconcile_board.py`, `tests/scripts/test_reconcile_board.py` | docstrings explaining the retired MASTER_PLAN-parsing path → state the file is now deleted | docstring accuracy only |
| `docs/reference/DELIVERY_PIPELINE.md` | § 4 "Relationship with the MASTER_PLAN" → "Relationship with the dispatch resolver + owner console"; the drift-catcher, self-dispatch, boundary and topology-diagram refs | a live reference doc; its "Stream Board" text is *already* stale (Linear-native dispatch, 2026-07-04) — corrected in passing since the section is being rewritten anyway |
| `docs/reference/PROJECT_DIRECTORY_STRUCTURE.md` · `docs/reference/AGENT_WORKFLOW_METHODOLOGY.md` · `docs/plans/{README,AGENTS,DEV_TRACKER}.md` · `docs/README.md` · `docs/architecture_decisions/TECHNICAL_DEBT.md` · `docs/VISION_DOC.md` | pointer/index refs re-pointed at the resolver + console | dangling links to a deleted file |
| `docs/research/README.md:243` | the **live quick-links index** at the file's foot links `[Master Plan](../plans/MASTER_PLAN.md)` → re-pointed | a live index, not a historical research document — the blanket `docs/research/**` exclusion does not reach it |
| `.cursor/rules/{agent-planning,session-orientation}.mdc` | 4 refs | live editor rules on the same tree |

### Deliberately NOT touched

`docs/plans/completed/**` · `docs/plans/sessions/**` · `docs/research/**` · `docs/superpowers/**`
(49 historical plans) · `docs/archive/**` · `docs/specs/**` · historical ADRs — these record the past
accurately and AC-b excepts them. **`.claude/settings.local.json`** is untracked (stale permission
entries only). `docs/architecture_decisions/README.md`'s single hit is ADR-0131's own title.

Three exclusions are deliberate and were challenged in review — each is recorded here with its reason,
because two of them do leave a stale token behind:

- **`docs/plans/LAST_SESSION.md:9,56`** — genuinely becomes a dangling pointer ("See §0b of
  MASTER_PLAN"). Not fixed here anyway: **D4, which this very PR establishes, makes the outgoing master
  session its sole writer**, and the file is overwritten wholesale at the next wind-down. Editing
  another store's file while codifying its single-writer rule would contradict the change. **Flagged to
  master in the handoff** to strip at the next `prepare-reset` — a one-reset window, not standing drift.
- **`docs/architecture_decisions/ADR-0110.md:161–163`** — states an operational boundary referencing
  MASTER_PLAN. Left as written: an ADR is a dated record of a decision *as taken*, and ADR-0131 is the
  document that supersedes it. Rewriting superseded ADRs in place is how a decision trail stops being
  one.
- **`CLAUDE.md`'s historical `<details>` snapshot (line 239)** — *is* edited, despite the block being
  marked "retained for context, not maintained." **AC-b is explicit**: a repo-wide grep must find no
  remaining reference *in root CLAUDE.md*, with only `docs/plans/completed/` and `docs/research/`
  excepted. The AC wins; the edit is a single parenthetical re-pointed at Linear, and the snapshot's
  historical content is otherwise untouched.

## 6. Execution order

| # | Step | Verify |
|---|---|---|
| 1 | Plan → codex plan-review → revise | codex verdict recorded |
| 2 | Post §3 disposition table on FRE-1085; ask the owner for O1/O2/O3 | owner approval comment on the ticket, dated |
| 3 | Author `OWNER_CONSOLE.md` from O2/O3 | `wc -l` ≤ the stated bound; 6 ladder rows; every record matches its AC-4 schema |
| 4 | Execute the dispositions: ~28 ticket comments, ~13 `Backlog` filings | each row in §3 marked executed with its comment/ticket id |
| 5 | All D5 edits (§5 table 1) | per-file diff review |
| 6 | All fold-ins (§5 table 2), test updated in lockstep | `make test-file FILE=tests/scripts/test_dispatch_runbook.py` green |
| 7 | `git rm docs/plans/MASTER_PLAN.md` | file absent |
| 8 | Quality gates | `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files` |
| 9 | Self-review (`code-review`, effort `low` — docs + one test assertion, no src logic) | findings fixed on-branch |
| 10 | Rebase on `origin/main`, PR, handoff comment | AC proof per §7 |

**Atomicity guard.** ADR-0131 requires the migration to land atomically — no state where a skill
mandates writing a deleted file, and none where two contradictory contracts coexist. Steps 3–7 are
therefore **one commit, and nothing is pushed until every one of them plus the full-repo audit passes**.
Concretely: do not commit before step 7 completes, and gate the push on
`grep -rn MASTER_PLAN .claude/ CLAUDE.md docs/plans/ docs/reference/ docs/runbooks/ scripts/ tests/ .github/ infrastructure/ .cursor/`
returning only the three deliberate exclusions above. An interrupted run leaves an uncommitted working
tree, which is recoverable; a pushed half-migration is the failure mode the ADR names.

## 7. Acceptance criteria → proof

| AC | Proof |
|---|---|
| **AC-a** — console with D2 contract header, numeric size bound, exactly one ladder row per manifest class | `grep -c` the six class names in `OWNER_CONSOLE.md`, each exactly 1; paste the header and the ladder table into the handoff |
| **AC-b** — MASTER_PLAN absent; no reference in the four skills / lifecycle-rules / root CLAUDE.md | `git ls-files docs/plans/MASTER_PLAN.md` → empty; `grep -rn MASTER_PLAN .claude/skills/ CLAUDE.md` → no output. Paste both |
| **AC-c** — disposition table + owner approval on the ticket, dated before merge | link the two Linear comments with timestamps |
| **AC-d** — prepare-reset has no compaction step; prime-master step 8 names resolver + console; master's close-out has the on-merge transition and no plan-update step | paste the three post-edit excerpts |

## 8. Halt conditions specific to this ticket

- **The owner does not approve the disposition table** → the PR does not merge (ADR-0131: "deleting
  the file is forbidden until it is"). Surface and stop; do not delete on a partial adjudication.
- **A directive-shaped row the owner keeps has no stateable retirement condition** → D2 refuses it as
  a directive. Surface the specific row rather than inventing a condition.
- **`make mypy` shows >5 errors not introduced here** → separate ticket (lifecycle-rules § Halt).
