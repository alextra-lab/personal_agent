# FRE-1341 — Eval gateway image freshness

## Problem

`docker-compose.eval.yml`'s gateway services use `image: seshat-gateway:latest` + `build:`,
but `docker compose up` only builds when the tag is missing. A cached image can silently
serve months-stale code — `/health` reports `"status": "healthy"` regardless. Bit twice:
once serving a 4-month-old catalog (no `claude-sonnet-5` id), once masking a config guard
(ADR-0112 allowlist) that didn't exist yet in the cached build.

## Acceptance criteria (from the ticket, verbatim intent)

- **AC-1**: staleness detection proven by a seeded negative (build → change src → run →
  refuses/warns, naming staleness → rebuild → passes).
- **AC-2** (load-bearing): a harness can ask an *already-running* gateway what it's running
  (git SHA or build timestamp) and compare — not just "bring-up now always rebuilds".
- **AC-3**: state the wall-clock cost of the chosen route, measured.
- **AC-4**: retire `scripts/eval/fre1337_intent_probe/README.md`'s manual "rebuild by hand"
  workaround.

Master's routing note: AC-2 is load-bearing because most eval runs point at a stack someone
else already started — an always-`--build` bring-up doesn't protect that case.

## Design — REVISED after codex plan-review (see addendum at bottom)

Bake a **content fingerprint of the exact files Docker copies into the image** — not the git
commit SHA — into the image, expose it over `/health`, and give harnesses a small assertion
helper that compares a running gateway's reported fingerprint against one computed from the
current working tree. Also make `make eval-infra-up` always rebuild, as defense at the
bring-up path itself (cheap once cached, see AC-3 measurement).

**Why a content fingerprint, not `git rev-parse HEAD`** (codex plan-review finding, BLOCKER):
AC-1's seeded negative is "build → change something in `src/` → run → refuses". A change
under `src/` does not have to be committed to make a running image stale relative to what's
on disk — and `git rev-parse HEAD` is blind to exactly that case (dirty/untracked edits under
a copied path leave HEAD unchanged). A sha256 over the actual on-disk content of every path
`Dockerfile.gateway` COPYs — computed fresh, right before build, and again at check time —
catches both committed and uncommitted staleness, which is what "is this gateway running the
code in front of me right now" actually means.

### 1. New `scripts/eval/gateway_freshness.py` (written first — the Makefile and Dockerfile both call into it)

```python
#: The exact paths Dockerfile.gateway COPYs into the image (plus the Dockerfile itself —
#: editing a RUN/COPY instruction changes what the image contains just as much as editing
#: src/). Keep this in sync with Dockerfile.gateway's COPY list.
BUILD_INPUT_PATHS = (
    "src", "config", "docs/skills", "docker/mcp",
    "pyproject.toml", "uv.lock", "Dockerfile.gateway",
)

def compute_build_fingerprint(repo_root: Path) -> str:
    """sha256 over the on-disk content of every Docker build input, path + bytes.

    Reads the working tree directly, not the git index — an uncommitted or untracked
    edit under any of these paths changes the fingerprint. That's the point: this is
    what makes AC-1's "build, then edit src/ without committing, then check" sequence
    detectable, which `git rev-parse HEAD` alone cannot do.
    """
    ...  # walk BUILD_INPUT_PATHS, sorted file list, hash relative-path + contents

class GatewayStaleError(RuntimeError): ...

@dataclass(frozen=True)
class FreshnessResult:
    fresh: bool
    running_fingerprint: str | None
    expected_fingerprint: str

async def check_gateway_freshness(client: httpx.AsyncClient, base_url: str, repo_root: Path) -> FreshnessResult: ...
async def assert_gateway_fresh(client: httpx.AsyncClient, base_url: str, repo_root: Path) -> FreshnessResult:
    # raises GatewayStaleError if fingerprints differ, or if the gateway reports
    # None/"unknown" (missing build info is treated as stale, never as fresh),
    # or if the gateway is unreachable/non-2xx. Message names both fingerprints
    # (short form) and prints the exact rebuild command:
    #   docker compose -p seshat -f docker-compose.cloud.yml -f docker-compose.eval.yml \
    #     build seshat-gateway-control seshat-gateway-treatment
```

`compute_build_fingerprint` is sync (plain file I/O, easy to unit test); call sites in async
code wrap it in `asyncio.to_thread` per the async-I/O convention. `main()`/CLI mirrors
`harness.py`'s style (structlog to stderr for the check path). Two CLI modes:
- `uv run python -m scripts.eval.gateway_freshness <url>` — assert-fresh check, exit 0/1.
- `uv run python -m scripts.eval.gateway_freshness --print-fingerprint` — prints just the
  current fingerprint to stdout (plain, no structlog) — a value contract for shell capture,
  the same shape as `git rev-parse HEAD`. This is what the Makefile shells out to.

