# FRE-1020 — Make the ADR-0098 D2 supersession safety guard reachable

**Ticket:** FRE-1020 (Approved, Tier-1:Opus, stream:build1)
**Backing ADR:** ADR-0098 D2 (living claims, *not* naive last-write-wins) + D6 (co-authorship → trust)
**Neighbour:** ADR-0100 (owed a premise note) · raised by ADR-0126 D8
**Status:** rev 2 — revised after codex plan-review (findings 1/4 accepted and redesigned; 4 refuted with live data)

---

## 1. Verified diagnosis

Confirmed in source and independently against the live graph (`cloud-sim-neo4j`, 2026-07-27):

| Probe | Result |
|---|---|
| `count(:Claim)` | **94** |
| `count(DISTINCT confidence)` | **1** — `[0.8]` |
| `count(DISTINCT source_type)` | **1** — `["conversation"]` |
| Claims ever REJECTed (`invalid_at` set, `superseded_by` null) | **0** |
| `supersession_reason` distribution | `evolution` × 3, else NULL |
| Supersessions labelled `correction` | **0** |

Chain: `entity_extraction._build_provenance` hard-codes `"source_type": "conversation"`
(line 592) → `consolidator._build_claim` sets `confidence=KnowledgeWeight.from_source(source_type).confidence`
(line 117) → `_DEFAULT_CONFIDENCE["conversation"] = 0.8` → `service.assert_claim` passes that
constant into `supersession.adjudicate`.

In `adjudicate`, with `new_confidence == candidate.confidence` always:

- `if new_confidence < candidate.confidence: REJECT` — **unreachable**.
- `reason = "correction" if new_confidence > candidate.confidence else "evolution"` — the
  `correction` arm is **unreachable**; every heuristic supersession is labelled `evolution`
  (matching the live 3×`evolution`, 0×`correction`).
- Only `new_observed_at < candidate.observed_at` survives.

The explicit `update_kind` signal drives the *label* only, never the FRESH/REJECT safety
decision — and live it has never even reached a supersede: the single
`update_kind:"correction"` claim was adjudicated FRESH.

**Net: supersession is newer-wins + a staleness guard = exactly the naive last-write-wins
model ADR-0098 D2 names and rejects.** The ticket's premise is confirmed in full.

---

## 2. The design call (this is why the ticket is Tier-1)

ADR-0098 D6 says co-authorship is *"realized through `KnowledgeWeight.source_type` at the
promotion gate."* That is not implementable as written, and that is the root cause:

`SourceType = conversation | tool_result | web_search | manual | inferred` is a **channel**
vocabulary — *how the fact arrived*. Co-authorship is a **different axis** — *who asserted
it*. Every claim arrives through the conversation channel, so the channel vocabulary is
structurally incapable of expressing authorship. It is constant today because it is
recording something that genuinely does not vary. (`Claim.source_type`'s own docstring
already calls it "Origin channel".)

### 2.1 Does an agent-derived population actually exist? — measured, not assumed

Codex's sharpest challenge: the prompt frames claims as *the user's* personal facts and
`_finalize_extraction` forces `subject=owner`, so perhaps every claim is user-authored by
definition and the discriminator would just move a constant from 0.8 to 0.9.

**Measured offline against all 94 live claims** (each joins 1:1 to its source `:Turn`, which
retains `user_message` / `assistant_response`), scoring content-word overlap of the claim
against each speaker's text:

| Grounding | Claims | Share |
|---|---|---|
| **assistant-grounded** (a > u + 0.15) | **40** | **43 %** |
| user-grounded (u > a + 0.15) | 23 | 24 % |
| both similar | 18 | 19 % |
| neither | 13 | 14 % |

The agent-derived population is the **plurality**. Representative assistant-grounded claims,
all stored as durable Personal facts about the owner:

- *"The sandbox network bridge cloud-sim is not attached in this environment."* (u=0.00, a=0.86)
- *"The current container cannot install Python packages directly because pip is unavailable."* (u=0.10, a=0.60)
- *"The user's system currently lacks an automatic entity and fact extraction pipeline."* (u=0.12, a=0.75)

Today every one of these can clobber a fact the owner stated in their own words. The
challenge is **refuted**: the split is real and the defect it enables is material.

### 2.2 Recommendation — authorship derived in Python, never self-reported

**Add `asserted_by` as a first-class per-claim axis, computed by Python from the
role-partitioned captured text. The extraction model is not consulted.**

