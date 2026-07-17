# FRE-895 — Scrub the real deployment domain from the public repo (HEAD + guard)

Ticket: https://linear.app/frenchforest/issue/FRE-895
Backing: owner's standing no-deployment-identifiers-in-public-repo policy + ADR-0119 config-surface hygiene.
Predecessor: FRE-893 config-audit (PR #548).

Note on this document: once the guard script (§A) lands, no tracked file may spell the real domain
contiguously — including this plan. Everywhere below that needs an illustrative host, it uses the same
placeholder scheme the implementation adopts (`<subdomain>.example.com`), never the real string.

## Scope confirmed by inventory

`git grep -il` for the real domain → **104 tracked files**, 442 line hits, one literal domain with
subdomains `agent.`, `api.`, `artifacts.`, `es.`, `graph.`, `monitoring.`, `seshat.`, `slm.`. No case
variation, no collision with the unrelated word "FrenchForest" (Linear team name — out of scope,
untouched). A straight literal → `example.com` swap is safe everywhere it is purely textual; the
placeholder scheme is `<subdomain>.example.com` (mirrors the real subdomain structure so docs/tests read
naturally).

By directory: docs 50, src 10, scripts 10, tests 8, telemetry 8, config 8, seshat-pwa 5, root 4 (`Makefile`,
`CLAUDE.md`, `.env.example`, `docker-compose.cloud.yml`), e2e 1.

## Design call this ticket anticipated (its own guard rail: "if functional parameterization needs a
design call, escalate rather than hardcode")

Several FUNCTIONAL sites are **not simple settings-default swaps** — they are static YAML/JSON/Caddyfile/
Dockerfile/script values with **no existing env-override path**, and if placeholder-swapped without
adding one, prod breaks silently (Mac SLM tunnel calls, CF-Access header injection, artifact CSP, Neo4j
Bolt advertised address, the PWA's build-time artifact-host detector, a live eval benchmark). Verified via
`/opt/seshat/.env` (gitignored, real values) and **independently re-verified by a Codex second-opinion
pass reading every named file** (2026-07-17):

- `AGENT_ARTIFACTS_PUBLIC_BASE_URL` is **already** set → `settings.artifacts_public_base_url`
  (`config/settings.py:727-730`) is a live, working override path. Nothing new needed there.
- `NEXT_PUBLIC_SESHAT_URL` is present in `.env` but **commented out** — dead. The real mechanism is
  `docker-compose.cloud.yml`'s `SESHAT_URL` **runtime** env var → PWA's `/api/runtime-config` route reads
  `process.env.SESHAT_URL` per-request (FRE-339 design — image is deploy-portable;
  `seshat-pwa/src/app/api/runtime-config/route.ts:14-17`, `seshat-pwa/src/app/layout.tsx:38-63`). Currently
  hardcoded literally in the compose file (`docker-compose.cloud.yml:445-447`), not sourced from `.env`.
- `NEXT_PUBLIC_ARTIFACTS_HOST` (`seshat-pwa/src/components/MarkdownContent.tsx:17-18`) is a **build-time**
  Next.js var. `Dockerfile.pwa:22-24` runs `npm run build` with no ARG/ENV for that var, so the deployed
  image's `process.env.NEXT_PUBLIC_ARTIFACTS_HOST` is always `undefined` and the code **always** falls
  through to the hardcoded literal — the "fallback" is actually the sole load-bearing value in prod today.
  Swapping just the string breaks the PWA's artifact-link detection unless a real build arg is also wired.
  (Pre-existing duplication, not introduced by this ticket: this literal and `AGENT_ARTIFACTS_PUBLIC_BASE_URL`
  encode the same host from two independent sources with no shared origin of truth — not fixing that
  duplication here, just placeholder-swapping both consistently with the same wiring pattern.)
- `config/models*.yaml` (5 profiles: `models.yaml`, `models.cloud.yaml`, `models.benchmark-{4b,4b-f16,8b}.yaml`)
  hardcode the real SLM tunnel endpoint directly, with **zero** env-substitution support in
  `model_loader.py:51-69` / `config/loader.py:46-52` (plain `yaml.safe_load`). `config/models.cloud.yaml` is
  baked into the prod image (per project CLAUDE.md). No existing mechanism to override at runtime.
- `_SLM_TUNNEL_HOSTNAME` is duplicated as an **identical hardcoded literal in 3 files**
  (`llm_client/client.py:58`, `memory/embeddings.py:61`, `memory/reranker.py:36`) — used as a substring
  match against the endpoint URL (`client.py:400-405`, `embeddings.py:379-384`, `reranker.py:160-162`) to
  decide whether to inject CF-Access headers. Settings already has the CF credential fields
  (`config/settings.py:1950-1966`) but no SLM tunnel host/base field — this has to stay in lockstep with
  the real tunnel host or CF-Access header injection silently stops firing.
- `config/artifact_lib_substitution_map.json`'s `"origin"` key is loaded and used directly
  (`storage/artifact_export.py:134-140,210-213` — `raw["origin"]`, `sub_map.origin`) to match/rewrite
  `/lib/` asset URLs during artifact export. Load-bearing.
- **`config/artifact_lib_manifest.json`'s `"origin"` key is a *separate* load-bearing site the first pass
  missed** (Codex catch): consumed by `load_lib_manifest()` in `observability/artifact_envelope/spec.py:117-158`,
  then used for **live** `/lib/` verification in `scripts/verify_artifact_envelope.py:96-113`. That script
  already has a simpler existing override — `--origin` (`scripts/verify_artifact_envelope.py:169-170`) /
  `make verify-lib ORIGIN=...` (`Makefile:157-158`) — but the *default* invocation (no `ORIGIN` given) would
  silently probe the placeholder host if the manifest itself isn't also settings-driven.
- `observability/artifact_envelope/spec.py`'s `EXPECTED_CSP_DIRECTIVES` (`spec.py:21-36`) and
  `tools/artifact_tools.py`'s `_HTML_GENERATION_SYSTEM_PROMPT` (`artifact_tools.py:991-1032`, consumed at
  `:1446-1448`) both hardcode the real artifacts/agent hosts — the CSP verifier compares this **exactly**
  against the real Cloudflare Worker's policy (cross-repo seam, `spec.py`'s own docstring says drift here
  must be alarm-visible; verifier compare at `verifier.py:209-215`), and the system prompt is what the LLM
  is told to embed as literal URLs in every generated artifact. Both must resolve to the *real* artifacts
  origin at runtime, not a frozen placeholder.
