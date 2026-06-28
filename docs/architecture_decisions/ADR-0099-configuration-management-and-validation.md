# ADR-0099 — Configuration Management & Validation (single-source role matrix · profile-divergence policy · cross-config validator)

**Status:** Proposed — 2026-06-28
**Date:** 2026-06-28
**Deciders:** Project owner (authorized 2026-06-28, FRE-644)
**Extends:** ADR-0007 (Unified Configuration Management — established `AppConfig` as the typed-scalar authority and "secrets in `.env` only"), ADR-0031 (Model Configuration Consolidation — made `models.yaml` the source of truth for model *identity*; this ADR fixes the drift that re-entered through *role assignment*)
**Related:**
- ADR-0044 (Provider Abstraction & Dual-Harness — the `local`/`cloud` execution profiles whose divergence this ADR governs)
- ADR-0033 (Multi-Provider Model Taxonomy & LiteLLM — the model-definition schema the role matrix points into)
- FRE-375 (test/eval substrate isolation — the substrate guard this ADR generalizes to *all* config)
- FRE-645 (eval model-config fidelity — the first concrete instance; its "evals run on prod config" requirement becomes a *derived* guarantee here)
- ADR-0087 / ADR-0098 (Memory Recall Quality — a correctly-pinned extractor is a recall-quality prerequisite, which is why the eval-fidelity slice blocks a valid FRE-491 baseline)

**Validation:** the config audit/inventory table (first deliverable); the per-criterion checks in *Verification / Acceptance Criteria* below; the existing FRE-375 substrate-guard pattern reused for the divergence/orphan/secret guards.

> ADR-0007 unified *how config is accessed*. ADR-0031 unified *where a model's identity lives*. Neither stopped configuration from **silently diverging across deployment profiles**, because the one thing both left duplicated — the *role→model assignment* — got copy-pasted into the header of every role-bearing model YAML. This ADR removes that duplication at the root (one generative role matrix that makes role-*assignment* drift structurally impossible, with the residual model-*definition* drift caught by a guard and closed by consolidation) and adds the **cross-config validator** neither prior ADR provided.

---

## Context

**What is the issue we're addressing?**

Configuration is sprawled across many surfaces with **no single source of truth for role assignment and no cross-validation**. The 2026-06-28 audit (FRE-644) found, in this repo:

| Surface | What it holds | Count |
|---|---|---|
| `src/personal_agent/config/settings.py` (`AppConfig`) | ~150 typed scalar params + paths | 1 |
| Model-definition YAMLs **with role-assignment headers** | role headers (`entity_extraction_role:` …) **+** model defs | **4** — `models.yaml`, `models.cloud.yaml`, `models.eval.yaml`, `models-baseline.yaml` |
| Model-definition YAML **without role headers** | model defs only | **1** — `models.medium.yaml` |
| Model **policy** YAML (different schema) | per-mode role constraints (ADR-0005) — *not* model defs | **1** — `config/governance/models.yaml` |
| `config/profiles/{local,cloud}.yaml` | ADR-0044 profile (swaps `primary`/`sub_agent` only) | 2 |
| `config/governance/*.yaml` | budget, modes, safety, tools | 5 |
| `.env.example` (35 KB), `mcp-secrets.env` | secrets + env overrides | 2 |
| `docker-compose*.yml` service `environment:` | per-deploy env | **4** — `docker-compose.yml`, `.cloud.yml`, `.eval.yml`, `.test.yml`; only `.cloud.yml` and `.eval.yml` set `AGENT_MODEL_CONFIG_PATH` |

### The recurring drift, root-caused

The same conceptual assignment — "which model does entity-extraction use?" — is **redeclared independently in the header of each role-bearing model YAML** (four of them). With four copies, divergence is structurally inevitable. The audit found it already happened, **undeclared and unintended**:

| role | `models.yaml` (local) | `models.cloud.yaml` (**prod**) | `models.eval.yaml` / baseline |
|---|---|---|---|
| `entity_extraction_role` | `gpt-5.4-nano` | **`gpt-5.4-mini`** | `claude_sonnet` |
| `captains_log_role` | `gpt-5.4-nano` | **`claude_sonnet`** | `claude_sonnet` |
| `insights_role` | `gpt-5.4-nano` | **`claude_sonnet`** | `claude_sonnet` |
| `primary` / `sub_agent` | local SLM | `claude_sonnet` / `claude_haiku` | — |
| `embedding` / `reranker` | consistent | consistent | consistent |