This is the change codex's finding #1 forced, and it is the stronger design. My first draft
had the extractor emit `asserted_by` with Python validating only its vocabulary — which
would let the model mint the very credential that makes its own output authoritative
(the assistant hallucinates a fact, the extractor labels it `user`, it gets 0.9 and outranks
a correct claim). That is exactly what ADR-0098 AC-9 forbids: trust must pin to
independently-recorded source identity, never to a self-attributed label.

- **Where:** `entity_extraction._finalize_extraction` — it already owns Python-side stamping
  of `facet` / `update_kind` / provenance, and `user_message` / `assistant_response` are
  already in scope at its call site (line 1008). Thread both in; stamp `asserted_by` per
  claim beside the fields Python already owns.
- **What it keys on:** content-word overlap of the claim against each speaker's message —
  immutable captured evidence the model cannot influence after the fact.
- **Rule:** `user` when `user_overlap >= 0.5` **and** `user_overlap > agent_overlap + 0.15`;
  otherwise `agent`. Both thresholds are module constants.
- **Per-claim, not per-turn** — provenance is built once per turn and copied; authorship
  varies claim-by-claim, so it belongs on the claim object beside `facet`/`update_kind`.

Threshold sweep over the live 94 (the rule is stable, not knife-edge — 18–28 % across the
whole grid):

| min_user | margin | → user | → agent |
|---|---|---|---|
| 0.3 | 0.15 | 23 (24 %) | 71 |
| **0.5** | **0.15** | **20 (21 %)** | **74** |
| 0.6 | 0.15 | 17 (18 %) | 77 |

At the recommended `0.5 / 0.15`, the claims that earn the uplift are qualitatively right —
*"The user has an HKoenig glacier ice cream maker"*, *"The user has animals in the field next
to their home"*, *"The user is looking to buy a product to treat hair thinning due to GLP-1
use"*. Only ~1 in 5 claims receives elevated authority, which is the conservative direction.

### 2.3 Confidence mapping: uplift, never demote

Authorship is an **uplift over the channel base**, and the *agent* tier **is** the channel base:

| | conversation | inferred | manual |
|---|---|---|---|
| `agent` (and the default) | **0.8** ← today's constant | 0.4 | 1.0 |
| `user` | **0.9** | 0.5 | 1.0 (clamped) |

`USER_ASSERTED_UPLIFT = 0.1`, clamped to 1.0.

The direction is load-bearing. Demoting agent-derived *below* 0.8 would put all 94 legacy
rows above the new agent tier, so no agent-path claim could ever supersede a legacy claim
again — the substrate would freeze. Uplifting instead pins the agent tier to today's value.

### 2.4 What this changes — stated honestly (codex #2/#3 accepted)

My first draft called this "regression-free". That claim was too strong and I am dropping it.
Confidence is an **absolute veto** in `adjudicate` — it is tested before `update_kind` is
consulted, and a REJECTed claim is written immediately non-current with no later
reconsideration. So making the guard reachable necessarily makes its bad cases reachable too:

- **Intended:** an agent-derived claim can no longer clobber a user-asserted fact in the same
  slot. That is the whole point of the ticket.
- **Residual risk:** a genuine later user correction whose phrasing scores below the
  attribution floor is attributed `agent`, loses to a prior 0.9 claim, and is permanently
  retained as non-current while the stale fact stays current. Today it would have superseded.

What bounds it: only ~21 % of claims reach 0.9 at all, so most slots never hold a 0.9
incumbent; the losing claim is always retained as an audit row (recoverable); claims are
pull-only today, so a wrong current value arrives as a weighable tool result rather than
being pushed into context (ADR-0126 D5 scope note). **Mitigation added to the plan:** emit a
structured log on every REJECT carrying both attributions and overlap scores, so the
false-rejection rate is *measurable* rather than silent — ship observable, tighten later.

