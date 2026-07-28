# FRE-1037 — Widen the LLM call-role enum, thread the real role, fail closed

**Ticket:** https://linear.app/frenchforest/issue/FRE-1037
**Branch:** `fre-1037-widen-role-enum-fail-closed`
**Related:** FRE-989 (cost attribution audit, this underpins it), ADR-0082 (tier-aware routing gap — names the same `budget_role_for()` coarsening for primary/sub_agent), ADR-0099/ADR-0121 (established `config/model_roles.yaml` as the single generative source of truth for role→model assignment)

## Scope recap

`ModelRole` (`src/personal_agent/llm_client/types.py`) has 4 members (`primary`, `sub_agent`,
`compressor`, `artifact_builder`). `config/model_roles.yaml`'s `bindings:` block — the ADR-0099/0121
established single source of truth for role assignment — declares 12: those 4 plus
`entity_extraction`, `captains_log`, `session_summary`, `insights`, `embedding`, `reranker`,
`reranker_fallback`, `vision`. Because the enum can't express the other 8, every background producer
using them passes `role=ModelRole.PRIMARY` (or an `from_str(...) or PRIMARY/SUB_AGENT` fallback) at the
`.respond()` telemetry/routing boundary, corrupting both cost attribution and model-routing signal.

**Design decision — what "the configuration role matrix" means (flagging for codex + owner):**
There are two independently-evolving role vocabularies in this repo: `config/model_roles.yaml`
(model-routing assignment, ADR-0099/0121 SSOT) and `config/governance/budget.yaml` (cost-bucket
classes, consumed only via `cost_gate.budget_role_for()`). The ticket's "fifteen roles" doesn't match
either file's count exactly (12 and 9, union 17 minus overlaps). **Recommendation: derive `ModelRole`
strictly from `model_roles.yaml`'s `bindings:` block (12 members)** — that file is the one ADR-anchored
generative SSOT for a call's *routing identity*; `budget.yaml`'s classes are an intentionally coarser,
separate mapping (e.g. `primary` + `sub_agent` → `main_inference` by design, per ADR-0082) and conflating
the two reintroduces exactly the kind of second-drift-surface ADR-0099 fought. `skill_routing` (routed via a dedicated `AppConfig.skill_routing_model_key` field) and `study` (routed
via `scripts/study/categorizer.py`'s own convention, with a real `budget_role="study"` already in use)
are both real, separate telemetry gaps this ticket's investigation surfaced — same "reports primary/
sub_agent" symptom — but neither has an ADR-anchored matrix home to derive from. Proposed: fix both
(14 members total, tested by name, not matrix-derived, for these two) since each is a one-line, low-risk,
complete fix discovered in-scope; flag explicitly for owner sign-off rather than silently expanding the
"derive from config" claim to cover them.

**A subtler finding that changes the shape of one fix:** `captains_log/reflection.py`'s manual-JSON
fallback path constructs a bare `LocalLLMClient()` (no auth headers — confirmed by reading
`_do_request`, it never sets `Authorization`) and calls it with `role=ModelRole.PRIMARY`. `captains_log`
resolves to `claude_sonnet` (a cloud model) in config. Relabeling this call site's `role=` to
`CAPTAINS_LOG` **without** also switching to a factory-constructed client would not fix the
mislabeling — it would send an unauthenticated request to Anthropic and turn a silently-wrong-but-working
fallback into an outright broken one. The correct fix reuses `_captains_log_role` (already resolved at
`reflection.py:361`) to construct via `get_llm_client_for_key`, matching the pattern every other
correctly-routed call site in this codebase already uses. This is not "changing which model the role
maps to" (captains_log has always mapped to claude_sonnet) — it's making the fallback path actually
honor that existing mapping instead of silently defaulting to local. `second_brain/entity_extraction.py`
and `second_brain/session_summary.py`'s "local path" branches do **not** have this problem — they're
already gated on `provider is None` (i.e. only reached when the resolved model genuinely has no cloud
provider), so those are safe one-line role-label fixes.

## Step 1 — Widen `ModelRole`, guard against drift (no behavior change)

**File:** `src/personal_agent/llm_client/types.py`

Add 10 members to `ModelRole`: `ENTITY_EXTRACTION`, `CAPTAINS_LOG`, `SESSION_SUMMARY`, `INSIGHTS`,
`EMBEDDING`, `RERANKER`, `RERANKER_FALLBACK`, `VISION`, `SKILL_ROUTING`, `STUDY` (14 total). Values are
the lowercase snake_case strings matching the matrix keys exactly (`entity_extraction`, `captains_log`,
…); `skill_routing` and `study` are matrix-independent additions (see codex-review revision below) —
real, configured, recurring background roles that route via a dedicated setting/script convention
rather than `model_roles.yaml`.

Also regenerate `tests/personal_agent/config/catalog_snapshot_golden.json` (currently 4 role entries,
built by iterating every `ModelRole` in `LocalLLMClient.__init__` — `client.py:124` — via
`tests/personal_agent/config/test_catalog_snapshot.py:248`) — it will fail as soon as the enum widens
until regenerated with the 10 new roles' resolved data.

**File:** `tests/test_llm_client/test_types.py`

- Replace `test_model_role_exactly_four_members` (line 23-27) — hard `== 4` assertion — with
  `test_model_role_matches_bindings_matrix`: load `config/model_roles.yaml`'s `bindings:` keys via
  `personal_agent.config.model_loader` (reuse `_load_role_matrix`/`load_matrix`, whichever is public/
  test-accessible), and assert `{m.value for m in ModelRole} >= set(bindings.keys())` — every matrix
  role must be representable. Keep `skill_routing` as a documented, matrix-independent addition (assert
  it's present separately, with a comment explaining why it's not matrix-sourced).
- This test is the "cannot drift apart again" proof required by the ticket: a future matrix role added
  without a corresponding `ModelRole` member fails CI loudly.

**Verify:** `uv run pytest tests/test_llm_client/test_types.py -v` — new test passes; run full
`test_types.py` to confirm nothing else assumed 4 members.

## Step 2 — Thread the real role at each background call site

For every site below, `.respond(role=...)` currently passes `ModelRole.PRIMARY` (or a fragile
`from_str(...) or default`) where the call is not actually a primary orchestrator turn.

| File:line | Change | Risk |
|---|---|---|
| `captains_log/feedback.py:123-148` (`_feedback_llm_complete`) | Add a `role: ModelRole` parameter; pass it through to `client.respond(role, ...)` instead of hardcoded `ModelRole.PRIMARY` | none — client already correctly constructed via `get_llm_client_for_key` |
| `captains_log/feedback.py:274` (`handle_deepen`) | Call `_feedback_llm_complete(role_key, system, user, budget_role="insights", role=ModelRole.INSIGHTS)` | none |
| `captains_log/feedback.py:316` (`handle_too_vague`) | Call with `role=ModelRole.CAPTAINS_LOG` | none |
| `captains_log/reflection.py:364-368,480-489` | Reuse `_captains_log_role` (line 361) to construct via `get_llm_client_for_key(_captains_log_role, budget_role="captains_log")` instead of bare `LocalLLMClient(...)`; pass `role=ModelRole.CAPTAINS_LOG` | **routing fix, not just label** — see design note above; must confirm the DSPy-failure fallback path still round-trips correctly for a genuinely local `captains_log` config (test both) |
| `captains_log/reflection_dspy.py:417` (`captains_log_role is None` defensive branch) | `role=ModelRole.CAPTAINS_LOG` instead of `ModelRole.PRIMARY` (same `llm_client` object, same reasoning as reflection.py) | low — defensive/rarely-hit branch |
| `second_brain/entity_extraction.py:1006-1007` (cloud path) | `role=ModelRole.ENTITY_EXTRACTION` | none — telemetry only |
| `second_brain/entity_extraction.py:1032` (local path) | Replace `ModelRole.from_str(entity_extraction_role) or ModelRole.PRIMARY` with `ModelRole.ENTITY_EXTRACTION` directly | none — branch already gated on `provider is None` |
| `second_brain/session_summary.py:596-597` (cloud path) | `role=ModelRole.SESSION_SUMMARY` | none — telemetry only |
| `second_brain/session_summary.py:619-620` (local path) | Replace `ModelRole.from_str(role_name) or ModelRole.SUB_AGENT` with `ModelRole.SESSION_SUMMARY` directly | none — branch already gated on `provider is not None` check above it |
| `orchestrator/skills.py:475` (`route_skills`) | `role=ModelRole.SKILL_ROUTING` | none — telemetry only (cost already correctly attributed via explicit `budget_role` default) |
| `scripts/study/categorizer.py:145,151` | `role=ModelRole.STUDY` instead of `ModelRole.SUB_AGENT` | none — telemetry only; `budget_role="study"` already correctly set at line 124 |
| `orchestrator/executor.py:4174-4182,4554-4555` (vision escalation) | Introduce `respond_role = model_role`; when `effective_model_key != role_key` (an escalation occurred): (a) always use `budget_role_for(ModelRole.VISION.value)` for cost attribution (line 4181, safe — pure string computation); (b) only set `respond_role = ModelRole.VISION` for the `.respond()` telemetry label (line 4555) **when `isinstance(llm_client, LiteLLMClient)`** — `LocalLLMClient.respond()` re-resolves its deployment internally from `role.value` (`client.py:220`), so relabeling there risks a second, divergent resolution; `LiteLLMClient`'s model is fixed at construction, so its `role` is provably label-only | **routing/attribution fix** — today a vision-escalated turn bills and logs as whatever `model_role` (primary/sub_agent) was, not vision; must not change which model the turn actually runs on (`effective_model_key` is unchanged) |

**Do not touch:** the legitimate orchestrator call sites already correctly using
`PRIMARY`/`SUB_AGENT`/`COMPRESSOR`/`ARTIFACT_BUILDER` (executor.py's non-vision paths, sub_agent.py,
expansion.py, expansion_controller.py, context_compressor.py, artifact_tools.py, memory/service.py) —
these are genuinely primary/sub-agent/compressor/artifact-builder calls today.

**Deferred, flagged in PR + Linear comment (not fixed here):** `memory/embeddings.py` and
`memory/reranker.py` call their target APIs directly over HTTP, outside `ModelRole`/`.respond()`
entirely, and already write correct `purpose="embedding"`/`purpose="reranker"` to the cost ledger. They
have no current mislabeling to fix — `ModelRole.EMBEDDING`/`RERANKER`/`RERANKER_FALLBACK` exist in the
enum for completeness (matrix membership) but no call site needs updating.

## Step 3 — Fail closed

**File:** `src/personal_agent/llm_client/types.py`

Add `ModelRole.required(value: str) -> "ModelRole"` — same lookup as `from_str`, but raises
`ValueError(f"{value!r} is not a valid ModelRole")` instead of returning `None`. Keep `from_str`
returning `None` (still used defensibly by `executor.py:5378-5385` reconstructing `last_llm_role` from
persisted step metadata — that's data-resilience against stale/corrupt history, a different risk
profile than a live call-time assignment, and out of scope to change).

Since Step 2 removes every `from_str(...) or <default>` call-time-assignment pattern (the two
`second_brain` sites), there should be **zero** remaining production call sites relying on the silent
fallback by the time this step lands — confirm via `ast-grep run -p 'ModelRole.from_str($$$) or $$$' -l py src/`
returning empty.

**Enforce at the actual call boundary, not just as an unused helper.** Add an explicit
`isinstance(role, ModelRole)` guard at the top of both `LocalLLMClient.respond()` (`client.py:148`) and
`LiteLLMClient.respond()` raising a clear `TypeError` — without this, a non-`ModelRole` value reaching
either client today raises only an incidental `AttributeError` on `role.value`, which is exactly the
"warning a background task swallows" failure mode the ticket wants closed.

**Discovered, out of scope, flagged for a follow-up ticket (not fixed here):**
`second_brain/entity_extraction.py`'s and `session_summary.py`'s "local path" branches (`provider is
None`) are very likely unreachable dead code — `ModelConfig._deployments_reference_known_providers`
(`models.py:498`) requires every catalog entry, including local ones (`slm_local`), to declare a
provider, so `provider is None` can only occur when the resolved model key is missing from the catalog
entirely (a config error), not as a genuine "this model is local" signal. Step 2's one-line role-label
fix there is kept (harmless, and correct if the branch is ever reached), but the branch's dead-code
status and the `provider is None`-as-placement-detection bug are a separate pre-existing issue — do not
fabricate a "local role" test to cover it; note it in the PR and file a fast follow-up ticket for the
placement-detection fix itself.

**File:** `src/personal_agent/cost_gate/__init__.py`

Add explicit entries to `_BUDGET_ROLE_BY_FACTORY_NAME` for every newly-threaded `ModelRole` so none of
them silently fall through `budget_role_for()`'s `"main_inference"` default:
- `"session_summary": "captains_log"` (ADR-0124 D2's existing, explicit deferral — same lane, now
  declared instead of coincidental)
- `"vision": "main_inference"` (no dedicated budget lane exists in `budget.yaml`; vision escalations are
  part of a user-facing turn, so `main_inference` is the correct bucket, now explicit not coincidental)
- `"skill_routing": "skill_routing"` (already correct via the factory's own default param; adding it
  here makes it explicit and independent of that default)
- `entity_extraction`/`captains_log`/`insights`/`artifact_builder` already present — no change

**Test:** `tests/personal_agent/cost_gate/test_budget_role_for.py` — add cases for the three new entries.

**Test:** a new test asserting `.respond()` (both `LocalLLMClient` and `LiteLLMClient`) raises when
handed something that isn't a `ModelRole` member — since `role` is a required, statically-typed
parameter, the realistic failure mode to test is `ModelRole.required()` raising on an unrecognized
string at the boundary where a role is derived from a resolved string rather than passed as a literal
(document why a "missing role" case can't occur given the type signature — don't fabricate a test for
an impossible state).

## Step 4 — Proof

1. **Before/after role distribution.** Re-run the same 7-day-window query methodology the ticket's
   measurement used (Elasticsearch `model_call_completed`/`api_costs`, per FRE-433/434 measure-don't-assert
   convention). State the primary-share before (93%, from the ticket) and after this change ships and a
   comparable window has elapsed — this is a **post-deploy** proof, note it explicitly as deferred to the
   Linear close-out comment, not the PR checklist (PR hygiene: no post-deploy verification in the PR).
2. **Fail-closed test** — `ModelRole.required()` raises on a non-matrix string (Step 3).
3. **No-drift test** — Step 1's golden test passes and demonstrably fails against a fixture matrix with
   an added role absent from `ModelRole` (mirror the existing `test_role_resolution_golden.py` fixture
   pattern).

## Test plan (TDD order)

1. `tests/test_llm_client/test_types.py` — golden matrix test (Step 1), `ModelRole.required()` raise test (Step 3). Write failing, then implement.
2. Per-call-site regression tests: mock `.respond()` (or the constructed client) and assert the `role=` kwarg for each call site in Step 2's table (12 sites) — extend existing test files for `captains_log/feedback.py`, `captains_log/reflection.py`, `second_brain/entity_extraction.py`, `second_brain/session_summary.py`, `orchestrator/skills.py`, `orchestrator/executor.py` (vision escalation path — likely `test_orchestrator/test_executor_vision*.py` or similar, confirm exact file during implementation).
3. `tests/personal_agent/cost_gate/test_budget_role_for.py` — three new entries.
4. `tests/personal_agent/test_process_role_indirection.py` — this test currently **asserts the workaround as the contract** (`assert "entity_extraction_role" in ee_source`); once entity_extraction.py's local path passes `ModelRole.ENTITY_EXTRACTION` directly instead of round-tripping through `entity_extraction_role`, re-read this test and update its assertions to match the corrected pattern rather than leaving it asserting the old bug.
5. Full `make test`, `make mypy`, `make ruff-check`/`make ruff-format`.

## Explicitly out of scope (per ticket)

- Changing which model any role maps to (config/model_roles.yaml values are untouched).
- `promotion`/`freshness` budget-yaml roles — no current LLM call site exists for either (confirmed
  during scoping); not added to `ModelRole` since there's nothing to thread. (`study` **is** added —
  see codex-review revision below; it does have a live call site.)
