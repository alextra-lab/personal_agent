# FRE-1682 — ADR-0159 T1: the dev-agent network, the egress proxy and the images

**Ticket:** FRE-1682 (umbrella FRE-1679). **ADR:** `docs/architecture_decisions/ADR-0159-development-agent-opencode-sandbox.md`
(D3.3, D4.1, D4.2, D4.5, D2.1 image part, D7.1, D7.3 image part, D8.8 image part).
**Tier:** Tier-2. **Risk class:** Standard (security boundary, no `src/` runtime path changes). Codex plan review required.
**Scope:** one phase, one PR. The broker (T2), the supervisor and launcher code (T3) and the native tool are out of scope.

## Facts that shape the plan (verified 2026-11-06)

- OpenCode 2.x is the npm package `@opencode/cli` (not `opencode-ai`, which stops at 1.19.5). Latest 2.x: `2.2.5`. The binary name is `opencode`.
- The e2e harness pins Playwright 1.60.0. The base image `mcr.microsoft.com/playwright:v1.60.0-noble` exists.
- `e2e/artifact-lib/csp-server.ts` is a library. It has no entry point. It reads `build-manifest.json` from the served root. It has a value import of a type (`AddressInfo`), so Node cannot run it with type stripping.
- `expected_csp_directives()` depends on settings. `build_csp_header_template()` in `scripts/build_e2e_artifact_fixtures.py` renders it with an `{ORIGIN}` placeholder.
- Inside `cloud-sim`: Postgres 5432, Neo4j 7687, Elasticsearch 9200, Redis 6379, Caddy 8600 and 8601, gateway 9001.
- No compose service sets a static address (guard test exists). The new service must not set one.
- The base image has Node 24.15 (runs `.ts` with type stripping), `python3` and `git`. Its environment holds only `PATH`, `LANG`, `LC_ALL` and `PLAYWRIGHT_BROWSERS_PATH`. The npm package `playwright` is not installed, so the image installs `playwright@1.60.0`.

## Decisions

1. **Images** (tags follow `seshat-sandbox-python:0.1`): `seshat-dev-agent:0.1`, `seshat-dev-check:0.1`, `seshat-dev-handoff:0.1`, `seshat-dev-proxy:0.1`.
2. **One Dockerfile for check and agent.** `Dockerfile.browser` has two targets. `agent` extends `check` with OpenCode and git. Layers are shared, and disk is at 71%.
3. **The agent image has git.** The agent commits in its workspace (D8.4). The ADR image list omits git. The base image already has it, so no step is needed.
4. **Proxy = Squid.** CONNECT only, port 443 only, host `registry.npmjs.org` only, IP literals denied first, no cache, no plain HTTP. A custom proxy is more code on a security boundary.
5. **CSP server entry point.** A new `csp-serve.ts` CLI prints the origin. `csp-server.ts` gets two surgical changes: `import type { AddressInfo }` and an optional `manifestPath` parameter. The existing e2e spec keeps working.
6. **CSP manifest.** A committed `docker/dev-agent/csp-manifest.json` holds `csp_header_template`. A test pins settings to placeholders and fails on drift from `build_csp_header_template()`.
7. **Launcher spec = data.** `config/dev_agent/container_profiles.yaml` holds three profiles (agent, handoff, check). A frozen Pydantic model loads it. `build_create_args()` renders `docker create` arguments from a profile. `verification.py` has pure functions that compare `docker inspect` output, the CSP header, and the probe report with the spec.
8. **Pin check.** The OpenCode version lives in the spec YAML and in the Dockerfile `ARG`. A test fails if they differ. The live check compares `opencode --version` with the pin.
9. **Probe** = `docker/dev-agent/probe.py`, Python stdlib only, baked into the agent and check images (the base image has `python3`). The probe prints a JSON report and exits non-zero if any forbidden target answers or the positive control fails.
10. **Instrument check.** A negative result can come from a broken probe. The verify script also runs the same probe on a plain bridge network with a listener. The probe must report "reachable" there.
11. **No `container_name` on the new compose service.** A test project can then run beside production without a name clash.
12. **Verification does not connect to production by default.** The script starts canary listeners on a separate, randomly named network. The canary ports are the production ports (5432, 7687, 9200, 6379, 8600, 8601, 9001). The probe tries the canary IP addresses under the production labels. The same canaries are the instrument control: a probe on the canary network must see them as reachable. The flag `--include-production-ips` adds the real container addresses of `seshat_cloud-sim`. The flag is off by default. It only opens and closes TCP connections. It uses a separate compose project name and `--no-deps`, so no production service is recreated.
13. **The probe report is a strict schema** (verification.py): every required label exactly once, with the right polarity set by the checker and not by the probe, an attempted connection, a result of `connected` or `failed`, and a TCP or proxy stage for IP-literal targets. A name that only failed DNS does not count as proof for a service.
14. **Container lifecycle in the check:** `docker create` (no `--rm`) with the probe as the command, then `docker start --attach`, then `docker inspect`, then `docker rm` in a `finally` block.
15. **Squid:** `dstdomain -n` (no reverse lookup) plus an explicit numeric-authority deny. The live test sends CONNECT to the IPv4 literal of the registry, an IPv6 literal, github.com, port 80 and the allowed name.

## Review disposition (codex plan review, 2026-11-06)

| Finding | Disposition |
|---------|-------------|
| Verifier probes production containers | Accepted. Decision 12 rewritten: canaries by default. |
| Base image leaves environment names that cannot be removed | Not a defect. `LC_ALL` and `PLAYWRIGHT_BROWSERS_PATH` are declared in `env_fixed`. The check compares the exact name set. |
| Report evaluator accepts a probe that omits targets | Accepted. Decision 13. |
| Seeded inspect mutations do not cover every D3.3 field | Accepted. One mutation per property, including each tmpfs and mount. |
| `docker create` cannot `exec`; `--rm` removes the container before inspect | Accepted. Decision 14. |
| Squid `dstdomain` may reverse-resolve an IP | Accepted. Decision 15. |