- `docker-compose.cloud.yml` hardcodes the real Neo4j Bolt advertised address (`:103-106`) and
  `SESHAT_URL` (`:445-447`) directly in `environment:` blocks (no `${VAR}` indirection), unlike sibling
  secrets in the same file (`NEO4J_PASSWORD`, `GATEWAY_TOKEN_PWA`, etc., which already use the
  `${VAR:?required}` pattern reading from `.env`).
- `config/cloud-sim/Caddyfile` site-block hostnames (`:56,74,96,112`) are Caddy's live routing match keys —
  Caddy supports `{$ENV_VAR:default}` placeholders resolved at startup from the container's environment,
  so this is fixable without new tooling, but the compose `caddy` service needs those vars passed through.
- **`scripts/eval/fre435_memory_recall/separation_benchmark.py` makes live HTTP calls against hardcoded
  SLM endpoints with no override today** (Codex catch): `_SLM_EMBED_URL` (`:132`) and five entries in
  `RERANKER_ARMS` (`:516,522,534,540,546`) hardcode the real endpoint, consumed directly in live requests
  (`_embed_mlx()` at `:236-239`, reranker arms at `:771-830`). The CLI arg parser (`:1118-1168`) has no
  endpoint/base-url override flag. This is a manually-invoked research/benchmark script (not always-on
  service code) that already reads `os.environ` directly elsewhere in the file (`:57`, `setdefault`
  pattern) — so it doesn't go through `personal_agent.config.settings`, but the fix is the same shape:
  read the real base from an env var, default to the placeholder, so an unconfigured run fails loud
  (obviously-fake host) instead of pointing at a broken domain by accident.

