# FRE-916 — ADR-0121 T1: catalog and providers refactor

**Backing ADR:** ADR-0121 §Decision 1–2, §Sequencing step 1
**Branch:** `fre-916-adr-0121-t1-catalog-providers`
**Scope decision (owner, 2026-07-19):** `ExecutionProfile` is NOT deleted in this ticket — deleting it
would break the harness (the live Cloud pill loses Sonnet with no replacement until step 5). AC-1(b) is
split: the catalog half lands here, the `ExecutionProfile`/`resolve_model_key` half moves to FRE-917.

---

## 1. What ships

Three layers in one catalog, plus every consumer of the deleted path moved in the same PR.

| Layer | File | Contents |
|---|---|---|
| 1 — Providers | `config/models.yaml` `providers:` | base_url (`${VAR}`-resolvable), auth, placement, max_concurrency |
| 2 — Deployments | `config/models.yaml` `models:` | one entry per deployment, keyed by model alias; kind, limits, capabilities, cost, sub-limit, decoding defaults, summary |
| 3 — Role bindings | `config/model_roles.yaml` `roles:` | deployment ref + per-use effort/decoding overrides + `open` flag |

**Deleted:** `config/models.cloud.yaml`, `AGENT_MODEL_CONFIG_PATH`, `settings.model_config_path`,
`active_profiles`, `divergence`, `infer_provider_type`, the `reasoning_heavy` and `gpt-5.4-nano`
deployments, `config/deployment.yaml`'s `model_config_path`/`env_overrides` and their two guards.

---

## 2. Resolution model (the load-bearing change)

Today two resolvers disagree about who owns role→key:

- `resolve_role_model_key(role)` (matrix) — covers the writer roles only
- `resolve_model_key(role)` (profile) — covers `primary`/`sub_agent`/`artifact_builder` only, and
  returns `role_name` unchanged when no profile is active

The second half is why the catalog cannot simply be re-keyed: with no profile active,
`resolve_model_key("primary")` returns the literal `"primary"`, which stops being a catalog key the
moment deployments are keyed by model.

**After:**

```
resolve_role_model_key(role)  →  Layer-3 binding default            # ALL roles, single source
resolve_model_key(role)       →  profile override if active, else resolve_role_model_key(role)
```

`resolve_model_key` survives unchanged in signature and keeps its call sites (`client.py:203`,
`factory.py:105`, `executor.py` ×5). Only its fallback changes: `role_name` → the binding default.
This is what makes the catalog re-key safe while ExecutionProfile is still live, and it leaves FRE-917
a single seam to cut rather than a scattered one.

`config/profiles/{local,cloud}.yaml` are updated to reference real deployment keys instead of
slot-aliases — ADR-0121 §5's "replacing the local profile's `sub_agent` slot-alias reference with a
real model key", done here because the re-key forces it.

---

## 3. Catalog shape

```yaml
providers:
  slm_local:       { base_url: "https://slm.example.com/v1", auth: none, placement: local, max_concurrency: 2 }
  embeddings_local:{ base_url: "${AGENT_EMBEDDINGS_BASE_URL}", auth: none, placement: local, max_concurrency: 1 }
  openai:          { auth_env: AGENT_OPENAI_API_KEY,    placement: cloud, max_concurrency: 10 }
  anthropic:       { auth_env: AGENT_ANTHROPIC_API_KEY, placement: cloud, max_concurrency: 20 }
  voyage:          { base_url: "https://api.voyageai.com/v1", auth_env: AGENT_VOYAGE_API_KEY, placement: cloud, max_concurrency: 5 }
```

| Deployment key | was | provider | kind | note |
|---|---|---|---|---|
| `qwen3.6-35b-thinking` | `primary` | slm_local | llm | ctx 131072 (served 132k) |
| `qwen3.6-35b-instruct` | `sub_agent` | slm_local | llm | **ctx 65536** — served truth; 32768/16384 both under-declared |
| `claude_sonnet` | same | anthropic | llm | |
| `claude_haiku` | same | anthropic | llm | |
| `gpt-5.4-mini` | same | openai | llm | |
| `qwen3-embedding-0.6b` | `embedding` | embeddings_local | embedding | dims 1024 |
| `voyage-rerank-2.5` | `reranker` | voyage | reranker | |
| `qwen3-reranker-4b` | `reranker_fallback` | slm_local | reranker | ctx 8192 unchanged — out of scope |
| — | ~~`reasoning_heavy`~~ | | | deleted per ticket |
| — | ~~`gpt-5.4-nano`~~ | | | deleted (owner: nano retired; compressor was the missed binding) |
| — | ~~`compressor`~~ | | | was a role, not a model — becomes a binding on `gpt-5.4-mini` |

