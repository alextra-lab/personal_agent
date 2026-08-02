# FRE-1115 — Entities that carry no usable knowledge

**Ticket:** FRE-1115 (Approved → In Progress) · **Project:** Memory Recall Quality
**Backing analysis:** FRE-1116 (master's rescope comment, 2026-08-01) · **Related:** FRE-1114, FRE-1118
**Plan review:** codex adversarial pass, 2026-08-02 (session `019fc2f2-3046-7652-9584-02006735bcb0`) — revised below

---

## 1. Investigation findings (Step 2 — the ticket's three open questions, answered)

All figures re-derived on the live graph (`cloud-sim-neo4j`) 2026-08-02.
Census reproduces master's: **7,543 entities · 1,404 empty (18.6%) · 721 self-referential (9.6%)**.

### Q1 — Are the empties extraction failures, undescribed relationship targets, or a legitimate category?

**None of the three as posed. The mechanism is dedup orphaning**, established by a per-node trace join.

`create_entity` sets `e.entity_id` unconditionally; `create_conversation`'s inline DISCUSSES MERGE
(`memory/service.py:1275`) does not. That gives a clean discriminator:

| Probe | Result |
|---|---|
| Empty-description entities with `entity_id IS NULL` | **1,404 / 1,404** — none was ever written by `create_entity` |
| Empty entities carrying a `Turn-[:DISCUSSES]->` edge | **1,403 / 1,404** |
| `entity_creation_failed` events, 30d | **0** |
| `entity_created` / `entity_deduplicated`, 30d | **1,939 / 614** — 32% of writes renamed |
| Different-name renames in a 200-event sample | **30 (15%)**; 10 case-only; 160 no-op |
| **Per-node join: orphan's `originating_trace_id` → same-trace `entity_deduplicated` naming that exact orphan** | **60 / 60 confirmed, 0 exceptions** |

The chain: `create_conversation` bare-MERGEs an `:Entity` per `key_entities` name → `create_entity`
runs and dedup rewrites the write to a *different canonical name* → the description lands on the
canonical node → the bare node under the extractor's original name is **orphaned empty forever**.

The trace join is the decisive instrument (codex correctly judged the earlier signature-only evidence
underdetermined). The alternatives it raised — `store_episode`, mid-consolidation process termination,
owner bootstrap — each produce an orphan *without* a same-trace dedup event naming it; none occurred
in 60/60. **Honest limit:** the sample is 60 orphans from July onward, bounded by ES log retention.
May's 919 orphans cannot be tested this way. The claim is therefore established **for the live
minting population**, which is the population the fix addresses.

Still minting: **9.1% of July's new entities, 9.4% of August's** — live, not historical.

**The orphans are not inert.** `create_relationship` MATCHes endpoints by `entity_id OR name` using
the extractor's **raw** names — exactly the orphan names — so **2,115 entity→entity edges across
1,159 orphans** currently attach the semantic graph to knowledge-free nodes instead of to the
described canonical ones. This constrains the fix: removing the orphan without translating
relationship endpoints would silently **drop** those edges.

### Q2 — Do the self-referential descriptions cluster?

Yes — on **one prompt phrase**. `second_brain/entity_extraction.py:208` rule 10 asks *"what makes this
entity notable **here**?"*, directing the model to describe the occasion rather than the subject.
Every one of the 17 degraded descriptions examined is phrased `"<X> discussed as …"` / `"mentioned as …"`.

### Q3 — Should an empty-description entity be recallable?

It should not **exist**. Fix at source, not by filtering downstream.

### The corruption loop — established as general, severity corrected

Instrument: the `HAD_DESCRIPTION` → `EntityDescriptionVersion` archive.

| Overwrite class (71 archived versions total) | Count |
|---|---|
| non-self-referential → **self-referential** | **17 (24%)** |
| self-referential → non-self-referential (repair) | 9 |
| self-referential → self-referential | 2 |
| clean → clean | 43 |

**It is general** — not a single sighting. But the ticket forbids claiming a result from a regex count,
so all 17 were hand-classified:

- **~5 genuine knowledge loss.** `Personal Local AI Collaborator v0.1.0` replaced by a description of a
  different subject; `Amlodipine` lost "calcium channel blocker / high blood pressure"; `Pluralistic`
  lost "run by Cory Doctorow"; `Personal History Recall Tool` (×2) lost a recorded bug finding.
- **~10 framing-only or lateral** — `Matrix Gla-Protein`, `Ezetimibe`, `sendOffsetsToTransaction`,
  `Liver dysfunction` (×3) keep their knowledge, merely wrapped in discussion language.
- **~2 improved** (`Hopfield network`, `Pour sortir au jour de connesson`).

Dominant expression is **framing degradation**, with real destruction a smaller but live subset —
reported as such rather than as "24% corruption".

**Root cause.** The FRE-725 gate admits an equal-confidence `enrichment` when
`size($description) >= size(_old_desc)`. Discussion-framing is *longer* than the definition it
replaces, so the defect's own verbosity **defeats the anti-shrink guard**.

### The dedup threshold is conflating distinct entities

`dedup_similarity_threshold = 0.92` (`config/settings.py:535`) with the OVH 8B embedder:

```
Blueberries    -> Apricots            0.957      Azure         -> Bedrock            0.952
mathematics    -> computer science    0.960      Vertex        -> Bedrock            0.939
neuroscience   -> computer science    0.923      Walkaway      -> Little Brother     0.936
Arts faculty   -> Cornell University  0.935      WebAuthn      -> Passkey auth       0.978
```

Roughly 8–9 of 14 different-name renames inspected are wrong. The only existing guard is an ALL_CAPS
casing check (`dedup.py:97`).

**Codex's sharpest finding, accepted:** the orphan currently acts as an *accidental preservation* of
the distinct raw mention. Step 3 removes it, so the Turn — and, once endpoints are translated, the
semantic relationships — would attach directly to the **wrong** canonical entity. Emptiness would
improve while conflation became worse and less visible. **Shipping Step 3 without containment makes
the graph worse.** Containment is therefore in scope (Step 0); the full dedup redesign is not (Step 7).

---

## 2. Acceptance criteria

The ticket states an "It fails if" rather than an AC table; these are its decidable form.
(Flagged in the handoff: tickets in this project should carry an explicit AC table.)

| # | Criterion | Proof |
|---|---|---|
| AC-1 | The cause of the empty descriptions is established with evidence, not assumed | §1 Q1 — per-node trace join, 60/60, with its stated sample limit |
| AC-2 | The corruption loop is established as general behaviour, severity measured honestly | §1 loop table + 17-case hand classification |
| AC-3 | No new empty-description `:Entity` is minted by the consolidation write path | Test: dedup-renamed entity leaves no orphan; post-deploy probe shows mint rate → 0% |
| AC-3b | Relationships survive canonicalisation — no edge dropped or left on an orphan | Test: renamed endpoint yields an edge on the canonical node; edge count preserved |
| AC-4 | A self-referential description cannot overwrite a non-self-referential one | Test: gate rejects the overwrite; archive count unchanged |
| AC-5 | The extraction prompt no longer asks for the occasion | Contract test asserts rule 10 wording + BAD example |
| AC-6 | Success is not claimed from a reduced count alone | Repair script reports why each orphan is removed; no description invented |
| AC-7 | The fix does not trade emptiness for conflation | Test: each observed wrong pair (`Blueberries`/`Apricots`, `Azure`/`Bedrock`, `mathematics`/`computer science`) stays distinct; case/accent pairs still merge |

---

## 3. Implementation steps

### Step 0 — Containment: dedup may only merge name-equivalent entities *(memory/dedup.py)* — **NEW, gates Step 3**
Add a name-compatibility guard to the above-threshold branch: a different-name merge is permitted only
when the two names are equivalent under casefold + Unicode accent-fold + whitespace/punctuation
normalization (`predictive processing`≡`Predictive Processing`, `Pâté à bombe`≡`Pâte à bombe`).
Every rejected merge is logged as `entity_dedup_rejected_name_incompatible` with both names and the
similarity — this is the auditable dataset the Step 7 ticket needs.
Rejecting is **fail-safe**: it creates a distinct entity rather than destroying one. It also rejects
some arguably-fair merges (`Cumin`→`Ground cumin`); those are substantive decisions that belong to the
Step 7 ADR, not to a 0.92 cosine.
→ verify: `make test-file FILE=tests/personal_agent/memory/test_dedup.py`

### Step 1 — Prompt: stop asking for the occasion *(entity_extraction.py)*
Rewrite rule 10 to demand a standalone definition of the subject and explicitly forbid
discussion-framing; add a BAD example mirroring the Clafoutis case.
→ verify: `make test-file FILE=tests/test_second_brain/test_entity_extraction_contract.py`

### Step 2 — Gate: a self-referential description may not overwrite a clean one *(memory/service.py)*
Module constant `_SELF_REFERENTIAL_DESCRIPTION_PATTERN`, defined **once** and used for both sides:
the new-description predicate computed in Python, the old-description predicate applied to `_old_desc`
in Cypher, with the pattern bound as a parameter so the two cannot drift. Add to `_do_correct`:
`AND NOT ($new_self_ref AND NOT (_old_desc =~ $self_ref_pattern))`.
**`_do_fill` stays unguarded** — filling an empty description is an improvement even when framed.
Codex confirmed the archive interaction is correct: `HAD_DESCRIPTION` is written only under
`_do_correct`, so a rejected correction creates no archive row.
→ verify: `make test-file FILE=tests/personal_agent/memory/test_entity_description_correction_cypher.py`

### Step 3 — Remove the orphan generator *(second_brain/consolidator.py)*
Move the `create_entity` loop **before** `create_conversation` and build `key_entities` from the
returned canonical ids. Per codex, all of the following are part of this step, not optional polish:

- Keep a stable `raw_name -> canonical_id` map; apply it to `key_entities`, to the `_entity_data`
  type map, **and to every relationship endpoint** (otherwise the 2,115-edge population is dropped).
- **Deduplicate** the canonical `key_entities` list — several raw names can converge on one canonical.
- **One owner for `mention_count`.** Today both writes increment it; after the reorder both would land
  on the same canonical node. Increment in `create_entity` only; drop it from the inline MERGE.
- **No raw-name fallback when `create_entity` returns `""`.** Falling back re-creates the exact bug.
  Record the unresolved mention on the Turn without creating an `:Entity`.
- **Check `create_conversation`'s boolean result** before reporting `turns_created` or fetching edge
  ids — it is currently ignored, so a failed Turn write is reported as success.
- `dispatched_away_names` gating (ADR-0115 D3) is applied to the **raw** name before translation, so
  existing skip semantics are unchanged. Stub-Turn path is unaffected (`key_entities=[]`).
→ verify: `make test-file FILE=tests/personal_agent/second_brain/test_consolidator_dispatch.py`

### Step 4 — Repair script for the existing 1,404 orphans *(scripts/, runbook — master runs post-deploy)*
Dry-run by default; `--apply` repoints **both** DISCUSSES and entity→entity edges to the canonical node
and deletes the orphan **only** on the exact signature (`entity_id IS NULL` **and** empty description).
Where the canonical cannot be resolved, the orphan is left in place and reported — never deleted with
its edges. Never invents a description (AC-6).

### Step 5 — Tests (TDD, written first)
Step 0: each observed wrong pair stays distinct; case/accent pairs still merge.
Step 2: `discussed as` / `mentioned as` / capitalization / incidental use inside an otherwise clean
definition / clean repair still allowed / `_do_fill` unaffected / rejected write archives nothing.
Step 3: renamed relationship endpoint lands on canonical; several raw names → one canonical;
`create_entity` failure; Turn-write failure; no orphan minted.
Step 4: dry-run output correctness.

### Step 6 — Documentation
Update the memory write-path docs and any ADR-0115/ADR-0074 references the reorder touches.

### Step 7 — File the dedup-conflation ticket (Needs Approval, ADR-required)
Scope: threshold calibration per embedder, type-aware and name-aware merge policy, alias handling,
and what to do with the merges Step 0 now rejects. Attach the
`entity_dedup_rejected_name_incompatible` log data as its evidence base.

---

## 4. Quality gates
`make test` (module then full) · `make mypy` · `make ruff-check` · `make ruff-format` ·
`pre-commit run --all-files` · code-review **high** (memory write path) · security-review
(script touches live substrate).

## 5. Risk
Step 3 reorders a production write path — the main risk, and the reason Step 0 lands first. Step 4 is
destructive and therefore dry-run by default and handed to master as a runbook rather than executed
here.