### 2. `Dockerfile.gateway`

Add, immediately before `HEALTHCHECK` (after `RUN uv sync`, so the arg never invalidates the
expensive `COPY` / `uv sync` layers on its own — though note a `docs/skills/` change already
invalidates that layer independently, since it's itself a COPY'd path; codex plan-review
finding #5):

```dockerfile
ARG BUILD_FINGERPRINT=unknown
ENV AGENT_BUILD_FINGERPRINT=$BUILD_FINGERPRINT
```

### 3. `src/personal_agent/config/settings.py`

New field on `AppConfig`, next to `version`:

```python
build_fingerprint: str | None = Field(
    default=None,
    description=(
        "sha256 over every file Dockerfile.gateway copies into the image "
        "(scripts/eval/gateway_freshness.py computes it; Dockerfile ARG/ENV bakes it in "
        "at build time). None outside a container build. Lets a harness assert a running "
        "eval gateway reflects the code it's about to test, including uncommitted changes "
        "(FRE-1341)."
    ),
)
```

### 4. `src/personal_agent/service/app.py`

`health_check()` gains one field:

```python
"build_fingerprint": settings.build_fingerprint,
```

### 5. `docker-compose.eval.yml`

`x-seshat-gateway-eval-base.build` gains:

```yaml
build:
  context: .
  dockerfile: Dockerfile.gateway
  args:
    BUILD_FINGERPRINT: ${BUILD_FINGERPRINT:-unknown}
```

### 6. `Makefile`

`eval-infra-up` computes the fingerprint via the module above and passes `--build`. Existing
exact-string test (`test_eval_compose_redis_isolation.py::test_makefile_eval_infra_up_names_eval_services_explicitly`)
gets updated in the same commit to match the new `--build` flag (codex plan-review finding #3
— this is an edit to that existing assertion, not a new sibling one):

```makefile
eval-infra-up:          ## Start eval infra (names eval services explicitly — FRE-1342, never the file union; always rebuilds — FRE-1341: a cached image can silently serve stale code)
	@BUILD_FINGERPRINT=$$(uv run python -m scripts.eval.gateway_freshness --print-fingerprint) docker compose -f docker-compose.cloud.yml -f docker-compose.eval.yml up -d --build postgres-eval neo4j-eval elasticsearch-eval redis-eval seshat-gateway-control seshat-gateway-treatment
```

### 7. `scripts/eval/fre1337_intent_probe/behavioral.py`

`run_behavioral_arm` calls `assert_gateway_fresh(http, EVAL_CHAT_BASE_URL, repo_root())` once,
at the top, before the fixture loop (reusing the client already opened there). This is what
retires the manual README workaround (AC-4) — the harness now refuses loudly itself instead of
relying on the operator remembering to rebuild by hand.

### 8. `scripts/eval/fre1337_intent_probe/README.md`

**Narrower than originally planned** (codex plan-review finding #4): the manual `up -d`
command in "Running arm 3" uses `-p seshat` deliberately, to avoid a second, colliding
bridge network under a worktree's own directory-derived project name (the README's own
"Isolation" section explains why). `make eval-infra-up` does **not** pass `-p seshat` — that's
a separate, pre-existing gap this ticket doesn't touch (out of scope: fixing project-naming
across every `*-infra-up` target is a bigger, unrelated change). So AC-4's retirement removes
only the manual **`build`** step and its surrounding "rebuild first" paragraph — the `up -d
-p seshat ...` command stays exactly as it is. Replace the removed paragraph with: the harness
itself now asserts freshness before running arm 3 and refuses loudly (naming the rebuild
command) if the running gateway is stale — no need to rebuild by hand first. Update the "Known
gap, not fixed here" bullet under Isolation to say it's fixed, pointing at
`scripts/eval/gateway_freshness.py`.

## Test plan (TDD, failing-first)

1. New `tests/scripts/test_gateway_freshness_fingerprint.py` — `compute_build_fingerprint`:
   deterministic (same tree → same hash), changes when a file's content under a build-input
   path changes (create a temp dir standing in for a build-input path, or monkeypatch
   `BUILD_INPUT_PATHS` to a tmp_path fixture and mutate a file — this is the core unit that
   makes AC-1's dirty-tree case work, so it needs direct coverage, not just the mocked
   check-layer test below).
2. `tests/test_config/test_settings.py` (extend `TestAppConfig`) — `build_fingerprint`
   defaults `None`; reads `AGENT_BUILD_FINGERPRINT`.
3. New `tests/test_service/test_health_build_fingerprint.py` — call `health_check()` directly
   (established pattern: `from personal_agent.service.app import health_check`), patch
   `personal_agent.service.app.settings.build_fingerprint`, assert it's echoed in the response.
