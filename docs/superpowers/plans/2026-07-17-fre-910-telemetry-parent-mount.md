# FRE-910 — Telemetry durability by default: mount /app/telemetry once

**Backing:** surfaced by FRE-908 (found `within_session_compression` telemetry is unmounted and lost on
rebuild). Supersedes the narrow per-directory fix FRE-908's own plan proposed
(`docs/superpowers/plans/2026-07-17-fre-908-compression-gate-proof.md` Step 2) — that plan sketched adding
one more named volume for `within_session_compression` alone; this ticket explicitly rejects that
whack-a-mole pattern in favor of a single parent mount covering all nine current writers and any future one.

## Scope guard (from ticket)

Compose + a durability test only. Do NOT consolidate/remove the six existing nested telemetry volumes
(captains_log, feedback_history, graph_quality, context_quality, error_patterns, freshness_review —
separate optional tidy-up). Do NOT change any writer's path in `src/`. AC-4 (post-deploy gateway recreate
verification) is master + owner-timed — out of scope for this build session; this plan delivers AC-1/2/3
only and hands the runbook for AC-4 to master in the PR/ticket handoff.

## Investigation already done (this session, read-only)

- Confirmed the ticket's inventory: `grep -roE 'Path\("telemetry/[^"]+"\)' src/personal_agent` finds 10
  literal call sites resolving to 9 *unique* paths — `security.py` uses the same literal
  `Path("telemetry/security/domain_blocklist.json")` twice (a default parameter value and an explicit
  call-site override at lines 86 and 273). The other 8 unique paths are one each in `brainstem/scheduler.py`,
  `brainstem/jobs/freshness_review.py`, `telemetry/within_session_compression.py`, `config/settings.py`
  (the `log_dir` field default), `telemetry/error_monitor.py`, `telemetry/tool_result_digest.py`,
  `telemetry/context_quality.py`, `insights/skill_routing_threshold_monitor.py`. All use the identical
  literal-string idiom `Path("telemetry/<subpath>")`, CWD-relative. (Codex plan-review confirmed this
  count and flagged the earlier "9 matches" wording as ambiguous between call sites and unique paths —
  corrected here; the test itself was already unaffected since it de-duplicates into a `set[str]`.)
- **Accepted regex limitation** (codex plan-review flagged, documented rather than generalized per
  Simplicity First): the guard's `_WRITER_RE` only matches the exact double-quoted literal idiom
  `Path("telemetry/...")` actually used by all 9 current writers. It would miss a future writer using
  single quotes, `Path("telemetry") / "x"`, or a computed/f-string path. Widening to a full AST scan is
  not justified for a single-idiom codebase convention today — if a future writer adopts a different
  idiom, `test_nine_known_writers_found`'s count-regression pin will silently stop catching it (a known,
  accepted gap, not a false sense of coverage).
- `captains_log` and `feedback_history` (already durable) use a *different* idiom —
  `Path(__file__).resolve().parent.parent.parent.parent / "telemetry" / "<name>"` (`captains_log/manager.py`,
  `captains_log/suppression.py`, `captains_log/backfill.py`) — absolute, not CWD-relative. Out of this
  ticket's 9-writer inventory by construction; already mounted; untouched by this plan.
- `Dockerfile.gateway:56` sets `WORKDIR /app`, so every CWD-relative `Path("telemetry/...")` resolves to
  `/app/telemetry/...` inside the container — confirms the ticket's mount-path math.
- Current `docker-compose.cloud.yml` `seshat-gateway.volumes` (lines ~389-410) mounts six named volumes at
  `/app/telemetry/{captains_log,feedback_history,graph_quality,context_quality,error_patterns,freshness_review}`
  plus `/app/agent_workspace`; no mount at `/app/telemetry` itself. Top-level `volumes:` block (~526-578)
  declares each with `driver: local`.
- Local `docker-compose.yml` has no gateway telemetry mounts — `make dev` runs uvicorn directly on the host
  (root CLAUDE.md), not containerized. Confirmed out of scope; ticket AC-1 names `docker-compose.cloud.yml`
  specifically.
- Precedent for compose-YAML-driven pytest checks: `tests/personal_agent/config/test_substrate_manifest_guard.py`
  and FRE-908's `TestTelemetryDurability` (parses with `yaml.safe_load`, not text-slicing — codex flagged
  text-slicing as brittle on that ticket).

## What this plan builds

One compose edit, one new test file. No `src/` behavior change.

### Step 1 — `docker-compose.cloud.yml`: parent volume (AC-1, AC-2)

In the `seshat-gateway.volumes` list, add the parent mount immediately before the existing nested mounts
(Docker resolves by path depth regardless of list order; ordering here is for readability — parent first,
its more-specific children after, matching how the block already reads):

