# FRE-916 phase 2 — delete the second catalog, provider-key concurrency, collapse the divergence matrix

**Ticket:** FRE-916 (In Progress, phase 2) · **ADR:** ADR-0121 §Sequencing step 1, AC-1(b) catalog half, AC-3
**Base:** `origin/main` @ `f35d78a1` (contains phase 1, PR #584) · **Branch:** `fre-916-adr-0121-t1-phase2`
**Review:** codex plan-review run 2026-07-19 (6 findings, all incorporated — see §Review outcomes)

## What phase 1 left

PR #584 normalized `config/models.yaml` into `providers:` + `models:` (deployments) + Layer-3
`bindings:` in `config/model_roles.yaml`, and proved the resolution snapshot byte-identical. It
deliberately retained `config/models.cloud.yaml`, `AGENT_MODEL_CONFIG_PATH`, and `ExecutionProfile`.

Master's gate comment (2026-07-19) scopes phase 2 to three items. This plan implements those plus the
`provider_type` field removal (owner decision D3).

**Out of scope — FRE-917:** `ExecutionProfile`, `resolve_model_key`, `config/profiles/{local,cloud}.yaml`.
The other half of AC-1(b), reassigned by the ADR amendment (PR #585): deleting it before the selection
store exists would remove the live Cloud pill with no replacement.

## Ground truth established by inspection

| Fact | Evidence |
|---|---|
| The two catalogs differ **only in comments** | `diff config/models.yaml config/models.cloud.yaml` — every hunk is a comment line |
| `providers[].max_concurrency` is **declared but never read** | only `ModelDefinition.max_concurrency` reaches `register_model` (`llm_client/client.py:115`) |
| Outer concurrency is keyed on **normalized endpoint**, not provider | `concurrency.py:199,219,245` |
| `infer_provider_type` is the endpoint-string inference to retire | `concurrency.py:48-72`, called at `:220` |
| `divergence: allowed` is **decorative** | `resolve_role_model_key` has no production caller for `primary`/`sub_agent`; those route via `resolve_role_target` + `resolve_profile_redirect` (`client.py:222`). Codex independently confirmed |
| `primary`/`sub_agent` `local:`/`cloud:` values are **already identical** | `config/model_roles.yaml:52-53` |
| **Embedding calls bypass the concurrency controller entirely** | `request_slot` has exactly one caller, `llm_client/client.py:237`; `memory/embeddings.py:133,427` calls the API directly |
| `APP_ENV: eval` collapses to `DEVELOPMENT` | `env_loader.py:44-52` recognizes only production/staging/test |

## Decisions (owner-approved 2026-07-19)

- **D1 — `required_secrets` re-keys to a new explicit `AGENT_DEPLOYMENT_PROFILE`.**
  `settings.deployment_profile: Literal["local","cloud","eval"] = "local"`, set explicitly in the two
  compose files. Replaces the implicit catalog-path-derived profile with a one-line declaration.
  *Rejected:* keying on `settings.environment` — `APP_ENV=eval` maps to `DEVELOPMENT`, so the eval
  stack would silently lose the anthropic/openai boot guard it has today (codex finding 2, verified).
  *Rejected:* adding `Environment.EVAL` — flips `security.py:283` and other `Environment` branches on
  the eval stack.
- **D2 — delete `config/models.benchmark-{4b,4b-f16,8b}.yaml`** and their driver
  `scripts/eval/fre435_memory_recall/run_embedder_benchmark.sh`. FRE-817-era one-offs; the embedder
  question is settled (OVH-managed 8B @ 1024).
- **D3 — remove `ModelDefinition.provider_type` in this PR** (owner overrode the deferral
  recommendation). All read sites route through `ModelConfig.placement_of`.
  **Verified safe, with one correction.** Every deployment declares `provider_type` explicitly
  (`models.yaml:102,132,147,163,180,207,223,239`) using only `local`/`cloud` — no deployment is
  `managed`, so `Placement`'s two values are sufficient and `infer_provider_type` is already dead for
  the real catalog. The single mismatch is `embedding`: `provider_type: "local"` against provider
  `ovh` (`placement: cloud`). Git shows this is **drift, not design** — commit `48ae7529` wrote
  `provider_type: "local"` + `max_concurrency: 1` for `Qwen3-Embedding-0.6B` on
  `http://localhost:8503/v1`, a single-threaded local GGUF container; phase 1 repointed the entry at
  OVH but carried both forward, parking them behind a comment that names *this* step as where they go.
- **D5 — provider ceilings apply to every provider; dispatch and concurrency split** (owner).
  `placement` decides dispatch (`LocalLLMClient` vs `LiteLLMClient`); the **provider ceiling** decides
  concurrency, for all providers — the `_needs_control` cloud bypass is removed. Cloud ceilings are set
  high enough to be a safety valve rather than a throttle (owner directive), and every cloud value
  remains strictly *more* constrained than today's unlimited passthrough.

  | Provider | Placement | Before | After |
  |---|---|---|---|
  | `slm_local` | local | 2 | **2** (the real GPU constraint; AC-3's subject) |
  | `ovh` | cloud | 1 | **50** |
  | `openai` | cloud | 10 | **50** |
  | `anthropic` | cloud | 20 | **50** |
  | `voyage` | cloud | 5 | **50** |

  The `embedding` deployment's own sub-limit rises `1 → 50` (provider governs) and its
  `provider_type: "local"` is deleted — precisely what phase 1's comment deferred to here.
- **D4 — `sessions.model_config_path` keeps its column**, sourced from the single-catalog path
  constant. No schema change; ADR-0121 step 4 migrates telemetry attribution properly.

## Review outcomes (codex, 2026-07-19)

| # | Finding | Disposition |
|---|---|---|
| 1 | Step 2 missed live `settings.model_config_path` consumers: `config/substrate.py:121-122`, `config/resolve.py:50` | **Accepted** — added to Step 2 |
| 2 | D1 as written breaks eval secret enforcement | **Accepted** — verified, drove the D1 correction above |
| 3 | Provider-keying is a semantic change (endpoint-capacity → provider-capacity), not a general equivalence | **Accepted** — declared as intentional; two discriminating tests added |
| 4 | AC-3 test underspecified: `_PrioritySemaphore.active` is read without the lock (`concurrency.py:128` vs `:146-174`); external polling races | **Accepted** — measurement moved inside the slot, barrier + lock |
| 5 | The `ovh: 1` risk claim is **false** — embeddings never acquire a slot | **Accepted** — claim removed; logged as a follow-up |
| 6 | Step 2's grep gate too narrow; Step 4 named a nonexistent test path | **Accepted** — grep widened, path corrected to `tests/test_llm_client/test_concurrency.py` |

## Steps

Steps 1–5 land in **one PR**. The ADR is explicit that shipping the catalog deletion without the pin
updates is a broken deploy, not an intermediate state.

### Step 1 — snapshot guard first (TDD safety net)

The phase-1 golden (`tests/personal_agent/config/test_catalog_snapshot.py`) is profile-keyed and skips
when `models.cloud.yaml` is absent (`:245-248`). Re-key it to the single catalog **before** deleting
anything, so the deletion is proven behaviour-preserving by a test that is actually running.

- Rewrite to a single-catalog snapshot: `(role) → fully-resolved ModelDefinition` across phase 1's four
  dimensions (resolved definition, concurrency registration, per-role timeout, pricing registration).
- **Do NOT rebaseline the golden file in this step.** Translate the new single-catalog cells onto the
  existing golden's `local|…` slice and assert equality against the *committed* golden. A deletion
  proven against an unrebaselined golden cannot launder a change; regenerating first would destroy
  exactly the evidence AC-1(a) rests on.
- **The snapshot is rebaselined ONCE, in Step 4, deliberately.** Two guarded cells change shape there
  by design: `provider_type` → `placement_of` (D3), and the `runtime.concurrency` dimension moves from
  endpoint keys to provider keys with new ceilings (D5). Both are *declared, enumerated* deltas —
  the same discipline phase 1 used for its four behaviour changes — and each must be listed
  individually in the commit that rebaselines. **Step 1's assertion must therefore be green against
  the untouched golden before Step 2 begins**, so the deletion and the concurrency rework are never
  proven by the same rebaseline.
- `provider_type` stays in `_BEHAVIOUR_FIELDS` through Steps 1–3 (it still exists), and is swapped for
  the derived placement only in Step 4, alongside its deletion.
- Negative control: perturb one `context_length` digit; must fail naming the affected cell.

→ verify: `make test-file FILE=tests/personal_agent/config/test_catalog_snapshot.py` green, and red
with the negative control applied (cell named).

### Step 2 — delete the catalog file and every pin, atomically

- Delete `config/models.cloud.yaml`; per D2 delete `config/models.benchmark-*.yaml` +
  `run_embedder_benchmark.sh`.
- `config/model_loader.py:32-34` — `_REAL_CATALOGS` collapses to the one path; introduce the single
  catalog-path constant that replaces the setting.
- `config/settings.py:902-904` — remove `model_config_path`; remove from the `:133` path-coercion
  validator. Check whether `protected_namespaces=()` (`:51`) is still needed by another `model_*` field
  before touching it.
- **`config/substrate.py:121-122`** *(codex finding 1)* — `resolve_substrate()` resolves
  `model_endpoint:<role>` rows (`config/substrate.yaml:52`) through `settings.model_config_path`.
  Repoint to the single catalog + bindings.
- **`config/resolve.py:50`** *(codex finding 1)* — the `config-resolve` CLI gets its path from
  `model_config_path_for_profile`, a manifest helper being retired. Repoint to the single catalog; its
  `--profile` argument loses meaning and goes with it.
- `config/deployment.yaml` — remove `model_config_path` + the `AGENT_MODEL_CONFIG_PATH` `env_overrides`
  from all three rows (the `compose_file` axis stays; substrate.yaml references it).
- `config_guard.py` — retire `check_deployment_manifest_internal_consistency` (`:898`),
  `_normalize_container_model_config_path` (`:859`), `_compose_model_config_paths` (`:871`),
  `model_config_path_for_profile` (`:415`); trim `check_deployment_manifest_matches_compose` (`:935`)
  to the compose-file-existence half; update `run_all_checks` (`:1008`).
- `docker-compose.cloud.yml:360`, `docker-compose.eval.yml:165,200`, `.env.example:275,731` — drop pins;
  add `AGENT_DEPLOYMENT_PROFILE` (D1) to the two compose files.
- Eval/study harnesses setting the env var: `fre435_memory_recall/{harness,ab_multipath,ab_relevance_bounded,separation_benchmark}.py`,
  `fre630_extraction_quality/{harness,fre771_v1_prompt_snapshot}.py`, `fre720_insights_separation/separation_probe.py`,
  `fre817_corpus_ab_embedder/run_corpus_ab.sh`, `scripts/study/config.py:63`.
- `model_loader.py` — `load_model_config` / `resolve_role_model_key` / `resolve_active_attribution` /
  `check_vision_capabilities` default to the constant (D4).
- `llm_client/client.py:83,91,99` — the `model_config_path` parameter defaults to the constant.
- `tests/test_config/test_model_loader.py:530` — the live regex scanning `docker-compose*.yml` for the
  pin inverts to assert **no** compose file declares it.

→ verify *(codex finding 6 — widened)*:
`rg -n 'models\.cloud\.yaml|AGENT_MODEL_CONFIG_PATH|benchmark-4b|benchmark-8b' src/ config/ scripts/ tests/ docker-compose*.yml .env.example`
returns zero hits. A second sweep for `model_config_path` over the same paths returns hits **only** for
the `sessions.model_config_path` DB column (D4). Then `uv run python scripts/check_config.py` exits 0.

### Step 3 — collapse the divergence matrix

- `config/model_roles.yaml` — delete `active_profiles:`; collapse every `roles:` row to a single value
  (`primary`/`sub_agent` `local:`/`cloud:` → `all:`, identical today so a resolution no-op). Re-key
  `required_secrets:` to the D1 profile names — `local: []`, `cloud:` and `eval:` both
  `[anthropic_api_key, openai_api_key]`, which is exactly today's enforcement for all three.
- `config_guard.py` — retire the divergence half of
  `check_forbidden_role_divergence_and_dangling_refs` (`:499-526`); **keep the dangling-reference half**
  (`:486-497`) re-pointed at the single catalog — that check still has teeth. Retire
  `resolve_active_profile` (`:443`).
- `model_loader.py:278-304` — the `allowed` branch and its `resolve_active_profile` call go; `forbidden`
  becomes the only path. Preserve raise-on-unknown-role and raise-on-key-absent (ADR-0099 D1: no silent
  fallback).
- `settings.py:2602-2643` — `enforce_required_secrets` keys on `config.deployment_profile`.
- Update matrix fixtures under `tests/personal_agent/config/fixtures/` declaring `active_profiles`/`cloud:`.

**Tests:** `deployment_profile=cloud` with `anthropic_api_key` unset raises naming the profile and the
field; `deployment_profile=local` with everything unset boots clean; `deployment_profile=eval` enforces
the same set as cloud (the regression codex finding 2 identified — asserted, not assumed).

→ verify: `make test-file FILE=tests/personal_agent/config/` green; `scripts/check_config.py` exits 0.

### Step 4 — provider-keyed concurrency (AC-3)

**This is an intentional semantic change** *(codex finding 3)*: the outer capacity axis moves from
*endpoint* to *provider*. These coincide for every deployment in today's catalog — the two `slm_local`
deployments share one provider and one endpoint — so live behaviour is preserved, but the change is not
a general equivalence and is declared as such.

- `concurrency.py` — delete `infer_provider_type` (`:48-72`) and `_LOCAL_HOSTS`. Replace
  `_endpoint_semaphores` (keyed on `_normalize_endpoint`) with `_provider_semaphores` keyed on provider
  **name**, sized from `providers[].max_concurrency`. `register_model` takes a provider name + ceiling
  instead of inferring from the endpoint; per-deployment `max_concurrency` stays the inner sub-limit.
  `get_status()` reports `{"models": …, "providers": …}`.
- `llm_client/client.py:113-117` — pass `provider=model_def.provider` and the provider's ceiling from
  the loaded `ModelConfig`; the cloud-passthrough decision reads `ModelConfig.placement_of`.
- **D3 — delete `ModelDefinition.provider_type`** (`models.py:240`). Route every read through
  `placement_of`: `factory.py:115,165` · `executor.py:1723,1738,1837,1852,1967` · `dspy_adapter.py:113`.
  `ExecutionProfile.provider_type` (`profile.py:179`, `error_classification.py:76`,
  `executor.py:1174`) is a **different** field and stays — FRE-917 owns it.
- `service/app.py:2265-2291` inference status — re-key only if it reads endpoint semaphores; ADR step 3
  owns the availability rework, keep this minimal.

**Tests** in `tests/test_llm_client/test_concurrency.py` *(codex finding 6 — corrected path)*, written
failing first:

- **AC-3.** Provider ceiling **3** — deliberately non-default; the corrected AC is emphatic that at the
  default `2` the test passes on the old endpoint-semaphore behaviour and proves nothing. Register
  `qwen3.6-35b-thinking` and `qwen3.6-35b-instruct` under `slm_local`, each with sub-limit 3, drive 6
  concurrent acquisitions, assert peak in-flight at the provider is exactly 3. A per-deployment-only
  implementation reaches 6.
  **Measurement** *(codex finding 4)*: `_PrioritySemaphore.active` is read without the lock
  (`:128` vs `:146-174`), so external polling of `get_status()` races — it can miss the transient peak
  or observe post-release. Instead increment/decrement an in-test counter **inside** `async with
  request_slot(...)`, guarded by an `asyncio.Lock`, with an `asyncio.Event` barrier holding all tasks in
  the body until every acquisition that can proceed has.
- **Sub-limit below ceiling** is respected.
- **Same provider, different endpoints share a cap** — the new semantics, asserted.
- **Different providers, same endpoint do not share a cap** — the old semantics, asserted gone.

→ verify: `make test-file FILE=tests/test_llm_client/test_concurrency.py` — AC-3 red before the
`concurrency.py` change, green after.

### Step 5 — docs

`docs/reference/CONFIG_INVENTORY.md` §6 · `config/model_roles.yaml` header · `config/AGENTS.md:82,124,237`
· `docs/guides/CLOUD_DEPLOYMENT.md:363` · `.env.example` (new `AGENT_DEPLOYMENT_PROFILE`).
ADR-0121 Status Update recording phase-2 delivery: AC-1(b)'s catalog half met, the `ExecutionProfile`
half remains with FRE-917.

### Step 6 — gates

`make test` (module then full) · `make mypy` · `make ruff-check` + `make ruff-format` ·
`pre-commit run --all-files` · **code-review at `high`** (resolution-path, deletion-heavy, concurrency) ·
**security-review** (touches `auth_env` credential resolution).

## Acceptance criteria

| AC | Proof |
|---|---|
| **AC-1(b), catalog half** | The widened `rg` sweep (Step 2 verify) returns zero hits across `src/ config/ scripts/ tests/ docker-compose*.yml .env.example`; the single-catalog snapshot equals the phase-1 `local` golden cell-for-cell, negative-controlled, with placement guarded in `provider_type`'s stead |
| **AC-1(b), profile half** | **Not this ticket** — FRE-917, per the ADR amendment |
| **AC-3** | Provider ceiling 3 (non-default), 2 deployments × 3 concurrent = 6 issued, peak in-flight at provider == 3, measured inside the slot under a lock. Plus sub-limit-below-ceiling, same-provider-different-endpoints, different-providers-same-endpoint. AC-3 red pre-change |
| **required_secrets preserved** | All three profiles asserted: `local` boots keyless, `cloud` and `eval` both raise on a missing anthropic/openai key |
| **Behaviour preserved** | `make test` green modulo the known `test_memory/test_structural_arm` live-substrate failure (confirmed failing on `main` at `763d571e`) |

## Risks

- **Broken deploy if Step 2 is partial.** Mitigated by the widened `rg` sweep, `scripts/check_config.py`
  (run by both CI and pre-commit), and the inverted compose regex test.
- **D3 widens the blast radius into `executor.py`.** Five read sites sit in the escalation logic, where
  phase 1's codex review already found live bugs (every plain turn taking the escalation branch). The
  snapshot guards resolution, not escalation routing — the `high` code-review pass in Step 6 is the real
  net here, and the escalation branch deserves explicit attention in it.
- **`config-resolve` CLI loses its `--profile` premise.** Retired rather than redesigned; if any runbook
  invokes it, that runbook needs updating (checked in Step 5).

## Follow-ups to file (not this ticket)

- **Embedding calls bypass the concurrency controller entirely** (`memory/embeddings.py:133,427` never
  acquire a slot). Both `providers.ovh.max_concurrency: 1` and the deployment's own
  `embedding.max_concurrency: 1` are therefore unenforced on the memory embedding path — a pre-existing
  gap this refactor makes visible but does not create. Worth a ticket to decide whether embedding should
  acquire provider capacity.
- Carried from phase 1's handoff, still unfiled: the fixed/unmeasured context-compression cap; the DSPy
  adapter's inability to authenticate to the CF-Access-gated tunnel plus `reflection_dspy`'s dead PRIMARY
  fallback; `reranker_fallback` declared at 8192 against a server serving 40960.
