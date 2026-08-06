# FRE-1166 — Retire the local-model provisioning path

**Ticket**: [FRE-1166](https://linear.app/frenchforest/issue/FRE-1166) — Approved, In Progress
**Branch**: `fre-1166-retire-local-model-provisioning-path`
**Related**: FRE-340 (canceled, was the seed), FRE-1123 (removed gateway `depends_on`), FRE-1165 (system prompt fix, merged), ADR-0112 D4 (the *real* local-fallback design — an 8B model, not this 0.6B path)

## Scope (from ticket)

Retire the complete but disconnected provisioning chain for the local llama.cpp
embedding/reranker containers: the transfer script, both compose service
definitions, and the models mount. Nothing calls this path — embedding is OVH-managed,
reranking is Voyage with a Mac-tunnel fallback (neither routes through these containers).

## Scoping findings (this session)

- **Q1 (abandoned vs. deliberate fallback)**: No ADR treats the 0.6B llama.cpp
  containers as an outage fallback. ADR-0112 D4's actual local-fallback design requires
  an **8B** model ("must be the 8B — not today's 0.6B — to preserve the vector
  space") and isn't provisioned yet. These containers are architecturally unrelated to
  that design, not an implementation of it.
- **Q2 (eval profile)**: Eval never calls the local containers at the application level
  (both eval gateways inherit the `private` substrate profile → OVH/Voyage). But
  `docker-compose.eval.yml`'s shared `depends_on` block **does** force `embeddings`/
  `reranker` to `service_healthy` before either eval gateway will start — a real
  functional defect independent of the retire-vs-keep call.
- **Conflict found**: `tests/scripts/test_gateway_depends_on.py::test_embeddings_and_reranker_service_definitions_still_exist`
  was written for FRE-1123 to lock in that the service blocks survive, and
  `docs/guides/CLOUD_DEPLOYMENT.md:436` frames them as "retained... for optional manual
  testing only." FRE-1166's own description names this survival as the residue it exists
  to finish (FRE-1123 only removed `depends_on`; the definitions were explicitly left for
  this ticket). Plan below reverses that test's assertion as part of the ticket's intended
  scope — flagged for explicit confirmation before implementing.

## Files touched

| File | Change |
|---|---|
| `infrastructure/scripts/transfer-models.sh` | Delete (68 lines) |
| `Dockerfile.llmserver` | Delete (56 lines) — only consumer was the two services below |
| `docker-compose.cloud.yml` | Remove lines 210–301 (comment block + `embeddings` + `reranker` services, including the two `/opt/seshat/models:/models:ro` mounts); fix stale line-330 comment (`Depends on: ... embeddings, reranker, searxng` → drop the two dead names) |
| `docker-compose.eval.yml` | Remove `embeddings`/`reranker` entries (lines 41–44) from `x-seshat-gateway-eval-base`'s `depends_on` |
| `tests/scripts/test_gateway_depends_on.py` | Replace `test_embeddings_and_reranker_service_definitions_still_exist` with a test asserting they're **gone**; two existing tests need no change (they already assert gateway doesn't depend on them) |
| `tests/scripts/test_eval_compose_depends_on.py` (new) | Assert the eval-rendered `depends_on` for `seshat-gateway-control`/`-treatment` no longer names `embeddings`/`reranker` |
| `docs/guides/CLOUD_DEPLOYMENT.md` | Trim: topology block (55-56), host prereq (89), §3.3 transfer step (120-135), optional-services table rows (220-232), startup-order example commands (253-256), "do not use local containers" note (398-436, rephrase — containers no longer exist to warn about), troubleshooting section (560-566) |

**Left alone** (historical/point-in-time records, not living state — surgical-changes
discipline): `docs/architecture/2026-05-08-fre-214-vps-topology-audit.md`,
`docs/architecture_decisions/ADR-0111-*.md` (dated co-tenant survey), all
`docs/superpowers/plans/*` and `docs/research/*` references, `telemetry/evaluation/*`.
**Not touched**: `src/personal_agent/memory/embeddings.py`'s `localhost:8503` default
endpoint string — an app-level default, out of this ticket's stated scope (script/
containers/mount only); noted in the PR/ticket comment, not fixed here. The three
standalone eval probe scripts (`fre821_embedder_failover_probe`, `fre435_memory_recall`,
`fre720_insights_separation`) that assume a manually-started `localhost:8503` — already
broken today (FRE-1123 removed the only thing that auto-started it); deleting the compose
service removes their manual on-ramp entirely. Noted as a heads-up in the ticket comment,
not fixed — they're standalone manual benchmarks, not part of any pipeline this ticket owns.

## Acceptance criteria (this ticket)

1. `infrastructure/scripts/transfer-models.sh` and `Dockerfile.llmserver` no longer exist.
2. `docker-compose.cloud.yml` declares neither `embeddings` nor `reranker` services, and
   no `/opt/seshat/models` mount remains.
3. `docker-compose.eval.yml`'s `seshat-gateway-control`/`-treatment` no longer wait on
   `embeddings`/`reranker` health — fixes the forced-dead-container-start defect.