- Unifying `model_roles.yaml` and `budget.yaml` into one file — a larger, separate architectural
  question, not this ticket's.
- The `entity_extraction.py`/`session_summary.py` `provider is None` placement-detection bug (dead-code
  branch, discovered during codex review below) — fixing *how* cloud-vs-local is detected is a separate,
  pre-existing issue; only the role label inside that branch is corrected here.

## Open questions for owner sign-off before coding

Codex's review (below) resolved most open design questions with evidence; these two remain genuine
calls only the owner can make:

1. **Confirm `SKILL_ROUTING` and `STUDY` as matrix-independent 13th/14th `ModelRole` members** (recommended)
   rather than deferring either to a follow-up ticket — both are real, currently-live background roles
   the investigation surfaced, not merely hypothetical.
2. **Confirm the `reflection.py` fix is an in-scope fold-in**, not a separate ticket: switching its
   manual-fallback path from a bare `LocalLLMClient` to a factory-constructed client changes fallback
   *dispatch mechanics* (which client class executes the call) even though it changes no *model mapping*
   — the ticket's "explicitly not in scope: changing which model any role maps to" clause is about
   mapping, not dispatch, but this is close enough to the line to want an explicit yes.

## Revision after codex plan-review (adversarial pass) — corrections applied

Codex (via `codex:rescue`) reviewed this plan before implementation and found four issues, verified
independently (grepped `config/models.yaml` — every entry declares `provider:`, confirming finding #4):

1. **`study` is a fifth mislabeled role I missed.** `scripts/study/categorizer.py:124,145,151` already
   resolves the entity-extraction deployment with `budget_role="study"` (a real, distinct
   `budget.yaml` class) but reports `role=ModelRole.SUB_AGENT` at the `.respond()` telemetry boundary.
   **Added:** `ModelRole.STUDY` as a 14th member (matrix-independent, same class of addition as
   `SKILL_ROUTING` — both are real, configured, recurring background roles the matrix doesn't carry
   because they route via a dedicated setting/script convention, not `model_roles.yaml`). Two call
   sites in `categorizer.py` get their `role=` corrected the same way as the other background sites.
   Two one-time completed migration scripts (`scripts/migrate_fre772_entity_type_v2.py:477`,
   `scripts/migrate_fre865_entity_class_backfill.py:486`) are **explicitly left unfixed** — historical,
   already-run, no ongoing telemetry impact — noted in the PR rather than silently dropped.

2. **The `reflection.py` failure mechanism was mischaracterized (conclusion unchanged).** `claude_sonnet`
   has no `endpoint:` in `config/models.yaml`, so a relabel-only fix would make bare `LocalLLMClient`
   fall back to *its own* `base_url` (the local LM Studio URL) and send `model_id: claude-sonnet-5` to
   the **local** server — a wrong-model-id error from the local backend, not an unauthenticated cloud
   call as originally written. Either way the one-line relabel is unsafe; the plan's fix (construct via
   `get_llm_client_for_key(_captains_log_role, budget_role="captains_log")` instead of bare
   `LocalLLMClient(...)`) still stands and is confirmed necessary.