No *existing* supersession path regresses (the agent tier equals today's constant); what
changes is that a new, narrower REJECT path becomes live. That is the accurate statement.

### 2.5 Known limitations inherited, not introduced (codex #5/#6/#7)

- **Whole-set invalidation.** On SUPERSEDE, `assert_claim` invalidates *every* facet-matched
  claim, and matching is cosine similarity, not contradiction detection — so one
  high-authority claim can invalidate merely-similar neighbours. Pre-existing FRE-712 design
  (deliberate, for ≤1-current-per-slot self-healing). Out of scope; noted in the handoff.
- **Legacy rows carry no `asserted_by`.** Candidates are reconstructed from confidence only,
  so adjudication is unaffected; the 94 existing rows read as null = pre-FRE-1020. **No
  backfill** — rewriting historical rows for auditability alone is not worth the blast radius.
- **Embedder outage bypasses the guard entirely.** A zero-vector embedding matches nothing →
  FRESH regardless of authority (already covered by
  `test_claim_zero_vector_embedding_guard.py`). So "the guard is reachable" is true of the
  healthy path, not of an embedder outage. Stated in the handoff rather than claimed away.

### 2.6 Scope boundary

This makes the **D2 supersession guard reachable and live**. It does **not** implement D6's
corroboration/promotion gate (AC-9 (a)/(b)): that needs a source registry with ingest-time
trusted-source flags, and is genuinely separate, sequenceable, and ADR-requiring → **file a
Needs-Approval follow-up ticket**.

### 2.7 ADR posture

An **inline amendment note on ADR-0098 D6**, not a new ADR. D6's *decision* is unchanged
(co-authorship → trust); only the realization slot moves from `source_type` to a dedicated
Python-derived axis, because the named slot cannot carry it. Plus the note ADR-0100 is owed —
verified absent today: its "Adjacent decision context" (lines 70-73) still rests the
recency-gate demotion on "ADR-0098 now owns correctness-over-time", which was not true on
live data.

---

## 3. Acceptance criteria (the definition of done)

| # | Criterion | Proof |
|---|---|---|
| **AC-A** | On the production producer path, claim confidence takes ≥2 distinct values, determined by co-authorship | Producer-path test: one extraction payload with a user-grounded and an assistant-grounded claim yields two distinct confidences |
| **AC-B** | The D2 guard is live: an agent-derived claim in the same facet slot as a user-asserted current claim is **REJECTED**, not superseded | End-to-end test `_finalize_extraction` → `_build_claim` → `adjudicate` returns `REJECT` |
| **AC-C** | The `correction` supersession label is reachable: a user-asserted claim superseding an agent-derived one yields reason `"correction"` | Test asserts `reason == "correction"` |
| **AC-D** | The agent tier equals today's 0.8, so no existing supersession path regresses | Test against a legacy-shaped 0.8 candidate |
| **AC-E** | Authorship is durable and auditable on the node | Cypher/storage test: `asserted_by` persists on `:Claim` |
| **AC-F** | Authorship is never model-controlled (AC-9 principle) | Test: an extraction payload asserting its own `asserted_by: "user"` on an assistant-grounded claim is overridden to `agent` |
| **AC-G** | Every REJECT is observable | Test asserts the structured log fires with both attributions |
| **AC-H** | ADR-0100's false premise corrected; ADR-0098 D6 records the realized mechanism | Both notes in the diff |

Post-deploy (master's runbook, not a PR item): new claims show ≥2 distinct
`(asserted_by, confidence)` pairs; REJECT log volume reviewed.

---

## 4. Implementation steps

1. **`memory/weight.py`** — `AssertedBy = Literal["user", "agent"]`,
   `USER_ASSERTED_UPLIFT = 0.1`, and
   `KnowledgeWeight.from_claim_provenance(source_type, asserted_by)` returning the uplifted
   confidence clamped to 1.0. `from_source` untouched (entities still use it).
2. **`memory/models.py`** — `Claim.asserted_by: str = "agent"` + docstring stating it is the
   co-authorship axis, Python-derived, while `source_type` is the channel.
3. **`second_brain/entity_extraction.py`** — add `_attribute_claim_authorship(content,
   user_message, assistant_response)` (overlap scoring + the two threshold constants), thread
   `user_message`/`assistant_response` into `_finalize_extraction`, and stamp `asserted_by`
   per claim — **overwriting** any model-supplied value (AC-F). No prompt change.
4. **`second_brain/consolidator.py`** — `_build_claim` reads `asserted_by` and derives
   confidence via `from_claim_provenance`.
5. **`memory/service.py`** — persist `asserted_by` as a `:Claim` property; emit the
   structured REJECT log with both attributions (AC-G).
6. **Tests** (TDD — failing first): weight uplift; attribution scoring incl. the
   model-override case; `_build_claim` confidence varies; AC-B/AC-C/AC-D adjudication
   outcomes; cypher persistence; REJECT logging.
7. **Docs** — ADR-0098 D6 amendment note; ADR-0100 adjacent-context note.
8. **Follow-up ticket** — D6/AC-9 corroboration & promotion gate (Needs Approval).

Quality gates: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` ·
`pre-commit run --all-files` · code-review `high` (memory substrate) · security-review
(no input/subprocess/auth/network surface change — confirm at the gate).