4. `docker compose -f docker-compose.cloud.yml config` and
   `docker compose -f docker-compose.cloud.yml -f docker-compose.eval.yml config`
   both render cleanly (no dangling service references).
5. No remaining reference to `transfer-models.sh`, `Dockerfile.llmserver`, `embeddings:8503`,
   or `reranker:8504` in compose files or `docs/guides/CLOUD_DEPLOYMENT.md`.

## Steps (atomic, TDD)

1. Write the new/updated tests first (red): rewrite
   `test_embeddings_and_reranker_service_definitions_still_exist` →
   `test_embeddings_and_reranker_service_definitions_removed` (asserts absence); add
   `tests/scripts/test_eval_compose_depends_on.py` asserting eval depends_on excludes
   `embeddings`/`reranker`. Confirm both fail against current `main`.
2. Delete `infrastructure/scripts/transfer-models.sh`, `Dockerfile.llmserver`.
3. Edit `docker-compose.cloud.yml`: remove the comment block + two service definitions
   (lines 210–301), fix the line-330 stale comment.
4. Edit `docker-compose.eval.yml`: remove the two `depends_on` entries.
5. Confirm the two new/rewritten tests pass; run
   `docker compose -f docker-compose.cloud.yml config` and the two-file eval render to
   confirm AC4.
6. Trim `docs/guides/CLOUD_DEPLOYMENT.md` per the table above.
7. `git grep -n "transfer-models\|Dockerfile.llmserver\|embeddings:8503\|reranker:8504"`
   across compose files + the guide to confirm AC5 clean.
8. Quality gates (Step 8): `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`. Self-review (`code-review` skill, `low` effort —
   infra/config/docs only, no `src/` logic).

## Test commands

```bash
make test-file FILE=tests/scripts/test_gateway_depends_on.py
make test-file FILE=tests/scripts/test_eval_compose_depends_on.py
docker compose -f docker-compose.cloud.yml config >/dev/null
docker compose -f docker-compose.cloud.yml -f docker-compose.eval.yml config >/dev/null
make test
make mypy
make ruff-check
```

## Risk tier

**Standard** — no `src/` logic change, but touches production `docker-compose.cloud.yml`
topology and reverses a prior deliberate test/doc decision (FRE-1123). Codex plan-review
+ explicit owner approval before coding, per the skill's "when in doubt, treat as Standard."

## Post-review findings (expanded scope, owner-confirmed)

Codex's plan review found real consumers I'd missed: `scripts/study/run_baseline.py`
(+README), `scripts/eval/fre435_memory_recall/ab_multipath.py`, and
`scripts/eval/fre720_insights_separation/separation_probe.py` (+README) all instructed
`docker start cloud-sim-embeddings` before running. Investigating further: `config/models.yaml`'s
`embedding:` entry was pointed directly at the OVH endpoint on **2026-07-19** (`ba81b8985`,
"Replaces the retired local 0.6B") — since `model_endpoint:embedding` resolution is what
*every* substrate profile (`private`/`test`/`dev`) reads, all three scripts' "start the
local container" instructions have been stale/dead since that cutover, not live
dependencies. Confirmed with the owner ("this is all historical tech debt... we will not
go backwards") — full retirement proceeded, plus:

- Updated the 3 scripts' docstrings/READMEs and two runtime error-hint strings
  (`run_baseline.py`, `ab_multipath.py`) that told operators to `docker start
  cloud-sim-embeddings` on connection failure — now point at the real remediation
  (`AGENT_MANAGED_EMBEDDING_TOKEN` / OVH reachability).
- `.env.example` had a dead `AGENT_EMBEDDING_ENDPOINT`/`AGENT_RERANKER_ENDPOINT` override
  (not a real settings field) — replaced with the actual production secrets
  (`AGENT_MANAGED_EMBEDDING_TOKEN`, `AGENT_VOYAGE_API_KEY`).
- `docs/runbooks/embedder-managed-adoption.md` (master-owned, ADR-0112 D4/D6 adoption
  runbook) had a stale precondition claiming production is "still `private` (local 0.6B)"
  and steps assuming the 0.6B container still exists to mirror/stop. Fixed the precondition
  and the two steps this deletion directly breaks; **left the runbook's substantive
  local-8B-fallback sequence untouched** (out of scope, master-owned).
- **Flagged, not fixed**: the runbook correction surfaces that ADR-0112's AC-5/AC-6-gated
  migration (re-embed, verified same-model local-8B fallback) was never run — the July 19
  cutover bypassed it via a direct `models.yaml` edit. Production has had no verified
  embedder fallback since. Raised to master in the ticket comment as a discovered gap, not
  resolved by this ticket.
- **Deliberately left alone**: `scripts/eval/fre435_memory_recall/separation_benchmark.py`
  (FRE-695's completed reranker-choice benchmark) still has an `"rr-0.6b-cpu"` candidate
  config pointing at `localhost:8504` — one of several fixed historical comparison
  candidates in an already-decided benchmark, not an active workflow. Left as-is; noted in
  the ticket comment rather than edited, to avoid scope creep into a completed research
  artifact.