```yaml
      # FRE-910: durable-by-default parent mount. Every Path("telemetry/...") writer
      # in src (nine today, any future one) lands under a mounted path automatically —
      # no more one-mount-per-writer whack-a-mole. The six nested volumes below stay
      # mounted at their specific subpaths: Docker mounts by path depth, so a
      # more-specific mount keeps owning its subtree and existing data — nothing moves.
      - seshat_telemetry_cloud:/app/telemetry
      - seshat_captains_log_cloud:/app/telemetry/captains_log
      ...(existing six lines, unchanged)...
```

In the top-level `volumes:` block, add the matching declaration (placed with the other telemetry volumes,
before `seshat_captains_log_cloud`):

```yaml
  # FRE-910: parent mount for /app/telemetry — durable-by-default for every writer,
  # not just the ones a human remembered to mount individually. Nested volumes below
  # (captains_log, feedback_history, graph_quality, context_quality, error_patterns,
  # freshness_review) still own their own subtrees at their specific paths.
  seshat_telemetry_cloud:
    driver: local
```

No other lines change. This is the whole AC-1/AC-2 diff — additive only.

### Step 2 — new test file `tests/personal_agent/telemetry/test_mount_coverage.py` (AC-3)

Follows the `yaml.safe_load`-based compose-parsing convention from
`test_substrate_manifest_guard.py`/FRE-908's `TestTelemetryDurability`, not text-slicing.

```python
"""FRE-910 AC-3 — every Path("telemetry/...") writer in src resolves under a mounted
container path, so a new writer cannot silently be ephemeral (the guard-as-test the
parent mount makes near-tautological)."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import yaml

from personal_agent.config.config_guard import repo_root

_WRITER_RE = re.compile(r'Path\("(telemetry/[^"]+)"\)')


def _find_telemetry_writers(root: Path) -> set[str]:
    writers: set[str] = set()
    for path in (root / "src" / "personal_agent").rglob("*.py"):
        writers.update(_WRITER_RE.findall(path.read_text(encoding="utf-8")))
    return writers


def _load_compose(root: Path) -> dict:
    return yaml.safe_load((root / "docker-compose.cloud.yml").read_text(encoding="utf-8"))


def _gateway_mounts(compose_doc: dict) -> dict[PurePosixPath, str]:
    """Map each mounted container destination to its named-volume source."""
    volumes = compose_doc["services"]["seshat-gateway"]["volumes"]
    mounts: dict[PurePosixPath, str] = {}
    for v in volumes:
        if isinstance(v, str) and ":" in v:
            source, dest = v.split(":", 1)
            mounts[PurePosixPath(dest.split(":")[0])] = source
    return mounts


def _is_covered(writer_path: PurePosixPath, mounts: dict[PurePosixPath, str]) -> bool:
    return any(writer_path == m or m in writer_path.parents for m in mounts)


class TestTelemetryMountCoverage:
    def test_nine_known_writers_found(self) -> None:
        # Pins the ticket's measured inventory — a drop below 9 means the regex
        # stopped matching a writer (rename/idiom change), not that one was removed.
        assert len(_find_telemetry_writers(repo_root())) == 9

    def test_parent_mount_is_the_named_seshat_telemetry_volume(self) -> None:
        # Not just "something is mounted at /app/telemetry" — the specific named
        # volume this ticket adds, declared with a durable local driver.
        compose = _load_compose(repo_root())
        mounts = _gateway_mounts(compose)
        assert mounts.get(PurePosixPath("/app/telemetry")) == "seshat_telemetry_cloud"
        top_level = compose["volumes"]["seshat_telemetry_cloud"]
        assert top_level["driver"] == "local"

    def test_every_writer_resolves_under_a_mounted_path(self) -> None:
        mounts = _gateway_mounts(_load_compose(repo_root()))
        writers = _find_telemetry_writers(repo_root())
        assert writers, "writer discovery found nothing — regex or scan path broke"
        uncovered = [
            w for w in writers if not _is_covered(PurePosixPath("/app") / w, mounts)
        ]
        assert not uncovered, f"telemetry writers with no durable mount: {uncovered}"
```

Notes on design choices:
- `test_nine_known_writers_found` is a regression pin on the *count* (not the exact names) — catches the
  regex silently stopping matching a renamed/refactored writer, without hard-coding the 9 literal strings
  (which would make the test itself part of the whack-a-mole the ticket is ending).
- `test_every_writer_resolves_under_a_mounted_path` is the AC-3 guard proper. Before Step 1's compose edit
  it fails for the 5 ticket-cited ephemeral writers (`within_session_compression`, `tool_result_digest`,
  `skill_routing_monitor`, `security/domain_blocklist`, `logs`) — confirms the test is real, not vacuous —
  then passes once the parent mount lands.
