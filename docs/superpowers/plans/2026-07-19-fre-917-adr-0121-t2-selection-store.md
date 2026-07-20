# FRE-917 — ADR-0121 T2: selection store, server-authoritative resolution, resolver unification

**Ticket:** FRE-917 (Approved, High, Tier-1:Opus, stream:build1) · parent FRE-887
**Backing ADR:** [ADR-0121](../../architecture_decisions/ADR-0121-model-catalog-and-selection-layer.md) — Decision **sections 4 + 6**, Sequencing **step 2**
**Inherits:** [ADR-0079](../../architecture_decisions/ADR-0079-session-execution-profile-ownership.md) (eleven invariants), [ADR-0076](../../architecture_decisions/ADR-0076-*.md) (selection-state precedent)
**Acceptance criteria this ticket must satisfy:** **AC-4, AC-6, AC-7**

---

## Scope boundary (read first — this is the load-bearing decision)

The ADR-0121 chain partitions the work so this ticket does **sections 4 + 6 only**. The
sibling tickets fix the boundary:

| Ticket | ADR sections | Owns |
|---|---|---|
| FRE-916 (T1, merged) | 1, 2 | catalog/providers/bindings; `RoleBinding.open` **declared but unread** |
| **FRE-917 (this)** | **4, 6** | **selection store + server-authoritative `primary` resolution + factory unification + guardrail** |
| FRE-918 (T3) | 3, 7 | config read API + provider-keyed **availability** filtering (AC-5) |
| FRE-919 (T4) | 8 | telemetry `profile` → provider+model (AC-8) |
| FRE-920 (T5, seam) | **3, 5** | Path removed end-to-end: PWA picker, **vision pinning + attachment-escalation dismantling**, CLI `--profile`, error card, **`execution_profile` column/read-path removal** (AC-9) |

**Therefore FRE-917 explicitly does NOT:**
- delete the `ExecutionProfile` class / `resolve_model_key` (section 5 territory; the class still
  governs `sub_agent`, `artifact_builder`, and non-model concerns until T5) — **the full grep-clean
  half of AC-1(b) completes at T5 when Path goes**, not here;
- touch vision / the attachment escalation path (`executor.py:1649-…`) — that is ADR §5 = FRE-920;
- remove the `sessions.execution_profile` column or the `profile` pill — the pill stays live through
  T2–T4, so **the selection store is seeded from the profile** and new-session resolution bridges the
  profile, keeping the deploy behaviour-preserving.

FRE-917 makes the **selection store the authority for the `primary` model only**, seeded so nothing
moves (AC-7), and lands the guardrail (AC-4) + server-authoritative asymmetry (AC-6).

*If the owner wants ExecutionProfile deletion pulled forward into this PR, that is a scope change to
confirm before coding — the plan below deliberately leaves it to T5 per the sequencing.*

---

## What lands (component map)

### 1. Selection store (mirror `constraint_preferences`)
- **Table** `session_model_selections` — composite PK `(session_id, role)` → `deployment_key`:
  ```sql
  CREATE TABLE IF NOT EXISTS session_model_selections (
      session_id     UUID NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
      role           TEXT NOT NULL,
      deployment_key TEXT NOT NULL,
      created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      PRIMARY KEY (session_id, role)
  );
  ```
  Scoped by session (ownership flows through `sessions.user_id`); no `user_id` column — the write
  API enforces ownership by first resolving the session for the CF-Access user (404 on mismatch),
  exactly like the existing `SessionRepository.update` path.
- **SQLAlchemy model** `SessionModelSelectionModel` on the same `Base` (`service/models.py`).
- **Repository** `SessionModelSelectionRepository(db)` — `service/repositories/session_model_selection_repository.py`:
  - `get(session_id, role) -> str | None`
  - `get_all(session_id) -> dict[str, str]`
  - `upsert(*, session_id, role, deployment_key)` — `pg_insert(...).on_conflict_do_update(...)` + commit.
  Pattern copied verbatim from `ConstraintPreferencesRepository`.
- **Migration** `docker/postgres/migrations/0020_session_model_selections.sql` (idempotent, `BEGIN;…COMMIT;`,
  `$AGENT_DATABASE_ADMIN_URL` header per FRE-808) **+ mirror the `CREATE TABLE` into `docker/postgres/init.sql`.**