**Resolution:** one consistent pattern everywhere a real runtime value is needed: *tracked source holds a
neutral placeholder; a small number of already-`.env`-driven settings fields (existing where possible, a
few new ones only where genuinely absent) carry the real value at runtime.* Concretely:

1. **Reuse `settings.artifacts_public_base_url`** (already exists, already wired) for every artifacts-CDN
   reference: `spec.py` CSP directives, `spec.py`'s `load_lib_manifest()` origin, `artifact_tools.py`
   system prompt, `artifact_export.py`'s substitution-map origin — all four get the same small override:
   if the setting is set, it wins over the file's placeholder value.
2. **Two new settings fields** (`config/settings.py`), each replacing several duplicated literals:
   - `slm_tunnel_base_url: str | None` (default `None`) — the real Mac SLM Cloudflare-tunnel base.
     `model_loader.py` gets one small, targeted post-load step: for any loaded `ModelDefinition.endpoint`
     whose host equals the placeholder host, rewrite host→real base (path preserved) when the setting is
     set; otherwise leave the placeholder as-is (dev/CI/test are unaffected — none of those profiles need
     a live tunnel). The 3 duplicated `_SLM_TUNNEL_HOSTNAME` literals collapse to a direct check against
     `settings.slm_tunnel_base_url`. The same env var name (`AGENT_SLM_TUNNEL_BASE_URL`) is read directly
     via `os.environ` in `separation_benchmark.py` (that script has no settings import and shouldn't gain
     one for this) so there is still exactly one real value, one env var, two readers.
   - `pwa_public_origin: str` (default placeholder) — the PWA's canonical public origin, feeding CSP
     `frame-ancestors` in `spec.py` and used as the default for `docker-compose.cloud.yml`'s `SESHAT_URL`
     interpolation.
3. **`slm_health_url`** already exists as a real field with a hardcoded real-looking default — swap the
   default string to a placeholder; `.env` needs (and will get) an explicit override.
4. **`cors_allowed_origins` / `allowed_ws_origins`** are already `AGENT_`-prefixed list fields — swap
   their defaults to placeholders, add explicit JSON-array overrides to `.env`.