- `test_parent_mount_is_the_named_seshat_telemetry_volume` machine-verifies AC-1 precisely — the specific
  named volume + destination + `driver: local`, not just any mount incidentally covering the path
  (codex plan-review finding: the original draft only checked the destination, which would have passed
  even for an unrelated or misconfigured mount at that path).

### Step 3 — quality gates

```
make test-file FILE=tests/personal_agent/telemetry/test_mount_coverage.py   # confirm it fails pre-Step-1, passes post
make test                  # full suite — no src/ touched, so this is a regression check
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

### Step 4 — self-review

`code-review` skill at `low` effort (compose is additive-only config; the new test file reads repo files,
no behavior change). No `security-review` needed — no inputs/subprocess/auth/network touched (the compose
YAML read is a repo-file read, not a subprocess; no secrets involved, no new env vars).

## Risk-tier self-classification

**Standard** — no `src/` logic change and the compose edit is additive-only, but it is a production deploy
topology change with real data-durability and migration-safety stakes (must not disturb the four
currently-mounted volumes' existing data), so codex plan-review requested per skill default ("when in
doubt, treat as Standard").

## Post-deploy runbook (for master, AC-4 — NOT run in this build session)

1. `ENV=cloud make rebuild SERVICE=seshat-gateway` is a **rebuild**, not just recreate — but per the
   ticket's deploy constraint, a volume change needs container **recreate** at minimum (`make up`, not
   `make restart`). Confirm the deploy path used actually recreates the container (check `docker compose up
   -d` semantics vs `restart`).
2. After recreate, trigger a write to a previously-ephemeral stream, e.g. `within_session_compression`
   (any turn that hits the hard-compression gate, or a direct probe).
3. Recreate the gateway container again (`make up` a second time — no code change needed, just re-apply).
4. Confirm the record from step 2 survived the second recreate (read it back from the container or via
   `docker volume` inspection of `seshat_telemetry_cloud`).
5. This is owner-timed (drops any in-flight turn) — do not run without explicit owner go-ahead, per the
   ticket's deploy constraint and the standing "ask first" class for gateway rebuilds (lifecycle-rules §
   Deploy).

## Self-review (workflow code-review, `high`/`--effort low`)

Three findings reported, all confirmed, all fixed on-branch:

- **Finding [0] (correctness, CONFIRMED)**: FRE-908's own
  `TestTelemetryDurability.test_gateway_volumes_mount_context_quality_but_not_within_session_compression`
  asserted `within_session_compression` is **not** durably mounted — true when FRE-908 wrote it, false
  after this ticket's parent mount lands, but the test kept passing (exact-string list membership, not
  path-containment) and its docstring now stated a falsehood about production behavior. Fixed: renamed to
  `test_gateway_volumes_mount_within_session_compression_durably`, flipped the assertion to confirm
  coverage, rewrote the docstring to narrate the FRE-908→FRE-910 handoff.
- **Finding [1] (cleanup, CONFIRMED)**: the fix for Finding 0 would have reimplemented compose-mount
  parsing a third time (FRE-908's test, this ticket's `test_mount_coverage.py`, and the fix itself).
  Extracted the shared logic into `tests/_helpers/telemetry_mounts.py` (the repo's existing
  cross-suite-helper location — see `tests/_helpers/trace.py` for precedent) and had both test files import
  from it, so there is exactly one compose-parsing implementation instead of two independently-drifting
  ones.
- **Finding [2] (cleanup, CONFIRMED)**: each test method in the original `test_mount_coverage.py` re-ran a
  full `rglob` over `src/personal_agent` and re-parsed the compose YAML from scratch. Fixed by
  `@lru_cache`-ing `find_telemetry_writers` and `load_compose` in the new shared helper — same coverage,
  a fraction of the filesystem I/O, and the caching is free now that the logic lives in one place both
  test files can share.

Also fixed while addressing Finding 0: `docs/research/2026-07-17-fre-908-compression-gate-proof.md`'s AC-4
table row and Finding 2 narrative stated the durability gap as current fact; added an inline "Fixed by
FRE-910" pointer and an addendum section rather than rewriting the historical investigation record — the
doc's job is to preserve what was found and when, not to be kept live-current.

Additionally fixed two pre-existing strict-mypy findings (`dict` without type parameters) in the new
`tests/_helpers/telemetry_mounts.py` and `test_mount_coverage.py` — not part of the `make mypy` gate
(`src/` only) but caught running `uv run mypy` directly on the new files, since the project's
`pyproject.toml` sets `strict = true` repo-wide. A third pre-existing strict-mypy finding at
`test_compression_gate_proof.py:170` (`msgs: list` missing type parameter) predates this diff — left
untouched per surgical-changes discipline (not something my change introduced or touched).

No `security-review` run — no inputs/subprocess/auth/secrets/network touched (compose is a static config
file, the new test/helper code reads repo files only, no live turns or external calls).