- **Backfill (in the same migration txn):** one `primary` row per existing session mapping the stored
  `execution_profile` to the primary deployment key that profile resolved to — **explicit rows, not
  implicit** (AC-7), so a later default change never moves them:
  | `sessions.execution_profile` | inserted `(role='primary', deployment_key=…)` |
  |---|---|
  | `'local'`  | `qwen3.6-35b-thinking` |
  | `'cloud'`  | `claude_sonnet` |
  ```sql
  INSERT INTO session_model_selections (session_id, role, deployment_key)
  SELECT session_id, 'primary',
         CASE execution_profile WHEN 'cloud' THEN 'claude_sonnet'
                                ELSE 'qwen3.6-35b-thinking' END
  FROM sessions
  ON CONFLICT (session_id, role) DO NOTHING;
  ```
  (Values are the T1 catalog keys; `local.yaml`/`cloud.yaml` `primary_model` confirm the mapping.)

### 2. Guardrail resolver (section 6 — the pure, unit-testable core; AC-4)
New function in `config/model_loader.py` (next to `resolve_role_target`):
```python
def resolve_selected_deployment(
    role: str, selection: str | None, config: ModelConfig
) -> str:
    """Resolve the effective deployment key for a role given an advisory selection.

    Fail-closed (ADR-0121 §6): the selection is honoured ONLY when the role is
    `open` AND the key is a valid, kind-compatible catalog entry. Otherwise the
    role's configured default binding wins. Pinned roles are never consulted.
    """
```
Rules, in order:
1. `binding = config.roles.get(role)`; `default_key = binding.deployment if binding else role`.
2. **Structural** — if `selection is None` or `binding is None` or **not `binding.open`** → return `default_key`
   (pinned roles never honour a selection, even if a store row exists — the AC-4 discriminator).
3. **Fail-closed validity** — honour `selection` only if `config.models.get(selection)` exists AND
   its `kind` equals `ROLE_KIND_REQUIREMENTS.get(role, LLM)`; else return `default_key`.
   *(Availability = provider-health is FRE-918/AC-5; T2 checks existence + kind. Note the seam.)*
4. Return the honoured `selection`.

A companion **validation predicate** the write API uses (reject vs fall-back are different policies):
```python
def is_writable_selection(role: str, key: str, config: ModelConfig) -> bool:
    # role is open AND key is a valid, kind-compatible catalog entry
```

### 3. Factory unification (section 6 — one door; AC-4c)
`llm_client/factory.py`:
- Extract the shared tail both paths already duplicate:
  ```python
  def _build_client(model_key, model_def, budget_role, config) -> Any:
      if config.placement_of(model_key) is not Placement.LOCAL:
          return LiteLLMClient(model_id=model_def.id, provider=model_def.provider or "anthropic",
                               max_tokens=model_def.max_tokens or 8192, budget_role=budget_role)
      return LocalLLMClient()
  ```
- `get_llm_client(role_name="primary", *, selection_key=None)`:
  - resolve-to-key: `chosen = resolve_selected_deployment(role_name, selection_key or get_current_selection(role_name), config)`
    — the selection comes from the explicit arg OR the per-turn contextvar (§4);
  - if `selection_key`/contextvar is `None`, fall through to the existing profile-redirect key
    (`resolve_profile_redirect`) so `sub_agent`/`artifact_builder` behaviour is unchanged;
  - `resolve_role_target(role_name, model_key=chosen, config)` → `(key, model_def)` (keeps per-use binding overrides);
  - `budget_role = budget_role_for(role_name)`; `return _build_client(...)`.
- `get_llm_client_for_key(model_key, budget_role="skill_routing")` — **kept** (7 trusted-config
  callers + tests assert its raise-on-unknown contract); validate-or-raise, then route through
  `_build_client`. It is the trusted-config door, not a user-selection door; the user-selection
  guardrail lives on `get_llm_client`. *(Documented boundary — the future sub-agent ADR routes
  model-proposed keys through the guarded path.)*

**AC-4c** is proven against `resolve_selected_deployment` (open role + non-catalog key → default) and
end-to-end via `get_llm_client("primary", selection_key="<bad>")` → default client.

### 4. Server-authoritative resolution (section 4; AC-6, AC-7 in-flight)
Mirror the existing profile machinery exactly, one layer up.

