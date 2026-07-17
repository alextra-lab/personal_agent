# FRE-670 — Vocabulary-divergent (semantic) recall probe split

**Ticket:** FRE-670 (Approved, Tier-1:Opus, project "Memory Recall Quality", parent FRE-435)
**Backing ADR:** ADR-0087 (measurement-first recall program, §D2 gate set). FRE-670 extends the §D2 gate.
**Branch:** `fre-670-vocab-divergent-recall-probe`

## Problem

The FRE-489 gate set (`bespoke_probe.yaml`) is **lexical masked as semantic**: its queries are
oblique about entity *names* but share *description-level* vocabulary with the stored note text. So a
plain BM25 keyword search beats the vector path (R@5 1.00 vs 0.72, FRE-656), and the 0.6B and 4B
embedders score identically — there is no semantic gap for a better embedder to close. The probe
cannot justify the vector+reranker apparatus or distinguish embedders, and it **blocks the embedder
swap decision** (FRE-656/671): no production re-embed should proceed until a probe exists where
semantic matching actually matters.

## Goal / acceptance criteria (the definition of done)

From the ticket + ADR-0087 §D2 + the owner/master build handoff (Linear comments 2026-06-29):

1. **AC1 — a vocabulary-divergent probe split exists**, committed and PII-scrubbed, translated from
   the owner-authored 54-case working set. Two disciplines structurally enforced in CI: *referential*
   (query never names its answer) and *vocab-divergent* (query↔note content-token overlap is low).
2. **AC2 — BM25 keyword recall@5 lands materially below vector recall@5 on the positives.** This is
   the headline instrument check: if keyword wins or ties, the split is still lexical and the ticket
   is NOT done. Measured by folding a **BM25 standing column** into the harness/instrument.
3. **AC3 — the harness reports** positives recall@k, control abstention rate, and the
   natural-vs-imagery **register delta** (does recall degrade on oblique phrasing?).

Master re-runs the FRE-656 embedder A/B (0.6B vs 4B vs cloud reference) on this split afterward; that
A/B is FRE-656, not this ticket. FRE-670 delivers the *scoreboard* + the instrument-check that it is
genuinely semantic.

## Source material (gitignored working files — provenance, never committed)

- `telemetry/archive/fre670_probe_queries.md` — owner-authored 54-case set (44 positives across 12
  themes; 7 natural-voice register-pairs vs imagery twins; 9 compound; 10 controls incl. 4 over-recall
  traps). Real personal facts + real expected answers.
- `telemetry/archive/fre670_session_summaries.md` — ground-truth corpus digest the queries were
  authored against.

## Design decisions

### D1 — Substrate / instrument: THREE ARMS, test substrate only (owner-decided 2026-06-29)

**Never the live prod KG.** Everything runs in the isolated FRE-375 test substrate (Neo4j :7688 /
ES :9201 / Postgres :5433). Three arms over the 54 paraphrased notes, reusing the existing FRE-656
tools — no new big instrument:

- **Arm A — BM25 baseline:** `keyword_baseline.py` (parametrized to the new probe). Ranks each query
  against the co-resident note texts (name + description); recall@1/@5 + register split.
- **Arm B — 0.6B embedder vector path:** `run_embedder_benchmark.sh 0.6b ab --probe-set …` →
  `ab_relevance_bounded.py` on the test substrate (real `:8503` embedder, production recall path).
- **Arm C — 4B embedder vector path:** `run_embedder_benchmark.sh 4b ab --probe-set …`. **Needs
  CF-Access creds** (Access-gated `slm.example.com`) — produced in-session only if creds are
  available, else handed to master with the exact command (master ran FRE-656).

Per arm, report **recall@1, recall@5, control abstention rate, natural-vs-imagery register delta**.
**AC2** = BM25 recall@5 materially below the vector recall@5. Separately, the **0.6B-vs-4B delta is
the embedder/re-embed decision input** (the FRE-656 re-run on this harder probe).