3. **Vision-escalation fix narrowed to avoid a double-resolution hazard.** `LocalLLMClient.respond()`
   re-resolves its own deployment internally from `role.value` (`client.py:220`,
   `get_current_selection(role.value)` → `resolve_role_target`). If the escalated client happens to be a
   `LocalLLMClient` (not true for the current cloud-pinned `vision` binding, but a real hazard if `vision`
   is ever rebound locally), passing `role=ModelRole.VISION` to `.respond()` could trigger a **second,
   independent** resolution that diverges from the `effective_model_key` the executor already computed
   and constructed the client against. `LiteLLMClient` has no such hazard — its model is fixed at
   construction (`litellm_client.py:299`-`304`) and `role` is telemetry-only there.
   **Revised fix:** gate the `.respond()` role-label override on client type —
   `respond_role = ModelRole.VISION if isinstance(llm_client, LiteLLMClient) else model_role` — so the
   telemetry relabel only applies where it's provably safe. The `budget_role_for(...)` cost-attribution
   fix (line 4181) is unaffected by this gate — it's a pure string computation, safe regardless of client
   type, and always uses the vision-correct budget role when escalated.

4. **Fail-closed needs to live at the actual call boundary, not just as an unused helper.** Add an
   explicit `isinstance(role, ModelRole)` guard raising a clear error at the top of both
   `LocalLLMClient.respond()` and `LiteLLMClient.respond()` (not only `ModelRole.required()` sitting
   unused) — otherwise a non-`ModelRole` value reaching either client raises an incidental
   `AttributeError` on `role.value`, which is exactly the "warning a background task swallows" failure
   mode the ticket wants closed.
   **Also discovered, out of scope, flagged for a follow-up ticket:** `entity_extraction.py`'s and
   `session_summary.py`'s "local path" branches (`provider is None`) are very likely **unreachable dead
   code** — `ModelConfig._deployments_reference_known_providers` (`models.py:498`) requires every
   catalog entry, including local ones (`slm_local`), to declare a provider, so `provider is None` can
   only occur when the resolved model key is entirely missing from the catalog (a config error), not as
   a genuine "this model is local" signal. The one-line role-label fix in the plan's Step 2 table is kept
   (harmless, correct if the branch is ever reached), but the branch's *dead-code status* and the
   `provider is None`-as-placement-detection bug are a separate, pre-existing issue — not fixed here, and
   not fabricated a "local role" test for, per codex's caveat. Noted in the PR description; a fast
   follow-up ticket will be filed for the placement-detection fix itself.
   **Also added to the test plan:** `tests/personal_agent/config/catalog_snapshot_golden.json` is
   generated by iterating every `ModelRole` (`client.py:124`, `test_catalog_snapshot.py:248`) and
   currently has only 4 entries — widening the enum will fail this golden test until it's regenerated
   with the new roles' resolved timeout/definition data (Step 1 test list).