4. New `tests/scripts/test_gateway_freshness.py` — unit-tests `check_gateway_freshness` /
   `assert_gateway_fresh` against a mocked `httpx.AsyncClient` (`httpx.MockTransport`) and a
   monkeypatched `compute_build_fingerprint`: match → fresh; mismatch → `GatewayStaleError`
   naming both fingerprints + the rebuild command; `build_fingerprint` missing/`"unknown"` →
   treated as stale, not silently fresh; non-2xx/unreachable → raises, never silently fresh.
5. New `tests/scripts/test_eval_compose_build_args.py` — same `docker compose ... config`
   render pattern as `test_eval_compose_redis_isolation.py` (skipped if no docker CLI):
   asserts both gateway services' rendered `build.args.BUILD_FINGERPRINT` resolves from the
   shell env.
6. **Edit** (not extend-only) `test_eval_compose_redis_isolation.py::test_makefile_eval_infra_up_names_eval_services_explicitly`
   — update its exact-string assertion to include `--build`; add an assertion that the recipe
   line sets `BUILD_FINGERPRINT=` via the freshness module (codex plan-review finding #3).
7. New test in `tests/evaluation/` (mirroring existing fre1337 test files) — patches
   `assert_gateway_fresh` and asserts `run_behavioral_arm` calls it once with
   `EVAL_CHAT_BASE_URL` before touching the fixture loop.

## AC-1 live seeded-negative proof (not just unit-mocked)

On this VPS, with real Docker, using this ticket's own implementation work as the seed (real
uncommitted `src/` edits, not a synthetic change — and the case `git rev-parse HEAD` alone
would have missed):
1. Once the fingerprint plumbing (Dockerfile/compose/Makefile/settings/health) is implemented
   and committed, `make eval-infra-up` → confirm `curl -s localhost:9002/health | jq
   .build_fingerprint` matches `uv run python -m scripts.eval.gateway_freshness
   --print-fingerprint` run against the working tree at that moment (call it fingerprint A).
2. Continue implementing — wire `gateway_freshness` into `behavioral.py`, uncommitted. This is
   a real edit under `src`-adjacent... actually under `scripts/eval/`, which is **not** one of
   `BUILD_INPUT_PATHS` (the gateway image doesn't COPY `scripts/`) — so this specific edit
   would NOT move the fingerprint. Use a `src/` edit instead: touch something already planned
   for this ticket under `src/personal_agent/` (e.g. the settings/health changes if not yet
   built at step 1, or make a throwaway single-line edit to a `src/` file and revert it after
   the proof). Don't commit it.
3. Without rebuilding: `uv run python -m scripts.eval.gateway_freshness http://localhost:9002`
   → expect nonzero exit, loud message naming the stale fingerprint vs the current (uncommitted)
   working-tree fingerprint, plus the rebuild command.
4. `make eval-infra-up` again (rebuilds against the now-current tree) → rerun the same command
   → expect exit 0.

Captured verbatim (commands + output) in the PR body / handoff comment as AC-1's evidence.

## AC-3 measurement

Time, on this VPS:
- `docker compose -f docker-compose.cloud.yml -f docker-compose.eval.yml build
  seshat-gateway-control seshat-gateway-treatment` with nothing changed since the last build
  (full cache hit).
- The same command immediately after touching a file under `src/` (forces `COPY src/` +
  `uv sync` to rerun).

Both numbers recorded in the PR body, since that's what `make eval-infra-up`'s new
always-`--build` costs on every bring-up.

## Risk tier

Standard/Complex — touches `src/` (config, health endpoint) and infra config
(Dockerfile/compose/Makefile). Codex plan-review required before implementation.

## Not in scope

Production gateway's build path (ticket's own "Not in scope"). Wiring the same freshness
guard into FRE-481/FRE-1286's harnesses — those are separate tickets' surface; `gateway_freshness.py`
is written generically so they can adopt it later without rework. `eval-infra-up`'s missing
`-p seshat` (pre-existing, independent of this ticket — see §8 above) — noted, not fixed here;
worth its own ticket if the worktree-vs-primary-checkout network collision it enables ever
actually bites someone.

## Addendum — codex plan-review (2026-08-31)

Original v1 of this plan compared running-gateway `git rev-parse HEAD` against local HEAD.
Codex plan-review (`codex:rescue`) caught that this fails AC-1's literal seeded-negative test
for an uncommitted change, and flagged two smaller gaps (Makefile exact-string test breakage,
`-p seshat` inconsistency in the README edit) plus one minor cache-claim overstatement (already
corrected above: `docs/skills/` changes invalidate the `uv sync` layer regardless of the SHA
arg's placement). All four are folded into the design above; the git-SHA field was replaced
outright by the content-fingerprint field rather than kept alongside it, per Simplicity First
— one field, one source of truth for "is this stale."
