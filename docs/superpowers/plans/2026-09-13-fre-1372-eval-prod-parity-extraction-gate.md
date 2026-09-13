# FRE-1372 (reopen) — eval production parity + extraction settle gate

Ticket: https://linear.app/frenchforest/issue/FRE-1372
Owner reopen comment: 2026-09-13 20:10 UTC (`bf1fd105`)
Prior Verify Failed comment: 2026-09-07 08:23 UTC (`4382dc99`)

## Background

FRE-1372's structural cross-arm isolation (`IsolatedArmRunner`, PR #1095) is merged and
correct. It could not be proven live: `scripts/eval/fre1372_isolation_probe.py` ran at
exit 0, but the eval Neo4j held zero nodes, because `docker-compose.eval.yml` never sets
the memory/recall/embedder settings that turn extraction on — both eval gateways ran
`settings.py`'s defaults (memory graph OFF, and every recall arm OFF). The probe's own
`require_nonzero=True` settle check caught `extraction_settled=False` and logged it, but
nothing made the run fail on it, so an empty-vs-empty comparison reported a pass.

The reopen names two separable fixes:

1. **Production behaviour parity for both eval gateways**, from a committed file, so
   extraction actually runs and AC-1/AC-2 become measurable.
2. **The probe must fail loudly when extraction does not settle**, so an empty-graph run
   cannot report a pass again.

## Fix 1 — production behaviour parity

**Source of truth:** the owner's uncommitted `~/fre1498-harness/override-prodparity.yml`
covers `seshat-gateway-treatment` only, with 22 `AGENT_*` keys interpolated from `.env`.
The committed version must cover `seshat-gateway-control` too, and pass nothing that
addresses production storage.

Two parts:

### 1a. `docker-compose.eval.yml`

