# FRE-1751 — Fill the dev-agent toolkit volume from the pinned `/lib/`, with a digest check

Backing ADR: ADR-0159 D3.3 (agent mount), D7.3 (check mount), D7.5 (toolkit digest in the check result).
Design source: ADR-0089 Addendum A (the pinned `/lib/` toolkit). Related: FRE-1684 (launcher), FRE-1686 (T5 check).

## Scope

1. A command, `make dev-agent-toolkit`, builds the Docker volume `AGENT_DEV_AGENT_TOOLKIT_VOLUME` from the assets of `config/artifact_lib_manifest.json`.
2. The command checks each file against the SHA-384 SRI of `config/artifact_lib_substitution_map.json`. One bad file refuses the whole volume.
3. The command writes `toolkit-manifest.json` (with a digest) into the volume.
4. Before the launcher mounts the volume, it copies the files out and recomputes every SRI against the committed config. It also compares the digest in `toolkit-manifest.json`.

## Acceptance criteria (from the ticket)

| # | Criterion | Proof |
|---|-----------|-------|
| AC-1 | After the command, the volume holds every manifest asset, and each file's SHA-384 equals its pinned SRI. A missing or mismatched file fails. | Live test: fill a volume, read every file back through a container, recompute SRI in Python. Unit tests: missing file and mismatched file each raise. |
| AC-2 | A seeded corrupt file makes the command fail and leaves no usable volume. | Live test: a corrupt asset raises, and `docker volume inspect` of the target fails. Unit test: zero Docker calls on a mismatch. |
| AC-3 | An agent container that the launcher starts can read `/srv/artifact-lib/<asset>` and cannot write it. | Live test: `DockerLauncher.start` with the real `agent` profile, then `docker exec` read succeeds and write fails with a read-only error. |

## Decisions

- **Byte source: R2.** The bucket holds all ten assets at the key `lib/<manifest path>`. Four assets (three.js and the three fonts) have no public CDN twin, and the hosted origin is behind Cloudflare Access. The VPS holds the R2 credentials already (`AGENT_R2_*`). The fetch function is a parameter, so tests pass a fake.
- **Verify before any Docker call.** The command fetches and checks all bytes in memory first. A mismatch raises before it creates anything.
- **Replace, not merge.** If the volume exists, the command removes it first. `docker volume rm` refuses a volume that a container uses. The command then creates the volume and fills it. After the copy, it reads the volume back and runs the full check. A failure after the create removes the helper container first, then the volume.
- **No label.** A label is set at create time, before the bytes exist, and it never binds the bytes. Codex review (2026-11-07) showed that a label lets a half-filled volume pass. The launcher trusts bytes only.
- **Write and read path: `docker create` + `docker cp`.** No container starts. The helper container uses the agent image from the `agent` profile and `--network none`. Write: copy a temporary directory (files `0444`, directories `0755`) into the volume. Read: mount the volume read-only in a new helper container and copy `/srv/artifact-lib/.` out to a temporary directory. Live probe on 2026-11-07: this works, modes survive, and uid 10001 can read and cannot write. The files keep the owner id of the host user, and the read-only mount is the write guard.
- **Digest.** `sha256:` plus the SHA-256 of the lines `"<path> <sri>\n"`, sorted by path. Each SRI binds the file bytes, so the digest binds the content. `toolkit-manifest.json` holds the digest and the path/SRI list.
- **One check function, two callers.** `check_toolkit_contents(assets, contents)` requires: every asset present with the pinned SRI, no other file except `toolkit-manifest.json`, and a manifest digest equal to the digest of the config. The fill command calls it on the read-back. `DockerLauncher.start` calls it before each job (about 2 MB, ten files).
- **Launcher input.** `DockerLauncher` takes a required `toolkit_assets` sequence and the helper image comes from its profile. The gateway passes `load_toolkit_assets()`.
- **D7.5 stays with FRE-1686.** This ticket supplies `toolkit_digest(assets)` and the in-volume file. The T5 check records the digest in its own result.
- **All ten assets**, including the `eval_gated` paged.js entry. The ticket says "every asset of the manifest".
- **Path safety.** A manifest path that is absolute or holds `..` raises, because the command writes it under a temporary directory.

## Steps