5. **`docker-compose.cloud.yml`**: Neo4j advertised address → `${NEO4J_ADVERTISED_ADDRESS:-graph.example.com:443}`
   (mirrors the file's own existing `${VAR:?msg}` idiom); `SESHAT_URL` → `${SESHAT_URL:-https://agent.example.com}`.
6. **`Dockerfile.pwa`**: add `ARG NEXT_PUBLIC_ARTIFACTS_HOST=artifacts.example.com` +
   `ENV NEXT_PUBLIC_ARTIFACTS_HOST=$NEXT_PUBLIC_ARTIFACTS_HOST` before `RUN npm run build`;
   `docker-compose.cloud.yml`'s `seshat-pwa.build.args` passes the real value from `.env`.
7. **`config/cloud-sim/Caddyfile`**: site-block hosts become `{$AGENT_HOST:agent.example.com}`,
   `{$GRAPH_HOST:graph.example.com}`, `{$ES_HOST:es.example.com}`, `{$API_HOST:api.example.com}`; the
   `caddy` service in `docker-compose.cloud.yml` gets an `environment:` block passing those through from
   `.env` (mirrors the existing `cloudflared` service's `TUNNEL_TOKEN: ${CLOUDFLARE_TUNNEL_TOKEN}` pattern).
8. **`separation_benchmark.py`**: replace the hardcoded endpoint literals with
   `os.environ.get("AGENT_SLM_TUNNEL_BASE_URL", "https://slm.example.com")`-derived values (same env var
   as point 2, read directly since this script has no settings import).
9. **All PROSE/DATA files** (docs, ADRs, telemetry snapshots, eval frozen fixtures, research writeups,
   `.env.example` comments, test literals that are pure fixture values with no config-loading path):
   mechanical literal → `example.com` (subdomain-preserving) text swap. No behavior change.
10. **`.env` additions** (gitignored, real VPS file — adding new keys only, not touching existing ones):
    `AGENT_SLM_HEALTH_URL`, `AGENT_SLM_TUNNEL_BASE_URL`, `AGENT_PWA_PUBLIC_ORIGIN`,
    `AGENT_CORS_ALLOWED_ORIGINS`, `AGENT_ALLOWED_WS_ORIGINS`, `NEO4J_ADVERTISED_ADDRESS`, `SESHAT_URL`,
    `NEXT_PUBLIC_ARTIFACTS_HOST`, `AGENT_HOST`/`GRAPH_HOST`/`ES_HOST`/`API_HOST` (Caddy) — each set to the
    *current* real value, so behavior is unchanged; this is what makes the placeholder-in-source safe.

## File-by-file plan

### A. New guard script (lands after the scrub)
- `scripts/check_no_deployment_identifier.py` — mirrors `scripts/check_no_personal_paths.py` in shape
  (git ls-files, text-suffix filter, self-exclusion), but structured with a pure, directly-testable
  function (`find_violations(paths) -> list[str]` or similar) rather than a script that only runs via
  subprocess — `check_no_personal_paths.py` has no unit test today; `tests/scripts/test_check_identity_threaded.py`
  is this repo's model for a directly-importable, `tmp_path`-driven guard test, so the new script follows
  that shape instead.
- `.pre-commit-config.yaml`: add `check-no-deployment-identifier` hook entry alongside the existing local
  hooks.
- Known, accepted gap (Codex-confirmed): self-exclusion protects only the checker file itself. Any other
  tracked file that spells the real domain contiguously — including future post-mortems or review docs
  discussing this ticket — will be blocked too. That's the ticket's own zero-literal AC working as
  intended; the convention going forward is to describe the domain generically or use the placeholder,
  never spell it out, which is why this very plan document uses the placeholder throughout.
- Proof required by AC #3: a scratch file containing the real literal, run through the hook, captured
  showing the block, then removed (never committed).

### B. `src/personal_agent/config/settings.py`
- `cors_allowed_origins` default → placeholder equivalents (`seshat.example.com`, `agent.example.com`)
- `allowed_ws_origins` default → same placeholders
- `slm_health_url` default → `"https://slm.example.com/health"`
- `artifacts_public_base_url` description example → placeholder host
- CF-Access header field descriptions (~lines 1955/1963) → placeholder host in prose
- **New**: `slm_tunnel_base_url: str | None = Field(default=None, description=...)`
- **New**: `pwa_public_origin: str = Field(default="https://agent.example.com", description=...)`

### C. `src/personal_agent/config/model_loader.py`
- New helper `_apply_slm_tunnel_override(config: ModelConfig) -> ModelConfig` (or mutate in place before
  return) — for each `ModelDefinition`, if `urlparse(endpoint).hostname == "slm.example.com"` and
  `settings.slm_tunnel_base_url` is set, rewrite scheme+host, keep path. Called once at the end of
  `load_model_config`.

### D. `src/personal_agent/llm_client/client.py`, `memory/embeddings.py`, `memory/reranker.py`
- Delete the 3 duplicated `_SLM_TUNNEL_HOSTNAME` module constants.
- Replace call sites (`if _SLM_TUNNEL_HOSTNAME in current_endpoint`) with
  `if settings.slm_tunnel_base_url and settings.slm_tunnel_base_url in current_endpoint`.
- `reranker.py` module docstring prose mention → placeholder.

### E. `src/personal_agent/observability/artifact_envelope/spec.py`
- `EXPECTED_CSP_DIRECTIVES` becomes a function (`_build_expected_csp_directives()`) reading
  `settings.artifacts_public_base_url` (falls back to a placeholder constant if unset — matches existing
  optional-field behavior elsewhere) and `settings.pwa_public_origin`, called at import time to preserve
  the existing module-level `Mapping` shape callers already depend on.
- `load_lib_manifest()` (`:117-158`): after loading `config/artifact_lib_manifest.json`, override the
  parsed `origin` with `settings.artifacts_public_base_url` when set — same pattern as the substitution-map
  override in `artifact_export.py` (§G), closing the gap Codex flagged so `make verify-lib` without an
  explicit `ORIGIN` still probes the real host, not the placeholder.
- `verifier.py` docstring example → placeholder.

### F. `src/personal_agent/tools/artifact_tools.py`
- `_HTML_GENERATION_SYSTEM_PROMPT`'s six library-URL literals become an f-string (or `.format()`) built
  from `settings.artifacts_public_base_url` at call time — check how the prompt constant is consumed
  (module-level string vs. built per-call) and adjust minimally; if referenced as a plain module constant
  today, convert the single usage site to call a small builder function instead of a bare string.

### G. `src/personal_agent/storage/artifact_export.py`
- After loading `artifact_lib_substitution_map.json`, if `settings.artifacts_public_base_url` is set,
  override `raw["origin"]` with it before constructing `sub_map`.
- Docstring/comment mentions → placeholder.

### H. `src/personal_agent/service/artifacts_router.py`, `service/cf_access_jwt.py`
- Docstring-only mentions → placeholder text. No functional change (confirmed no literal runtime string
  beyond prose in these two files, independently re-verified).

### I. `config/` (non-Python)
- `config/models.yaml`, `config/models.cloud.yaml`, `config/models.benchmark-{4b,4b-f16,8b}.yaml`:
  real endpoint → placeholder (real value resolved at runtime by the model_loader override in C). Comments
  referencing the host → placeholder.
- `config/artifact_lib_substitution_map.json`: `"origin"` → placeholder (resolved at runtime by G).
- `config/artifact_lib_manifest.json`: `"origin"` → placeholder (resolved at runtime by E's
  `load_lib_manifest()` override — corrected from the first pass, which conflated this file with the
  substitution map).
- `config/cloud-sim/Caddyfile`: site-block hosts → `{$VAR:placeholder}` per design point 7.

### J. Root files
- `docker-compose.cloud.yml`: per design points 5–7 (Neo4j advertised address, `SESHAT_URL`, PWA build
  arg, Caddy env passthrough).
- `Dockerfile.pwa`: per design point 6.
- `Makefile:151`: usage-message example URL → placeholder (cosmetic, no behavior).
- `CLAUDE.md:191`: prose table entry → placeholder.
- `.env.example`: comments → placeholder text (this file is a template; never holds real values anyway).

### K. `tests/`, `e2e/`
- All hits are self-contained fixture literals (mocked endpoints, CSP string constants asserted against
  local `spec.py` output, PWA env-var test setup) — confirmed none read the real `config/models*.yaml` or
  hit a live network. Mechanical placeholder swap; assertions still compare fixture-to-fixture so nothing
  breaks. `tests/personal_agent/service/test_artifacts_router_export.py` and
  `tests/personal_agent/storage/test_artifact_export.py` construct their own `_ORIGIN` constant
  independent of the real settings value — swap consistently with the same placeholder used elsewhere so
  a reader doesn't wonder if they're different hosts.

### L. `seshat-pwa/`
- `MarkdownContent.tsx`: fallback literal → placeholder (real value now arrives via the wired build arg
  from I/J).
- `__tests__/*.tsx`, `*.test.ts`: fixture literals → placeholder.
- `ArtifactCard.tsx`, `ArtifactViewer.tsx`: docstring/comment-only mentions → placeholder.

### M. `scripts/eval/fre435_memory_recall/separation_benchmark.py`
- Per design point 8: hardcoded endpoint literals → `os.environ.get("AGENT_SLM_TUNNEL_BASE_URL", "https://slm.example.com/v1")`-derived
  values, so the benchmark still runs against the real tunnel when the operator has `.env`'s new key set
  (or sourced into their shell), and fails obviously (fake host, not a live one) otherwise.

### N. `docs/`, `telemetry/`, `scripts/study/eval_artifacts/frozen/*.json`, remaining `scripts/eval/**`
- Bulk mechanical literal → `example.com` (subdomain-preserving) across all ~50 docs files, 8 telemetry
  files, and the remaining eval/research scripts and frozen JSON fixtures. Verified these are prose,
  historical snapshots, or eval-gold-data — not runtime-loaded config. `scripts/build_e2e_artifact_fixtures.py`,
  `scripts/verify_artifact_envelope.py`, `scripts/eval_04b_occupancy_curve.py` are CLI tools with the
  literal only in a help string / module constant used to build fixture data for **local** e2e runs — safe
  placeholder swap (not wired to settings; these already accept `--agent-url`/origin as a CLI arg for the
  real target when actually run against prod).

## Acceptance criteria mapping

1. `git grep -i` for the real domain → zero hits at HEAD — verified by the file-by-file sweep above (A–N
   cover all 104 files).
2. Functional hits parameterized, profiles still resolve real endpoints from env — B–J, M above; verify
   locally: set `AGENT_SLM_TUNNEL_BASE_URL` to the real tunnel URL and confirm `load_model_config()` shows
   the real endpoint after override; same pattern check for CSP (`spec.py`), the lib-manifest origin, and
   the artifact-export origin.
3. Guard proven — scratch-file rejection captured in the PR/handoff.
4. `make test`, `make mypy`, `make ruff-check`/`ruff-format`, PWA `npm run lint` all clean.
5. (Post-deploy, master's gate, not this session) — document the exact `.env` keys to add on the VPS and
   the rebuild/verify steps in the final Linear comment per skill Step 9.

## Test plan (TDD, Step 4)

- New test for `model_loader._apply_slm_tunnel_override` (or equivalent): placeholder host untouched when
  `slm_tunnel_base_url` is `None`; rewritten when set; path preserved.
- New test for the `spec.py` CSP builder: directives reflect `settings.artifacts_public_base_url` /
  `settings.pwa_public_origin` when set, placeholder when not.
- New test for `spec.py`'s `load_lib_manifest()` origin override (manifest `origin` superseded by setting
  when set).
- New test for `artifact_export.py`'s origin override (JSON `origin` superseded by setting when set).
- Update existing tests whose literals reference the old constant name/shape (`_SLM_TUNNEL_HOSTNAME`
  import, if any test imports it directly — grep confirms none do; all reference the string value only).
- New guard-script tests, directly importing the pure violation-finder function per §A (no existing
  `check_no_personal_paths.py` test to mirror; `tests/scripts/test_check_identity_threaded.py` is the shape
  to follow instead).

## Risk / rollback

- Highest-risk edits: CSP (`spec.py`) and CF-Access header injection (client.py/embeddings.py/reranker.py)
  — a mismatch here either breaks the artifact envelope verifier (alarm-visible, per its own docstring) or
  silently stops CF-Access auth from firing (Mac tunnel calls would then 403 upstream, loud failure, not
  silent). Both fail loud, not silent, which bounds the blast radius.
- `docker-compose.cloud.yml` / `Dockerfile.pwa` / Caddyfile changes only take effect on a master-driven
  rebuild — this build session ships the code; verification against the real deploy is explicitly master's
  post-deploy step (AC #5), not mine to execute.
- `.env` changes are additive only (new keys, current real values) — no existing key is touched, per the
  ticket's guard rail.

## Codex second-opinion review (2026-07-17)

Ran `codex:rescue` against this plan before implementation (required — this touches src/, security config,
and multi-file behavior). Verdict: design confirmed as "mostly minimal, slightly under-engineered" — the
two gaps it found (`artifact_lib_manifest.json`, `separation_benchmark.py`) are folded into §E and §M
above; no redesign needed. Full result on file; Codex session `019f6f1e-ef9b-7922-9095-fe97e467e976`.