## Files

| Path | Action |
|------|--------|
| `docker/dev-agent/Dockerfile.browser` | new: targets `check`, `agent` |
| `docker/dev-agent/Dockerfile.handoff` | new |
| `docker/dev-agent/Dockerfile.proxy`, `docker/dev-agent/squid.conf` | new |
| `docker/dev-agent/handoff-bundle.sh` | new: hardened `git bundle create` wrapper |
| `docker/dev-agent/probe.py` | new |
| `docker/dev-agent/csp-serve.ts`, `docker/dev-agent/csp-manifest.json` | new |
| `docker/dev-agent/README.md` | new |
| `e2e/artifact-lib/csp-server.ts` | edit: two lines |
| `docker-compose.cloud.yml` | edit: networks `dev-agent` (internal), `dev-agent-egress`; service `dev-agent-proxy` |
| `config/dev_agent/container_profiles.yaml` | new |
| `src/personal_agent/dev_agent/{__init__,container_spec,verification}.py` | new |
| `scripts/verify_dev_agent_images.py` | new: live check, prints one JSON report |
| `Makefile` | edit: `dev-agent-build`, `dev-agent-verify` |
| `tests/personal_agent/dev_agent/…`, `tests/scripts/test_dev_agent_compose.py` | new |

## Steps (TDD: the test first, see it fail, then implement)

1. **Spec model.** Tests: `tests/personal_agent/dev_agent/test_container_spec.py` — the YAML loads; the agent profile has every D3.3 value; the check profile has every D7.3 value; the handoff profile has no network; the model is frozen; unknown keys are rejected; `build_create_args()` output has the flags and no `-v` of a host path and no socket; launcher env uses `-e NAME` (no value on the command line).
   Run: `uv run pytest tests/personal_agent/dev_agent/test_container_spec.py -q`.
2. **Verification functions.** Tests: `test_verification.py` — canned inspect JSON passes; each seeded change (cap added, memory 4 GB, writable root, socket mount, host bind, extra env name, extra network, privileged) yields a violation; a CSP header with a missing, extra or changed directive yields a violation; a probe report with one answering forbidden target yields a violation; a report without the positive control yields a violation.
3. **CSP.** Tests: `test_csp_manifest.py` — the committed manifest equals `build_csp_header_template()` under pinned settings; the header with `{ORIGIN}` rebound to a localhost origin equals `expected_csp_directives()` rebound the same way (exact directive sets); a mutated manifest fails the comparison. Then add `csp-serve.ts`, the two edits in `csp-server.ts`.
4. **Probe.** Tests: `test_network_probe.py` loads the file by path — a local TCP listener is reported `connected`; a closed port is `refused`; a CONNECT refused by a fake proxy (403) is `denied`; the exit code is non-zero when a forbidden target answers; the env report has names only.
5. **Images and proxy.** Write the Dockerfiles, `squid.conf`, `handoff-bundle.sh`. Add compose networks and service. Test: `tests/scripts/test_dev_agent_compose.py` — `dev-agent` has `internal: true`; only `dev-agent-proxy` is on it among compose services; the proxy is also on `dev-agent-egress` and nothing else; `cap_drop: ALL`, `read_only`, `no-new-privileges`; no static address; no published port; the squid config denies IP literals before it allows the host.
6. **Build.** `make dev-agent-build`.
7. **Live verification.** `uv run python -m scripts.verify_dev_agent_images --compose-project fre1682t1 --up --down`. The script:
   - brings up only `dev-agent-proxy` with `--no-deps`;
   - creates the agent container with `docker create` using `build_create_args()`, runs the probe with extra targets (the real IPs of production containers on `seshat_cloud-sim`, the IP of `registry.npmjs.org` as a literal through the proxy, the network gateway address), runs `docker inspect` and compares with the spec, removes the container;
   - runs `opencode --version` and compares with the pin;
   - runs the CSP server in a hardened check container, fetches a page, compares the header with `expected_csp_directives()` rebound to localhost, and runs a Chromium smoke test (load the page, zero console errors);
   - runs the handoff wrapper on a hostile repo (repo-local `core.fsmonitor`) and checks that no marker file appears and the bundle is valid;
   - runs the control probe on a plain bridge network with a listener and requires "reachable".
8. **Gates.** `make test` (unit), `make mypy`, `make ruff-check`, `make ruff-format`, `uv run python scripts/check_config.py`, `pre-commit run --all-files`. Then commit, then `feature-dev:code-reviewer` and `security-review` on `git diff origin/main...HEAD`.
9. **PR and handoff comment.** Diff class: escalated if the review finds a production write path. This diff adds compose infrastructure and no production write path, so the expected class is self-serve. The handoff comment follows the build skill.

## Acceptance criteria → proof

| Criterion (ticket) | Proof |
|--------------------|-------|
| Probe: every forbidden target fails; npm through the proxy succeeds | `verify_dev_agent_images` JSON `network` section; unit tests of the report evaluation; control-run report showing the probe can see "reachable" |
| `docker inspect` shows every D3.3 flag; no socket, no host mount | JSON `inspect` section; `test_verification.py` seeded negatives |
| `opencode --version` equals the pin | JSON `opencode` section; pin-equality unit test |
| CSP server sends `expected_csp_directives()`, origin rebound | `test_csp_manifest.py` plus JSON `csp` section from the live container |

## Deploy note for master

The compose change adds two networks and one container. It restarts no existing service. The four images are built by `make dev-agent-build`. Deploy class: not in the reversible list, so use the stricter class.
