# FRE-1337 — intent-classification eval harness

Puts Stage 4's deterministic classification (`request_gateway/intent.py::classify_intent`)
next to a model's own classification of the same text against the same taxonomy, and
measures where they disagree. Feeds FRE-1288 with data.

## Arms

1. **Deterministic** — `classify_intent()` called directly. No I/O.
2. **Probe** — a raw, stateless, single-turn LLM call per model
   (`qwen3.6-35b-thinking`, `qwen3.6-27b-ovh`, `claude_sonnet`) with the taxonomy injected
   and *no tools, no history*. Contamination-free by construction — there is nothing for
   `search_memory` to be.
3. **Behavioral** (optional, `--behavioral`) — a live full turn through the **isolated eval
   gateway** (never production), reading tool-call/token/wall-clock signals back from its
   own Elasticsearch.

## Running arms 1+2

No infra beyond network access to the three model deployments and the FRE-375 TEST
substrate's Postgres (for the cost ledger — `make test-infra-up` if it isn't already up):

```bash
make test-infra-up   # if not already running
uv run python -m scripts.eval.fre1337_intent_probe.harness --run-id 2026-08-30
```

Fails loudly (non-zero exit) rather than under-reporting:
- exit 2 if any of the 3 models errored on any fixture (AC-1 — all three are mandatory)
- exit 3 if no model's confusion matrix has a real diagonal cell anywhere (AC-5 — the
  seeded-agreement claim needs live evidence, not just a fixture assertion)

## Running arm 3 (behavioral)

**Rebuild the eval image first** — `seshat-gateway:latest` is reused across `up -d` calls,
so a stale cached build (found 4 months stale during FRE-1337's own verification: it
predated the current `claude_sonnet` model id and couldn't reach the local SLM tunnel)
silently serves old code with no error. Always:

```bash
docker compose -p seshat -f docker-compose.cloud.yml -f docker-compose.eval.yml \
  build seshat-gateway-control
docker compose -p seshat -f docker-compose.cloud.yml -f docker-compose.eval.yml \
  up -d postgres-eval neo4j-eval elasticsearch-eval redis-eval seshat-gateway-control
export NEO4J_PASSWORD=<the eval stack's password>   # same one docker-compose.eval.yml needs
uv run python -m scripts.eval.fre1337_intent_probe.harness --run-id 2026-08-30 --behavioral
```

Two more things verified live during FRE-1337 (2026-08-30), worth knowing before you run
this: (1) the eval gateway's default `AGENT_SLM_BASE_URL` (`http://localhost:8000` inside
its own container) cannot reach the local SLM tunnel — by design, per the compose file's
own comment ("evals must never traverse the Cloudflare egress path"), so any fixture that
routes to the local primary will fail with `LLMConnectionError` unless you explicitly pass
`model=claude_sonnet` (or another cloud key) on `/chat`, or override `AGENT_SLM_BASE_URL`
to the docker bridge gateway IP (`docker network inspect seshat_cloud-sim` for the address)
— pick per your threat model, don't just default it open. (2) Use `-p seshat` (the
project name the live stack already runs under — check with
`docker inspect cloud-sim-seshat-gateway --format '{{index .Config.Labels "com.docker.compose.project"}}'`)
or `docker compose up` creates a second, colliding bridge network under the worktree's own
directory-derived project name.

`make eval-infra-down` only stops the gateway containers — it does **not** wipe
`neo4j-eval`'s volume. The harness owns its own per-fixture wipe (`substrate.py`), so this
is fine for repeated runs; if you want a clean slate at the container level:

```bash
docker compose -p seshat -f docker-compose.cloud.yml -f docker-compose.eval.yml down -v \
  seshat-gateway-control seshat-gateway-treatment postgres-eval neo4j-eval elasticsearch-eval redis-eval
```

### Isolation

- `substrate.py` hardcodes the only URIs this harness will ever touch:
  `bolt://localhost:7689` (`neo4j-eval`) and `http://localhost:9002` (`seshat-gateway-control`).
  Anything else raises `SubstrateGuardError` — this is a string-equality check, not the
  `Environment` enum, because `docker-compose.eval.yml`'s `APP_ENV=eval` actually resolves
  to `Environment.DEVELOPMENT` (`env_loader.py`'s fallthrough has no `EVAL` member).
- Between every fixture in the behavioral arm, the eval graph is fully wiped
  (`MATCH (n) DETACH DELETE n`) — the AC-3 control for FRE-1338's incident (one turn's
  freshly-extracted entities leaking into the next turn's `search_memory`).
- Both eval gateways now point at their own `redis-eval` service (FRE-1342, fixed
  2026-08-30) — the shared production Redis DB they used to share was a transport for
  Streams events, not KG data, but it let an eval turn's `request.captured` reach
  production's own consolidator and write to the production knowledge graph. See
  `docker-compose.eval.yml`'s FRE-1342 comment block for the full trace.
- **Known gap, not fixed here** (filed as a follow-up during this ticket's live
  verification): `seshat-gateway:latest` is reused across `up -d` calls and can silently
  serve a stale build with no error (FRE-1341, found 4 months stale) — always rebuild
  first, per the section above.

## Output

`telemetry/evaluation/fre1337-intent-probe/<run-id>.json` (every row + the raw prompt) and
`<run-id>.md` (the confusion matrix, one table per model).