**Apples-to-apples honesty (codex flag #1):** BM25 ranks over the clean 54-note corpus; the vector
arms run the full production recall path (wipe-per-case + a live read-only distractor background →
recency pressure). BM25 thus gets the *easier* corpus (no distractor noise), so if it still loses,
the semantic win is conservative. This asymmetry is stated in the writeup, never hidden.

Raw results → `telemetry/archive/` (gitignored); commit **paraphrased aggregates only**.

### D2 — Paraphrase honesty (anti-gaming)

The note text (`seed_entities[].description`) must faithfully represent the *real stored content* and
its real vocabulary; PII is scrubbed token-by-token (locations → generic descriptors, personal names
→ placeholders) but the note's content words are NOT rewritten to manufacture divergence. The
divergence must come from the owner's oblique *query*, not from my note-rewriting. Public facts
(Knossos, King Charles III, Rayleigh scattering, Kafka log compaction) stay verbatim — they are not
PII. This is enforced by keeping the working file as the auditable provenance record and by the
CI overlap test being a *consequence* of honest paraphrase, not a target to optimize.

### D3 — Encode register/type/theme/compound as TAGS (no ProbeCase schema change)

`ProbeCase.tags` is already free-form. Encode: `register:natural` | `register:imagery`;
`type:positive` | `type:control`; `theme:<n>`; `compound`; `pair:<id>` (links register-pairs);
`trap:over-recall` on the 4 control traps. Controls carry empty `expected.entity_names` +
`must_not_deny: false` (the harness already treats these as abstention cases). **No schema change** →
zero risk to the existing FRE-488/489 probes. Report aggregations read the tags.

### D4 — BM25 standing guard = parametrized `keyword_baseline.py` (NOT a harness.py fold)

The owner's reuse-first direction makes `keyword_baseline.py` the standing BM25 guard: parametrize it
with `--probe` so any probe reports keyword recall, run alongside the embedder benchmark. This honors
the ticket's "BM25 reported alongside vector recall as a standing lexical-leakage guard" while
**avoiding the four correctness traps codex flagged** in folding BM25 through
`harness.py`/`scoring.py`/`CaseResult` (binary-vs-fractional recall mismatch, namespaced-id merge,
tie-order non-determinism, schema-boundary change). If master later wants it literally inside
`harness.py`, that is a clean follow-up ticket. (Noted in the Linear handoff for master's call.)

## Plan (atomic steps)

### Step 1 — Translate the 54-case working set → committed `semantic_probe.yaml` (TDD: tests first)
- **1a.** Write `tests/evaluation/test_fre670_semantic_probe.py` (RED): load `semantic_probe.yaml`;
  assert 54 cases, unique ids, ≥44 positives, ≥10 controls, ≥7 register-pairs (matched `pair:` tags
  in both registers), 12 themes present; referential discipline (reuse FRE-489 test logic);
  extended PII denylist (location/name tokens from the working set: e.g. `mane`, `forcalquier`,
  `manosque`, `marseille`, `florian`, `theo`, `susan`, `lyon`, plus the existing `alex`/`icloud`/`@`);
  **vocab-divergence**: for every positive, content-token Jaccard(query, note text **incl. history**,
  light suffix-stemming) < 0.15 AND query does not name expected entity; controls have empty relevant
  + `must_not_deny: false`; positives have `seed_entities` and each expected entity is seeded.
  - verify: `make test-k K=fre670_semantic` → fails (no YAML yet).
- **1b.** Author `scripts/eval/fre435_memory_recall/semantic_probe.yaml` — 54 cases translated from the
  working file, PII-scrubbed per D2, tagged per D3. Note text = faithful paraphrase of the real stored
  content (`seed_entities[].description`); query = owner's oblique phrasing, PII-scrubbed only.
  - verify: `make test-k K=fre670_semantic` → passes.

### Step 2 — Arm A: parametrize `keyword_baseline.py` as the BM25 standing guard (TDD)
- **2a.** Add a pure unit test (`test_fre670_retrieval_baseline.py`): BM25 ranking determinism on a
  synthetic corpus (score>0 filter + deterministic tie-break — codex flag #4c), and the per-register
  recall split.
- **2b.** Refactor `keyword_baseline.py`: factor the BM25 core into a reusable helper; add
  `--probe <path>` (default `bespoke_probe.yaml`, back-compat); fix zero-score ranking (a doc with
  BM25 score 0 is NOT a hit) + deterministic tie-break by name; add a **per-register** split
  (`register:natural` vs `register:imagery`) + a controls row. Recall@1/@5.
  - verify: `make test-k K=fre670_retrieval` passes; `uv run python .../keyword_baseline.py
    --probe .../semantic_probe.yaml` prints the BM25 table (recall@1/@5, register split, controls).

### Step 3 — Arms B/C: extend `ab_relevance_bounded.py` summary for the per-arm report (TDD)
- **3a.** Add unit tests for the new pure summary helpers (recall@1 alongside @5; register-delta from
  tags; control-abstention rate = fraction of `type:control` cases the flag-on path correctly denies).
- **3b.** Extend `_print_summary` / `ABReport` in `ab_relevance_bounded.py`: also capture recall@1, the
  natural-vs-imagery register delta (tag-driven), and the control-abstention rate. No change to the
  seeding/recall mechanism (reused as-is). The 0.6B/4B arms run through `run_embedder_benchmark.sh`.
  - verify: `make test-k K=fre435` (full fre435 suite) passes.

### Step 4 — Run the three arms + capture the acceptance number (test substrate only)
- **4a. Arm A (BM25):** `uv run python .../keyword_baseline.py --probe .../semantic_probe.yaml`.
- **4b. Arm B (0.6B):** `export AGENT_NEO4J_PASSWORD=<test :7688 pw>` then
  `run_embedder_benchmark.sh 0.6b ab --probe-set .../semantic_probe.yaml`.
- **4c. Arm C (4B):** additionally `export CF_ACCESS_CLIENT_ID/SECRET` (from the repo `.env`,
  tunnelled `slm.example.com`) then `run_embedder_benchmark.sh 4b calibrate --probe-set .../semantic_probe.yaml`.
- **AC2 gate:** BM25 recall@5 must land materially below the vector recall@5. If not, STOP and
  surface — do NOT rewrite notes to force the gap. 0.6B-vs-4B delta = the FRE-656 embedder input.
- Raw → `telemetry/archive/` (gitignored). Curated **aggregates only** → Linear comment +
  `docs/research/2026-06-29-fre-670-semantic-probe.md` (no raw queries, no PII).

### Step 5 — Docs + quality gates
- Update `scripts/eval/fre435_memory_recall/README.md` (new probe + `--probe` BM25 guard + three-arm run).
- `make test` (module then full) · `make mypy` · `make ruff-check` + `make ruff-format` ·
  `pre-commit run --all-files`.

## Risks / halt conditions
- **AC2 fails (BM25 ≥ vector):** the split is still lexical → STOP, surface to owner, do not game by
  rewriting notes. This is the whole point of the ticket.
- **Embedder/test stack down:** produce BM25 + structural tests in-session; hand the vector number to
  master's live step. (Both currently UP, so expect to produce the full number.)
- **PII leak:** the extended denylist test is the backstop; the working files stay gitignored.

## Out of scope
- The FRE-656 embedder A/B re-run (0.6B vs 4B vs cloud) — that is FRE-656, gated on this probe.
- Any production embedder swap / KG re-embed (FRE-656/671).
- Live-prod-KG scoring by real entity name (PII; not how the harness works).