- **Per-turn contextvar** `current_selection` in a small new module `config/selection.py`
  (`set_current_selection(map)/get_current_selection(role)/reset`), symmetric with
  `config/profile.py`'s `_current_profile`. Set once per background task → **in-flight isolation is
  free** (each `asyncio.Task` gets its own context copy). In T2 it carries only `{'primary': key}`.
- **`/chat/stream`** (`service/app.py`):
  - add optional advisory field `primary_selection: str | None = Form(default=None)` (the future
    picker / the AC-6 tests supply a model key here; the live PWA does not yet);
  - new `_resolve_session_selection(session_id, supplied_key, supplied_profile, user_id)` mirroring
    `_resolve_session_profile`: **existing session** → stored `primary` selection wins (supplied
    ignored); **new session** → adopt `supplied_key` if given, else the profile-derived primary key
    (bridge for the live pill), validated through `resolve_selected_deployment('primary', …)`; persist
    on row creation;
  - thread the resolved key into the background task; `set_current_selection({'primary': key})` right
    after `set_current_profile(...)` (`app.py:258-262`); echo resolved key + provenance in the response.
  - Persist the new-session `primary` row where the session row is created (`app.py:287-304`).
- **PATCH selection write** — extend the gateway session router (`gateway/session_api.py`) with a
  `primary_selection` field on `SessionProfileUpdate` (or a sibling `PATCH …/selection`), requiring
  `sessions:write`, resolving the CF-Access user, user-scoped repo read (404 on mismatch), then:
  - **reject** (422) if `not is_writable_selection('primary', key, config)` — pinned role or
    non-catalog/wrong-kind key (server-side validation, §6);
  - `upsert` the selection; `emit_session_profile`-style `STATE_DELTA` (`session_selection` key) to the
    single active socket (ADR-0075); return the confirmed value.
- **Hydration** — add resolved `primary` selection + provenance to the session GET payload
  (`gateway/session_api.py` `_session_to_dict` augmentation, mirroring the FRE-426 context/cost
  pattern). WS-reconnect STATE_DELTA hydration seam emitted on connect. *(Live reload/reconnect proof
  is AC-9 = FRE-920; T2 lands the server side + unit coverage.)*
- **Provenance** — emit `server-hydrated | localStorage | default` shape (invariant 10), same as the
  profile provenance the PWA derives today.

---

## AC → proof mapping (the master-gate input)

| AC | Proof (test/probe asserting the *outcome*) |
|---|---|
| **AC-4a** | Inject a store row for each `r ∈ {entity_extraction, captains_log, insights, embedding, reranker, reranker_fallback, sub_agent, compressor}` **and** `vision`; assert `resolve_selected_deployment(r, injected, cfg)` returns the role's **default** (pinned → never honoured). Discriminating because the row *exists*. |
| **AC-4b** | Write API `PATCH` naming a pinned role (or `vision`) → **422 before storage**; store unchanged. |
| **AC-4c** | `resolve_selected_deployment('primary', '<non-catalog>', cfg)` → primary **default**; and end-to-end `get_llm_client('primary', selection_key='<non-catalog>')` builds the default client (not empty/arbitrary). |
| **AC-6a** | Existing session storing **A** (non-default); `/chat/stream` supplying **B** → resolves/runs **A** (supplied ignored). |
| **AC-6b** | New session, no row, supplying **B** → runs **B** and **B persisted** (not **D**). |
| **AC-6c** | New session supplying nothing → **D** (default) adopted + persisted. |
| **AC-6d** | `PATCH` from a different user's bearer token → **404**, stored value unchanged. |
| | *Fixtures: A=`claude_sonnet`, B=`claude_haiku`, D=`qwen3.6-35b-thinking` — three distinct llm-kind keys, so no branch passes by coincidence.* |
| **AC-7a** | Seed sessions with `execution_profile` **both** `'local'` and `'cloud'`; run migration; each resolves to the **same** model as before (per the T1 snapshot) **and** has a persisted `primary` row. |
| **AC-7b** | A turn in flight across the migration boundary completes on the model it launched with (contextvar snapshot at launch). |

---

## Atomic steps (TDD; failing test first each)