Role bindings carry the per-use params that were wrongly on the model: `sub_agent` keeps
`max_tokens: 2048` + `disable_thinking`, `compressor` keeps `max_tokens: 512`, `entity_extraction`
keeps `temperature: 0.0`.

---

## 4. Steps

Each step names its verification.

**S1 — Snapshot harness (first, on main's behaviour).**
`tests/personal_agent/config/test_catalog_snapshot.py` captures, for every role × {local, cloud},
**five** dimensions — a definition-only snapshot passes while live behaviour changes (codex finding):
1. fully-resolved definition (id, provider, endpoint, decoding, limits)
2. **concurrency registration** — which semaphore key + limit each role acquires
3. **effective timeout per `ModelRole`** — the `_role_timeouts` value actually applied
4. **pricing registration** — the `litellm.model_cost` entries produced
5. **substrate `model_endpoint:<role>`** resolution

Generated on `main` BEFORE any change, golden committed in its own commit so the diff is reviewable.
*Verify:* `make test-file FILE=tests/personal_agent/config/test_catalog_snapshot.py`

**S2 — Schema.** `llm_client/models.py`: `ProviderDefinition`, `DeploymentDefinition` (kind,
reasoning, limits, capabilities, cost-per-mtok, capacity, decoding, summary, status), `RoleBinding`;
`ModelConfig` gains `providers` + `roles`. Validators: every deployment's `provider` exists; every
binding's `deployment` exists and is kind-compatible with the role.
*Verify:* new unit tests; AC-2 both directions.

**S3 — `${VAR}` resolver.** Provider `base_url` interpolation at load, resolved from the `AGENT_`
settings surface, raising `ModelConfigError` naming provider + variable when unresolved.
*Verify:* AC-10 (a) host value, (b) container value, (c) unset raises.

**S4 — Catalog rewrite.** `config/models.yaml` to the shape above; `config/models.cloud.yaml` deleted;
`config/model_roles.yaml` to Layer-3 bindings; `config/profiles/*.yaml` re-pointed to deployment keys.

**S5 — Loader.** `config/model_loader.py`: single catalog, no `model_config_path`;
`resolve_role_model_key` covers all roles; `config/profile.py`'s `resolve_model_key` falls back to it.
*Verify:* S1 snapshot re-run — byte-identical except the two declared deltas (§6).

**S6 — Concurrency + the `ModelRole` seam (highest-risk step).**
`concurrency.py`: semaphores keyed on provider name with per-deployment sub-limits;
`infer_provider_type` and `_normalize_endpoint` deleted; `client.py` registers deployments.

**The trap (codex finding, verified):** `ModelRole`'s values ARE the slot-aliases being deleted —
`PRIMARY = "primary"`, `SUB_AGENT = "sub_agent"`, `COMPRESSOR = "compressor"`
(`llm_client/types.py:26-28`). Two sites look those strings up in the catalog:
- `client.py:116-127` builds `_role_timeouts` from `cfg.models.get(role.value)`. Post-re-key that
  returns `None` and **primary's timeout silently drops 600s → the hardcoded 60s fallback**, breaking
  long thinking turns.
- `client.py:209` calls `request_slot(role=role.value)`. Post-re-key that key is unregistered and
  `concurrency.py:295-297` **yields with no concurrency control at all**.

Both must resolve through the binding first. Both get explicit regression tests asserting the
*values* (primary timeout == 600; a slot is actually acquired), not merely that the code runs.
*Verify:* AC-3 at a **non-default** provider ceiling (see §7.1), plus the two above.

**S7 — Guards + settings.** `config_guard.py`: retire `check_forbidden_role_divergence_*`'s divergence
half and both deployment-manifest checks; add catalog-reference + kind-compatibility checks.
`settings.py`: delete `model_config_path`; replace `enforce_required_secrets`'s active-profile lookup
with a new explicit `AGENT_DEPLOYMENT_TARGET` setting (`local`/`cloud`/`eval`), set per compose file,
and keep `required_secrets` keyed on it. **NOT `settings.environment`** — see §7.2.

**S7b — `model_config_path` attribution.** Six stamping sites (`app.py:135,297,443,1456,1467,1813`)
plus the `messages.model_config_path` column carry deployment provenance. The setting is deleted, so
these stamp the catalog path constant (`config/models.yaml`). The column stays; renaming it is
FRE-917+ territory.

**S8 — Deployment surface (same PR, or the deploy is broken).**
`docker-compose.cloud.yml:360`, `docker-compose.eval.yml:165,200`, `config/deployment.yaml`,
`.env.example:275,731`, `config/resolve.py` (the config-resolve CLI), and the **9 eval/study scripts**
that pin `AGENT_MODEL_CONFIG_PATH`. Add `AGENT_EMBEDDINGS_BASE_URL` to cloud/eval compose.

**S9 — Cost fields.** `input_cost_per_token` → `input_per_mtok` per ADR §2; update `pricing.py` and
`test_pricing.py`.

**S10 — Quality gates.** `make test` · `make mypy` · `make ruff-check` · `make ruff-format` ·
`pre-commit run --all-files`; code-review at `high` (src + config + cost + concurrency);
security-review (`${VAR}` resolution reads the environment).

---

## 5. AC proof map

| AC | Proven by |
|---|---|
| AC-1(a) | S1 snapshot, re-run at S5 — identical modulo §6 |
| AC-1(b) — catalog half | grep: `models.cloud.yaml`, `AGENT_MODEL_CONFIG_PATH` absent from `src/`, `config/`, compose |
| AC-1(b) — profile half | **Deferred to FRE-917** (owner-approved split) |
| AC-2 | kind-mismatch tests, both directions |
| AC-3 | provider ceiling honoured across two deployments, at a non-default value |
| AC-10 | host / container / unset |

---

## 6. Declared deltas (not silent)

1. `sub_agent.context_length` → **65536**. Served truth from the SLM server; 32768 (local) and 16384
   (cloud) were both under-declarations. Raises the ceiling for sub-agent context assembly and local
   artifact HTML builds — a real prod behaviour change, wants a live sanity check post-deploy.
2. `compressor` → **gpt-5.4-mini**, via the deletion of `gpt-5.4-nano`. Not a drift correction —
   completing a retirement already decided; `compressor` was the missed binding.

---

3. **`artifact_builder`'s Layer-3 default must be `qwen3.6-35b-instruct`, not `claude_haiku`.**
   ADR-0121:167-170's illustrative binding says `claude_haiku`. Taking that literally reproduces the
   FRE-879 regression exactly — a local artifact build with no active profile crossing to cloud Haiku.
   The local profile's real binding is `sub_agent`. Pinned + regression-tested.
   *Declared behaviour change:* today a no-profile `artifact_builder` call raises `ModelConfigError`
   (`client.py:204`) because no such catalog key exists. After, it resolves to the local instruct
   deployment. That is a fix, but it is a change, so it is declared rather than absorbed.

---

## 7. Open items for the owner

1. **AC-3 is weaker than the ADR claims.** The ADR says 4 concurrent calls across two `slm_local`
   deployments run unbounded today. They do not — `_normalize_endpoint` already groups both Qwen
   deployments onto one endpoint semaphore at `default_endpoint_limit=2`. The real gain is that the
   ceiling becomes *configurable per provider* instead of a hardcoded 2. I will test at a non-default
   value so the test cannot pass on the old hardcoded behaviour.

2. **`required_secrets` cannot key on `settings.environment`** (codex finding, verified).
   `env_loader.py:43-52` recognises only `production`/`prod`, `staging`/`stage`, `test` — **everything
   else, including `APP_ENV=eval`, returns `DEVELOPMENT`**. Keying on it would make the eval stack stop
   requiring the cloud API keys it actually spends money through. Three ways out:
   - **(a) New `AGENT_DEPLOYMENT_TARGET` setting** (`local`/`cloud`/`eval`), set per compose file.
     Truly 1:1 with today's buckets, one new field, makes explicit what `model_config_path` was
     accidentally encoding. **← my recommendation, and what the plan assumes.**
   - (b) Teach `env_loader` an `EVAL` environment — touches a pre-settings primitive with wider blast
     radius than this ticket wants.
   - (c) Derive required secrets from each provider's `auth_env` where a bound role uses it — elegant,
     but newly hard-fails local boot without an OpenAI key (`entity_extraction` binds `gpt-5.4-mini`),
     which today's `local: []` permits.

3. **`config/models.benchmark-{4b,4b-f16,8b}.yaml`** are selected *only* via
   `AGENT_MODEL_CONFIG_PATH`, which this PR deletes — so they become unreachable. The ADR never
   mentions them. Convert all three to the new shape, or delete them with
   `scripts/eval/.../run_embedder_benchmark.sh`? (FRE-656 embedder benchmark is held.) **Default if
   you don't say: delete, and note it in the handoff.**

4. **`coding_large_context`** is dead — no role binds it, no provider, no endpoint. **Default:
   drop it** rather than carry it into a curated catalog. Say if you want it kept.