Add the same 22-key `environment:` block (behaviour flags: memory graph, proactive
memory, multipath/relevance-bounded/lexical/multiquery recall, similarity floor,
grounding mode, location, second brain, skill routing, owner name, history depth,
substrate profile, managed-embedder endpoint/model/token, local fallback embedding
model, Voyage/Perplexity/Linear keys, Captain's Log reflection interval) to **both**
`seshat-gateway-control` and `seshat-gateway-treatment`. None of these 22 keys overlap
with the existing eval-substrate `environment:` overrides (`AGENT_DATABASE_URL`,
`AGENT_NEO4J_URI`, `AGENT_ELASTICSEARCH_URL`, `AGENT_EVENT_BUS_REDIS_URL`,
`AGENT_SYSGRAPH_DATABASE_URL` is not among them and stays absent), so nothing already
guarding eval substrate isolation changes.

Deliberately NOT added (matches the owner's file's own header): `AGENT_SYSGRAPH_DATABASE_URL`
(points at production Postgres), R2/artifact, CORS/WS/PWA, freshness, insights, feedback
polling.

### 1b. `Makefile` — `eval-infra-up` / `eval-infra-down`

Add `--env-file /opt/seshat/.env` to both compose invocations, as a **global** `docker
compose` flag placed BEFORE the `up`/`down` subcommand (verified: `--env-file` after
`up`/`down` errors with `unknown flag`) — i.e.
`docker compose -f docker-compose.cloud.yml -f docker-compose.eval.yml --env-file /opt/seshat/.env up -d --build ...`.
Docker Compose's default
`.env` auto-load reads whatever `.env` sits in the **current working directory** — each
worktree carries its own stale, partial `.env` copy (verified: this worktree's copy is
missing `AGENT_GROUNDING_VERIFICATION_MODE`, `AGENT_LOCATION_ENABLED`,
`AGENT_ENABLE_SECOND_BRAIN`, `AGENT_LINEAR_API_KEY`, `AGENT_CAPTAINS_LOG_REFLECTION_MIN_INTERVAL_SECONDS`,
among others). An explicit `--env-file /opt/seshat/.env` makes interpolation always read
the one current, real file, regardless of which worktree invokes `make eval-infra-up`.

### 1c. Structural guard — `src/personal_agent/config/settings.py`

Add `_validate_eval_deployment_isolation`, a `model_validator(mode="after")` gated on
`self.deployment_profile == "eval"`: assert `neo4j_uri`, `elasticsearch_url`,
`database_url`, `database_admin_url` each resolve to a hostname ending in `-eval`
(the compose service-naming convention this stack already uses throughout —
`postgres-eval`, `neo4j-eval`, `elasticsearch-eval`, `redis-eval`). Raise `ValueError`
naming every offender otherwise. This is the ticket's "add a check that fails when an
eval gateway's resolved substrate URLs are not the -eval hosts" — enforced at process
boot inside the container itself, so it cannot be skipped by a compose-file mistake or a
forgotten flag, mirroring this file's existing `_validate_dev_test_profile_isolation` /
`_validate_owner_storage_allowlist` pattern.

The existing `_validate_owner_storage_allowlist` cannot serve this purpose: its
allowlist (`AGENT_OWNER_STORAGE_ALLOWLIST` in `docker-compose.eval.yml`) deliberately
lists *both* `neo4j`/`postgres`/`elasticsearch` (prod) and their `-eval` twins, so a
leaked prod hostname on an eval gateway passes it silently.

Add `_is_eval_host(uri: str) -> bool` next to the other fingerprint helpers in
`_substrate_fingerprint.py` (parse hostname, `.endswith("-eval")`).

## Fix 2 — probe fails loudly on unsettled extraction

`scripts/eval/fre1372_isolation_probe.py`: both `run_turn` calls pass `arm="control"`, so
`ArmTurnResult.arm` is identical for both and cannot name which call failed. Extract a
pure function `_extraction_failure(*, first: ArmTurnResult, second: ArmTurnResult) -> str | None`
that names failures by call order + `session_id` (e.g. "first turn (session ...)"),
never by `.arm`. `amain()` calls it right after both `run_turn` calls, before the graph
read; on a non-`None` result, prints the message and returns 1 immediately — never
reaches the leaked-node comparison.

## Tests

- `tests/personal_agent/config/test_eval_deployment_isolation_validator.py` (new,
  mirrors `test_dev_test_profile_isolation_validator.py`'s shape): raises for each of the
  four fields when non-`-eval` under `deployment_profile="eval"`; silent for `local`/
  `cloud` profiles regardless of host; silent when all four are `-eval` hosts; message
  names the offending field.
- `tests/personal_agent/config/test_docker_compose_eval_yaml.py` (new, pure YAML load,
  no docker/env needed): asserts `seshat-gateway-control` and `seshat-gateway-treatment`
  declare the same 22-key behaviour-settings set in `docker-compose.eval.yml` — a parity
  check so a future edit to one service and not the other is caught.
- `tests/evaluation/test_fre1372_eval_isolation.py` (existing file): add
  `_extraction_failure` unit tests — both settled -> `None`; arm1 unsettled -> message
  naming arm1; arm2 unsettled -> message naming arm2.

## Fix 1/2 do not close AC-1/AC-2 — the live run

AC-1 and AC-2 need one live two-arm run through the fixed eval gateway. **Blocked on a
decision the ticket itself asks to have made before firing**: `config/model_roles.yaml`'s
`entity_extraction` binding is pinned to `gpt-5.4-mini` (OpenAI, paid, $0.75/$4.50 per
MTok) with no per-profile override — there is no way to run extraction on the local
model without a role-binding change outside this ticket's scope. The reopen comment
says: "If production's role bindings would send any role to a paid provider ... stop and
ask master before firing." `entity_extraction` does. Surfacing this to the owner before
`make eval-infra-up` / firing the probe.

## Quality gates

`make test` · `make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files`.
Diff class: touches `src/personal_agent/config/settings.py` (a validator, additive-only,
no behaviour change outside `deployment_profile=="eval"`) and eval-only infra
(`docker-compose.eval.yml`, `Makefile`, `scripts/eval/`) — no production write path,
no schema change. Self-serve (feature-dev:code-reviewer + security-review), not escalated.