1. **Store schema + repo.** Write `test_session_model_selection_repository.py` (mirror the retention
   repo test, `:5433`, autouse engine-dispose). Add model + repo + migration `0020` + init.sql mirror.
   → `make test-file FILE=tests/personal_agent/service/repositories/test_session_model_selection_repository.py`.
2. **Migration backfill.** `tests/migrations/test_0020_session_model_selections_migration.py` — seed
   local+cloud sessions, apply `0020` via `$AGENT_DATABASE_ADMIN_URL`, assert one correct `primary`
   row each (AC-7a). → the migration test.
3. **Guardrail resolver.** `test_selection_resolver.py` — AC-4a (all pinned roles + vision → default),
   AC-4c (open role + bad key → default), open role + valid key → honoured. Implement
   `resolve_selected_deployment` + `is_writable_selection`.
4. **Factory unification.** Extend `test_factory*.py` — `get_llm_client('primary', selection_key=A)`
   builds A's client; `selection_key='<bad>'` → default; `get_llm_client_for_key` unchanged
   (existing tests stay green). Extract `_build_client`.
5. **Contextvar + turn resolution.** Unit-test `_resolve_session_selection` asymmetry (AC-6a/b/c) with
   a mocked repo; contextvar in-flight isolation (AC-7b) via two tasks. Wire `/chat/stream`.
6. **PATCH write + validation + 404.** API test (AC-6d + AC-4b) against the gateway router.
7. **Hydration payload + STATE_DELTA + provenance.** Unit coverage of the GET augmentation and emit.
8. **Docs** — update the ADR Status Updates (step-2 delivery note), this plan's outcomes, docstrings.

---

## Standards / gotchas
- Google docstrings, modern type hints, `structlog` + `trace_id`, no `print/os.getenv/bare except`,
  frozen models where they carry config. ADR-0074 identity on every new `log.*` / `MERGE` / emit.
- Migration runs as the `agent` superuser via `AGENT_DATABASE_ADMIN_URL` (FRE-808); DDL also mirrored
  into `init.sql`. No Alembic.
- One-phase-one-PR: no PWA, no vision/attachment, no ExecutionProfile deletion (all FRE-918/919/920).
- Deploy class for master: **Postgres migration** (ask-first) + gateway rebuild (ask-first).

## Open questions for owner (surfaced at approval)
1. **Scope confirm:** leave ExecutionProfile deletion + vision/attachment to T5 (FRE-920), per the
   sequencing? — **Owner decided 2026-07-20: narrow (A), recorded in ADR-0121 AC-1(b).**
2. **Contextvar vs explicit threading** for the per-turn primary selection — plan uses a contextvar
   symmetric with `set_current_profile` (lowest-touch, in-flight isolation free). OK? — kept.
3. **Availability in the guardrail:** T2 checks existence + kind-compat; provider-health availability
   is FRE-918/AC-5. Confirm that seam. — kept, seam noted.

## Delivery & review outcomes (2026-07-20)

Built per the narrow scope. **Codex plan-review** (pre-code) + **self code-review (high)** + **security
-review** run. Security review: clean (no new vulnerabilities; guardrail double-guarded, AuthZ/IDOR
closed, SQL parameterized). High code-review surfaced 10 findings; disposition:

- **Fixed:** #1 the live Path pill must write the selection store or flipping it no longer moves
  `primary` → PATCH `/profile` now upserts + emits the profile's `primary_model` (guardrailed);
  #2 `server_default` on the timestamp columns so `create_all`-before-migration can't NOT-NULL-fail
  the backfill; #3 the chat hot-path selection read + GET hydration degrade to the profile bridge on a
  DB error (migration lag → no 500); #4 provenance reports `default` (not `server-hydrated`) when the
  guardrail drops a stale stored key; #6 `_profile_primary` also fail-closes on `ValueError`;
  #9 typed `_build_client` params (no `Any`); #10 shared `required_kind_for_role` helper.
- **Accepted with rationale:** #5 cloud→cloud-primary is now conditional on config integrity (only
  breaks if `primary` is pinned or the cloud key is removed — a misconfig; profiles die in T5);
  #7 a narrow concurrent-first-turn race can skip persistence, degrading to the bridge (same resolved
  model); #8 two PK reads per turn (profile + selection resolvers) — both are transitional and T5
  collapses the profile resolver.