This is exactly the failure mode ADR-0031 named ("config drift started") and tried to end — it re-entered one layer up, at *role assignment* instead of *model identity*.

### Two distinct kinds of divergence — only one is legitimate

- **The inference brain** (`primary`, `sub_agent`) *should* differ by profile: local runs the SLM; cloud runs Claude. Deployment-forced, intentional.
- **The cognitive pipeline** (`entity_extraction`, `captains_log`, `insights`, `compressor`, `embedding`, `reranker`) writes *durable* artifacts into the shared substrate (the KG, Captain's Log). If local writes nano-quality entities and cloud writes mini-quality ones into the **same Neo4j**, the store is silently inconsistent, and **a measurement taken under one profile does not describe the other** — which is why an eval on the default config measures the *wrong extractor* (FRE-645).

### What is missing

1. **No central, intent-declaring home for role assignment** — so "may this role differ across profiles?" has no answer a tool can check.
2. **No cross-config validator** — nothing catches undeclared divergence, orphan `.env` keys, type mismatches, missing-required params, or secret leakage. ADR-0007's "fail fast" only validates a single `AppConfig` instance in isolation; it cannot see *across* profiles or files.
3. **No deployment-provenance trail** — which model YAML is live depends on which `docker-compose` file was deployed (`docker-compose.cloud.yml` sets `AGENT_MODEL_CONFIG_PATH: …/models.cloud.yaml`). The value *is* in the repo, but answering "is prod extraction nano or mini?" still required forensics because nothing ties *profile → compose file → active YAML* together in one readable place.

The owner has authorized an ADR to decide (1) the single source of truth and what legitimately lives where, (2) the profile-divergence policy, and (3) the validation layer. The parameter-manager **UI** ("PWA Config Console") is **explicitly deferred** — this ADR is the config model + validator, not the UI.

---

## Decision

We make four decisions. Together they move config from *documented intent* (ADR-0007/0031, which drift defeated) to *structurally enforced intent*.

### D1 — One generative role matrix is the single source of truth for role assignment

Introduce `config/model_roles.yaml` as the **one and only** hand-edited place a role→model assignment is declared, per profile, with explicit divergence intent:

```yaml
# config/model_roles.yaml — the authoritative role matrix (D1)
roles:
  # inference brain — divergence is legitimate and declared
  primary:            { divergence: allowed,   local: primary,   cloud: claude_sonnet }
  sub_agent:          { divergence: allowed,   local: sub_agent, cloud: claude_haiku }

  # cognitive pipeline — one model for EVERY profile, eval included
  entity_extraction:  { divergence: forbidden, all: gpt-5.4-mini }
  captains_log:       { divergence: forbidden, all: claude_sonnet }
  insights:           { divergence: forbidden, all: claude_sonnet }
  compressor:         { divergence: forbidden, all: claude_haiku }
  embedding:          { divergence: forbidden, all: qwen3-embedding-0.6b }
  reranker:           { divergence: forbidden, all: qwen3-reranker }
```

- `divergence: allowed` → the role *may* take a different model per profile; the per-profile values are declared inline.
- `divergence: forbidden` → the role resolves to **one model across all profiles, including eval**. Stating a per-profile value for a `forbidden` role is itself a config error the guard rejects.

**Generative, not merely validated (per owner: make drift impossible, not just caught).** The model loader resolves each role *directly from the matrix* at runtime; the role-assignment headers (`entity_extraction_role:`, `captains_log_role:`, `insights_role:`, …) are **removed from the per-profile YAMLs entirely**, and the runtime callers (the consolidator, Captain's Log, insights, compressor) read their role *only* through the matrix-backed loader — so deleting or corrupting `config/model_roles.yaml` makes them fail deterministically rather than silently falling back to a `ModelConfig` field default. There is then exactly one hand-edited place a role *assignment* can live.

**Two drift layers — be precise about what this eliminates.** A matrix keyed by a model *name* eliminates **assignment** drift (which role → which name), but it does **not**, on its own, eliminate **definition** drift while more than one model-definition file exists: the same key resolves to *different real models* across files today — `gpt-5.4-nano` carries `id: gpt-5.4-nano` in `config/models.yaml` but `id: gpt-4o-mini` in `config/models.eval.yaml`. So:
- **Assignment drift** → structurally impossible the moment the generative loader lands (D1, stage 2): one source, headers gone.
- **Definition drift** → *caught* immediately by the guard (D4) — for every `forbidden` role it compares the **fully-resolved `ModelDefinition`** (id + provider + sampling params), not just the role's name-key, across all active profile definition files and fails on a mismatch — and made *structurally impossible* only when the model-definition files are consolidated to a single source (staged-delivery stage 4).

Model **definitions** (id, endpoint, sampling params, token caps) continue to live in the model-definition file(s) per ADR-0031/0033 until that consolidation; only the role→model *mapping* centralizes in stage 2.

**`forbidden` makes "evals run on prod config" a derived property.** Because cognitive-pipeline roles resolve to the same model in every profile, an eval automatically uses the prod extractor — FRE-645's core requirement falls out of D1 rather than needing a bespoke rule. A deliberate nano-vs-mini A/B remains possible only via an **explicit, logged opt-in** that overrides a `forbidden` role; the guard fails an eval that diverges a `forbidden` role *without* that recorded opt-in.

### D2 — Hybrid registry: AppConfig stays scalar authority; a thin registry declares only what pydantic cannot

Per ADR-0007, `AppConfig` remains the authority for the ~150 typed scalar params — we do **not** duplicate them into a second file. The new **registry** is exactly the three things pydantic cannot express, and nothing more:

1. **The role matrix** (D1) — role × profile × divergence intent.
2. **A deployment-provenance manifest** — `profile → compose file → active model-definition file → env overrides`, so "which extractor does prod run?" is answerable from one committed file (`config-resolve --profile cloud --role entity_extraction`) with zero container forensics.
3. **A secret inventory** — but **derived, not hand-maintained**, so it cannot become the very second-surface this ADR fights. The secret fields already live in `AppConfig` (`anthropic_api_key`, `openai_api_key`, `linear_api_key`, `r2_secret_access_key`, `artifact_resolve_internal_token`, …). The guard identifies them from field metadata (a `secret: true` `json_schema_extra` marker added to those fields, or migration to pydantic `SecretStr`) — there is no separately-edited key list — and asserts no such field's value is ever committed to a YAML or `.env.example`.

Everything else the validator needs it **derives** by introspecting `AppConfig` and scanning `.env.example` — there is no third place that can drift from the pydantic field definitions. The registry is therefore *additive only* over what pydantic already expresses: the role matrix and the provenance manifest, which pydantic genuinely cannot represent.

### D3 — Profile-divergence policy (the rule the matrix encodes)

- **Cognitive-pipeline roles are consistent across profiles** (`divergence: forbidden`), because they write durable artifacts into shared substrate and are the unit of measurement.
- **Inference-brain roles may diverge** (`divergence: allowed`), because deployment forces it (local has no cloud-API access; cloud has no local GPU).
- **Evals/measurements run on the prod cognitive-pipeline config** — a consequence of `forbidden`, enforced for eval profiles, overridable only by a logged opt-in.
- **Any other cross-profile divergence is undeclared and is a guard failure.** "Allowed" is an allow-list, not a default.

### D4 — Tiered cross-config validator (generalizes the FRE-375 substrate guard)

A single validator runs on three surfaces and classifies findings into two severities:

| Class | Examples | Startup | CI / pre-commit |
|---|---|---|---|
| **Safety** | missing secret **required by the active profile** · type mismatch · `TEST` env → prod-fingerprint URI (FRE-375) · committed secret value · `forbidden`-role resolving to a dangling/mismatched `ModelDefinition` | **hard-fail boot** | **hard-fail** |
| **Policy** | undeclared `forbidden`-role divergence · orphan `.env` key · provenance-manifest ≠ actual compose · undocumented `AppConfig` field | **warn-loud (boots)** | **hard-fail** |

**"Required secret" is per-profile, not global.** Most secrets are optional in `AppConfig` (e.g. `openai_api_key: str | None`) because they are only needed under some profiles. The provenance manifest declares, per profile, which secrets are *required* (e.g. `cloud` requires `anthropic_api_key` + `openai_api_key`; `local` requires neither). The startup safety check fails only on a secret required by the **currently-active** profile — so a missing cloud key never wedges local-dev boot, and a cloud deploy missing its key never boots silently degraded.

Rationale (guardian principle — *prod down is a failure*): a policy nit (e.g. an orphan `.env` key) must never wedge a production boot, but it **must** block a merge. Safety violations — the FRE-375 class — block both. **Any new or unclassified finding defaults to *policy* (warn), never *safety*** — so a future check can never accidentally gain the power to wedge a boot. CI/pre-commit is the universal gate; startup is the last-line safety net for anything that bypassed CI (e.g. a hand-edit on the VPS).

### Rule of thumb (supersedes the scattered rules-of-thumb in ADR-0031 and `config/AGENTS.md`)

> **"Which model does role X use, and may it differ by profile?"** → `config/model_roles.yaml` (the matrix)
> **"What are model X's parameters (id, endpoint, tokens)?"** → the model-definition file (ADR-0031)
> **"What's the value of typed param Y?"** → `AppConfig` default, overridable by `AGENT_Y` env
> **"What is my API key / how much can I spend / is feature Z on?"** → `.env` (secret / operational / flag)
> **"Which config is live in prod?"** → the provenance manifest + `config-resolve` CLI

### Staged delivery (owner: "I want both — consolidation *and* guard — but understand the separation")

The ADR **decides** the consolidated end-state now; it **ships** as sequenced children, one concern per PR. A guard cannot be "green and meaningful" with nothing to check against, so **stage 1 lands the divergence *policy* (the matrix's intent layer) together with the guard, and corrects the current drift in the same PR** — it does not land a guard that is either red on day one or a vacuous no-op:

1. **Guard + divergence policy + drift correction** (D4 + the intent layer of D1) — introduce `config/model_roles.yaml` carrying only the **intent** (`divergence: allowed|forbidden` per role) and the guard checking the *existing four role-bearing YAMLs* against it (a `forbidden` role must resolve to the same `ModelDefinition` across all active profiles). Because today's state violates this (local extraction `nano` ≠ prod `mini`), the **same PR aligns the local `forbidden` roles to prod** so the guard lands green. Role headers still physically exist after this stage but are now guarded; state is frozen and corrected.
2. **Generative loader** (the mechanism half of D1) — the loader resolves roles *from the matrix*; role headers are removed from the profile YAMLs; the runtime callers read only via the matrix. Assignment drift becomes structurally impossible. (Guard from stage 1 protects against regressions during this migration.)
3. **Provenance manifest + `config-resolve` CLI** (D2.2) — plus the guard's manifest-vs-compose cross-check.
4. **Consolidate / retire redundant model-definition YAMLs** (`models.eval.yaml`, `models-baseline.yaml`, `models.medium.yaml`) into a single definition source subsumed by the matrix + profiles — gated green by the guard. This stage closes **definition** drift (the `nano`→`gpt-4o-mini` case), the half stage 1's guard only *catches*.

---

## Alternatives Considered

### Option 1: Documentation only — keep the role headers, add cross-references
**Description:** Note in each YAML "keep roles in sync with the others."
**Pros:** Zero code; lowest risk.
**Cons:** This is precisely what ADR-0031 already tried ("Alternative A: keep split but add documentation") and what *let this drift happen* — four hand-maintained copies of the role assignment. Documentation does not prevent recurrence.
**Why Rejected:** The owner's stated problem is *recurring* drift; a remedy that already failed once is not a remedy.

### Option 2: Validate-only — keep matrix *and* per-profile headers, guard catches disagreement
**Description:** Add `model_roles.yaml` but leave the role headers in the YAMLs; the guard hard-fails CI if they disagree.
**Pros:** Smaller loader change; no migration of the loader's resolution path.
**Cons:** Drift is *caught*, not *prevented* — humans still maintain the assignment in two places, and a CI-bypassing hand-edit on the VPS diverges at runtime until the next CI run (which may be never for a hotfixed container).
**Why Rejected:** Owner chose the generative model explicitly: one source, drift structurally impossible. Validate-only keeps two sources.

### Option 3: One standalone registry for *every* parameter
**Description:** A single `config/registry.yaml` declaring all ~150 params × default × owner × divergence; `AppConfig` and YAMLs validated against it.
**Pros:** The audit table becomes one living file.
**Cons:** A second authority for the typed scalars that can itself drift from the pydantic definitions — re-creating the very duplication problem one level wider. Contradicts ADR-0007.
**Why Rejected:** The registry should hold only what pydantic *cannot* (D2). Duplicating 150 typed fields trades one drift surface for a bigger one.

### Option 4: Collapse all model YAMLs into `AppConfig`/`.env`
**Description:** Move model identity + roles into flat env vars; abolish the YAMLs.
**Pros:** One config system.
**Cons:** Already rejected by ADR-0031 ("Alternative B") — model identity needs structured per-model sampling params, endpoints, and provider dispatch that flat key=value cannot express.
**Why Rejected:** Settled by ADR-0031; nothing has changed.

---

## Consequences

### Positive Consequences
- Role-*assignment* drift (which role → which model name) becomes **unrepresentable** once the generative loader lands (stage 2): one matrix entry, resolved everywhere, no header to diverge. Model-*definition* drift (the `gpt-5.4-nano`→`gpt-4o-mini` case) is **caught by the guard from stage 1** and made unrepresentable by the stage-4 definition consolidation.
- "Evals run on prod config" (FRE-645) is a **derived guarantee**, not a rule someone must remember.
- "Which model is live in prod?" is answerable from committed files in one command — no container forensics.
- The FRE-375 substrate guard generalizes to *all* config, on a tiered severity model that protects prod uptime.
- A/B testing a cognitive-pipeline model is now a *deliberate, logged* act — visible, not a silent default.
- Adding a new role is one matrix line + one guard pass; the "next person instinctively adds it to the wrong file" failure (ADR-0031's worry) is gone — there is only one file.

### Negative Consequences
- The model loader's resolution path changes (D1 migration) — a real, if contained, code change to a hot path; mitigated by guard-first sequencing and the existing `load_model_config` test surface.
- Three model YAMLs are retired — any external script reading them directly breaks (audit will enumerate readers).
- A deliberate nano-vs-mini eval now requires an explicit opt-in flag — slightly more friction for an intentional experiment (by design).

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Loader migration changes the model resolved in prod (silent behavior change) | **High** | Guard-first freezes current state; the migration PR includes a test asserting each profile resolves the **same** model it did pre-migration (a snapshot/golden test), so the matrix encodes today's *intended* values, not an accidental change. Cloud's `forbidden` roles are the intended prod values (mini, sonnet); local's nano values are the *bug* being corrected — the correction is explicit and reviewed, not incidental. |
| A `forbidden`-role correction silently re-points local extraction from nano → mini, raising local cost/latency | Medium | The correction is the *point* of the ADR and is owner-visible in the matrix PR; local cost impact is bounded (extraction is a background path) and surfaced in the migration ticket. |
| Startup hard-fail on a mis-classified finding wedges prod boot | Medium | Severity classification is explicit (D4 table) and unit-tested both ways (a policy violation MUST boot; a safety violation MUST NOT). Default for any *new/unclassified* finding is **policy** (warn), never safety. |
| Provenance manifest itself drifts from the compose files | Medium | The guard cross-checks the manifest against the actual `docker-compose*.yml` `environment:` blocks; a mismatch is a policy failure (CI-blocking). |
| Registry (matrix) drifts from model-definition files (role points at a non-existent model) | Medium | Guard resolves every matrix entry against the model-definition file and fails on a dangling reference (safety class — it would break a real call). |

---

## Implementation Notes

**Files affected (indicative — the audit ticket produces the authoritative list):**
- **New (repo root):** `config/model_roles.yaml` (matrix), `config/deployment.yaml` (provenance manifest), `scripts/check_config.py` (the guard, mirroring `scripts/check_no_direct_substrate_in_tests.py`).
- **New (`src/personal_agent/`):** `src/personal_agent/config/resolve.py` (the `config-resolve` CLI, `python -m personal_agent.config.resolve`).
- **Modified:** `src/personal_agent/config/model_loader.py` (resolve roles from the matrix), `src/personal_agent/llm_client/models.py` (`ModelConfig` — drop the role-default fallback so absence fails loudly), `src/personal_agent/config/settings.py` (tiered-validator hook at startup; `secret: true` field metadata), `.pre-commit-config.yaml` (add the guard), the role-bearing model YAMLs (strip role headers).
- **Retired (stage 4):** `config/models.eval.yaml`, `config/models-baseline.yaml`, `config/models.medium.yaml` — consolidated into a single model-definition source, guard-gated.

**Migration steps:** guard-first → matrix+loader (with golden resolution test) → provenance → YAML retirement. No database migration. No Alembic (project policy).

**Testing strategy:** unit tests for the guard against deliberately-broken fixtures (divergent `forbidden` role, orphan `.env` key, committed secret, dangling model ref); a golden test that each profile resolves the same model post-migration as the intended pre-migration value; two tiered-startup tests (policy warns+boots, safety raises).

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?** Each criterion is outcome-level and able to fail; today's repo state fails AC-1, AC-2, AC-5. The new artifacts each AC exercises are part of this ADR's delivery scope: the guard `scripts/check_config.py` (mirroring `scripts/check_no_direct_substrate_in_tests.py`), the matrix `config/model_roles.yaml`, the provenance manifest `config/deployment.yaml`, the `config-resolve` CLI (`uv run python -m personal_agent.config.resolve …`), and pytest fixtures under `tests/personal_agent/config/fixtures/`.

- **AC-1 — Forbidden roles resolve to one *ModelDefinition* across every active profile, on the real call path.** **Check:** a pytest that, for **each** `forbidden` role, constructs the actual runtime consumer under each active profile and asserts the **fully-resolved `ModelDefinition` (id + provider + sampling params)** it uses is identical across profiles — not merely the role's name-key. The consumer set must cover every `forbidden` role: `entity_extraction` (consolidator extractor), `captains_log` (`CaptainLogManager`), `insights` (insights engine), `compressor` (context compressor), `embedding` (memory embedding service), `reranker` (memory reranker). **Fails if** extraction resolves `id: gpt-5.4-nano` under local and `id: gpt-5.4-mini` under cloud (present assignment bug), **or** the name-key matches but the resolved id differs (`gpt-5.4-nano`→`gpt-4o-mini` definition bug). A standalone helper-only test does **not** satisfy this — the consumer's real path must be exercised.

- **AC-2 — One hand-edited home for role assignment, with no silent fallback (generative).** **Check (two parts, both required):** (a) `grep -rEl '^(entity_extraction|captains_log|insights|compressor|embedding|reranker)_role:' config/*.yaml` returns nothing — the only declaration site is `config/model_roles.yaml`; **and** (b) a pytest that renames/empties `config/model_roles.yaml` and asserts the runtime consumers from AC-1 **raise** (deterministic failure), rather than falling back to the `ModelConfig` field defaults in `src/personal_agent/llm_client/models.py`. **Fails if** a profile YAML still declares a role, **or** the loader silently substitutes a default when the matrix is absent (the existence-check-only loophole).

- **AC-3 — The guard fails CI on a deliberately-divergent fixture.** **Check:** `uv run python scripts/check_config.py --root tests/personal_agent/config/fixtures/divergent_forbidden_role/` exits non-zero and names the offending role; the same script exits zero on the real repo (post stage-1 correction). **Fails if** the guard passes the known-bad fixture (the FRE-375-style proof — a broken impl must fail it).

- **AC-4 — Orphan `.env` keys are caught.** **Check:** `scripts/check_config.py` run against a fixture `.env.example` containing one `AGENT_*` key mapping to no `AppConfig` field flags exactly that key (and flags none on the real `.env.example`). **Fails if** the planted orphan passes, or a real (mapped) key is falsely flagged.

- **AC-5 — Prod config is answerable from committed files alone.** **Check:** `uv run python -m personal_agent.config.resolve --profile cloud --role entity_extraction` reads only committed files (matrix + `config/deployment.yaml` + model-defs) — no running container, no `docker exec` — and prints `gpt-5.4-mini`; separately the guard asserts every `AGENT_MODEL_CONFIG_PATH` set across `docker-compose.cloud.yml` and `docker-compose.eval.yml` matches the manifest's `profile → compose → yaml` rows. **Fails if** answering requires container introspection, or a compose `AGENT_MODEL_CONFIG_PATH` disagrees with the manifest.

- **AC-6 — Startup enforcement is tiered and profile-aware.** **Check:** three pytests — (a) a *safety* violation (`environment=TEST` + prod-fingerprint URI) makes `AppConfig`/validator **raise** at startup; (b) a *policy* violation (a planted orphan `.env` key) **boots** and emits a `WARNING`-level structured log naming the finding; (c) a secret **required by the active profile** but unset (e.g. `anthropic_api_key` unset while the resolved profile is `cloud`) **raises**, while the same secret unset under the `local` profile **boots**. **Fails if** a policy nit blocks boot, a safety violation boots silently, or the required-secret check is global rather than per-active-profile.

- **AC-7 — Eval fidelity holds and is loud (the FRE-645 seam).** The eval discriminator is **`AGENT_MODEL_CONFIG_PATH`**, *not* an `Environment` value (`APP_ENV=eval` falls through to development today — there is no eval enum). **Check:** the FRE-435 recall harness, run with its pinned eval model-config, resolves every cognitive-pipeline role to the **same `ModelDefinition` as the `cloud` profile** and emits a startup log line stating the resolved `entity_extraction` id; the guard fails an eval model-config that diverges a `forbidden` role from `cloud` unless an explicit opt-in marker (e.g. `divergence_opt_in: [entity_extraction]`) is present and the divergence is logged. **Fails if** the harness silently runs `gpt-5.4-nano`/`gpt-4o-mini` while cloud runs `gpt-5.4-mini`, or a `forbidden`-role override is accepted without the logged opt-in.

- **AC-8 — No committed secret values.** **Check:** `scripts/check_config.py` run against a fixture committing a real value for an `AppConfig` secret-marked field (e.g. `AGENT_OPENAI_API_KEY=sk-live-…`) in a YAML or `.env.example` exits non-zero (safety class); the real repo passes. **Fails if** a secret value commits clean. The secret set is derived from `AppConfig` field metadata, so adding a new secret field auto-extends coverage without editing the guard.

- **AC-9 — Dangling / mismatched `ModelDefinition` references are rejected.** **Check:** a matrix entry whose model name is absent from the active profile's model-definition file makes the guard fail (safety — it would break a live call). **Fails if** a dangling reference passes the guard.

**Seam owner (decomposed ADR):** the *assembled* intent — "config has one source of truth for role assignment (drift structurally impossible), definition drift is closed, and the tiered validator gates it on startup + CI + pre-commit" — holds only once stages 1–4 all land. **No single child proves it.** The seam is owned at the **master integration gate on stage 4 (definition-YAML consolidation)**: master runs, on the assembled branch, (1) `uv run python scripts/check_config.py` green against the real repo, (2) the AC-2(a) grep returning zero role headers **and** the AC-2(b) deterministic-failure test passing, (3) `config-resolve --profile cloud --role entity_extraction == gpt-5.4-mini` from committed files only, and (4) AC-1 green (one resolved `ModelDefinition` per forbidden role across profiles). The ADR does not move to *Implemented* until all four pass together. **Stage 1 alone** (guard + correction) earns *Implemented (partial)* at most — assignment drift is still only *caught*, not yet *impossible*.

---

## References

- ADR-0007 — Unified Configuration Management (`AppConfig` authority; "secrets in `.env` only")
- ADR-0031 — Model Configuration Consolidation (`models.yaml` as model-identity SoT; named the drift this ADR finishes closing)
- ADR-0033 — Multi-Provider Model Taxonomy & LiteLLM (model-definition schema)
- ADR-0044 — Provider Abstraction & Dual-Harness Design (the `local`/`cloud` profiles)
- FRE-375 — Isolate test/eval scripts from production substrate (the guard pattern generalized here)
- FRE-644 — this ADR's authoring ticket (audit/inventory is its first deliverable)
- FRE-645 — Eval model-config fidelity (first concrete instance; its requirement derived from D1)
- `src/personal_agent/config/AGENTS.md` — existing config-module guidance (its rules-of-thumb are superseded by the Rule of Thumb above)
- `scripts/check_no_direct_substrate_in_tests.py` — the FRE-375 guard the validator mirrors

---

## Status Updates

### 2026-06-28 — Proposed
**Changed By:** adr session (Opus), FRE-644
**Reason:** Initial authoring. Decisions settled with owner: generative single-source role matrix (D1); hybrid registry — AppConfig stays scalar authority (D2); profile-divergence policy (D3); tiered validator (D4); staged delivery; new "Configuration Management" Linear project.

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