### 1. Tests first — `tests/personal_agent/dev_agent/test_toolkit.py`

Unit tests (fake fetcher, fake Docker runner):
- `load_toolkit_assets()` on the committed config returns ten assets, all `sha384-`.
- A manifest asset with no map entry raises `DevAgentToolkitError`. A path with `..` raises.
- `verify_toolkit_bytes` reports a missing path and a mismatched path in one error.
- `toolkit_digest` does not depend on order, and it changes when one SRI changes.
- `fill_toolkit_volume` with one corrupt asset raises and the runner records zero calls.
- `check_toolkit_contents` rejects: an extra file, a changed byte, a missing manifest file, a manifest with another digest.
- `fill_toolkit_volume` on success: the fake volume holds every asset and the manifest file.
- `fill_toolkit_volume` with a failing `cp` into the volume, and with a read-back that differs: the helper container is removed before the volume, and the volume is gone.

Launcher tests in `test_launcher.py`: a volume with a changed byte, an extra file, or a half-copied tree raises before the workspace volume is created. A good volume proceeds. `FakeDocker` serves the volume contents on `docker cp`.

Run: `uv run pytest tests/personal_agent/dev_agent/test_toolkit.py tests/personal_agent/dev_agent/test_launcher.py -q`. Expect failures (import error) before step 2.

### 2. Module — `src/personal_agent/dev_agent/toolkit.py`

- `TOOLKIT_MOUNT`, `DIGEST_LABEL`, `MANIFEST_FILE` constants.
- `ToolkitAsset(path, sri)` frozen dataclass.
- `load_toolkit_assets(manifest_path=None, map_path=None)` reuses `load_lib_manifest` and `load_substitution_map`.
- `toolkit_digest(assets)`, `verify_toolkit_bytes(assets, contents)`.
- `read_toolkit_volume(volume, image, runner)` returns `{relative path: bytes}`.
- `check_toolkit_contents(assets, contents)`.
- `fill_toolkit_volume(volume, assets, fetch, *, image, runner)`. The runner has no default, because `launcher.py` imports this module.
- `DevAgentToolkitError` in `personal_agent/exceptions.py`, beside `DevAgentLaunchError`.

### 3. Launcher and gateway

- `launcher.py`: new required argument `toolkit_assets`. `start` keeps `volume inspect` (clear error for a missing volume), then runs `read_toolkit_volume` and `check_toolkit_contents`.
- `DevAgentToolkitError` subclasses `DevAgentLaunchError`, so the supervisor handles it as a launch failure.
- `gateway.py`: pass `load_toolkit_assets()`.
- Update the two live-test construction sites (`test_supervisor_live.py`, `live_child.py`): fill the throwaway volume with synthetic assets through `fill_toolkit_volume`.

### 4. Command

- `scripts/fill_dev_agent_toolkit.py`: reads R2 through `get_artifact_store()`, key `lib/<path>`. It prints the digest. It exits non-zero on error and when R2 is not configured.
- `Makefile`: target `dev-agent-toolkit` with a `##` help line.

### 5. Live tests — `tests/personal_agent/dev_agent/test_toolkit_live.py` (marked `integration`)

Real Docker, synthetic assets, throwaway volume, network and scope. Covers AC-1, AC-2, AC-3, and the launcher refusal of a half-copied or tampered volume (write an extra file into the live volume, then expect `start` to fail).

### 6. Docs

- `docker/dev-agent/README.md`: replace "Filling that volume is FRE-1751" with the command and the deploy step.

### 7. Gates

`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`. Then commit, run `feature-dev:code-reviewer` and `security-review` on `git diff origin/main...HEAD`.

## Post-deploy runbook (master)

1. `make dev-agent-build` (the agent image must exist).
2. `make dev-agent-toolkit`. Expect the digest line and exit 0.
3. `docker run --rm -v seshat_dev_agent_toolkit:/t:ro --entrypoint cat seshat-dev-agent:0.1 /t/toolkit-manifest.json` prints the digest that step 2 printed.
4. Rebuild `seshat-gateway` only when the supervisor flag turns on. The supervisor stays off until then.

## Risk

Diff class: not escalated. No production write path, no schema, no cost or governance code. The command touches only one named Docker volume.
