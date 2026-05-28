# FRE-410 — `read` primitive: head cap + ranged (offset/limit) reads

> Canonical filename for the implementer: `docs/superpowers/plans/2026-05-28-fre-410-read-head-cap.md`
> (Plan mode constrained me to the harness-named file; `git mv` on execution.)
>
> **Note on the request:** the session opened with "Implement fre-110", but FRE-110 is
> `Done`/archived and its tool (`self_telemetry.py`) was deleted by FRE-265 (ADR-0063 PIVOT-6).
> The follow-up messages ("410", "ship-feature") redirect to **FRE-410**, which is `Approved`,
> High priority, `Tier-2:Sonnet`. This plan implements FRE-410.

## Context

The `read` primitive (`src/personal_agent/tools/primitives/read.py`) defaults `max_bytes` to
**1 MiB**, so any source file under ~1 MB is returned in full. Turn-forensics on traces
`cd5571fd` / `3d59462f` (2026-05-28) showed a single batch of `read` calls — dominated by
`read executor.py` (**~134 KB ≈ ~33K tokens**) — pushed the context window from ~12K to ~57K
input tokens. Because the reasoning model re-prefills the whole window every subsequent step,
that one slurp made every later primary call slow (and on a degraded SLM contributed to a 524s
turn). This is the cheap, high-leverage half of FRE-401.

**Fix:** mirror Claude Code's `Read` — default to a truncated head (first ~200 lines / ~8 KB)
with a `truncated` flag and a marker that nudges grep-first / paging, and add line-based
`offset`/`limit` so the model pages large files instead of slurping. Keep `tail_lines` (logs)
and explicit `max_bytes` (opt-in big read) working with no regression.

**Decisions confirmed with user (2026-05-28):**
1. Default head cap = **200 lines / 8 KB**, whichever hits first.
2. **Drop the `too_large` error entirely** — every read returns a truncated head with
   `truncated=true` + marker (Claude Code style). Two existing tests that assert `too_large`
   get rewritten.

## Scope / files

| File | Change |
|------|--------|
| `src/personal_agent/tools/primitives/read.py` | New line-window head mode + `offset`/`limit` params; drop `too_large`; tail mode keeps 1 MiB default cap; new marker; updated `ToolDefinition` description |
| `tests/test_tools/test_primitives_read.py` | New head/offset/limit/marker tests; rewrite the two `too_large`-asserting tests |

No governance change: `read` is already in `config/governance/tools.yaml:87`. No new tool —
only new parameters on the existing `read` tool. `read_executor` is invoked only via the tool
registry (`tools/__init__.py:146`) plus tests — **no internal Python caller depends on
`too_large`**, so dropping it is safe.

## Implementation

### 1. Constants + helpers (`read.py`)

Add near the top:

```python
DEFAULT_LINE_LIMIT = 200          # default head: first N lines
DEFAULT_HEAD_BYTES = 8_192        # default head byte cap (~2K tokens)
LEGACY_TAIL_CAP = 1_048_576       # tail mode keeps the old 1 MiB cap (no log regression)
```

Add a line-window reader (streams the file: O(window) memory, exact total line count).

**Byte-cap semantics (resolves Codex BLOCKER, 2026-05-28):** the byte cap is applied on
**whole-line boundaries** — a line that would overflow the cap is *excluded* (not clipped), and
`lines_returned` counts only fully-returned lines, so the continuation `offset` re-reads the
excluded line and **no content is lost across pages**. The single degenerate case — one line
on its own larger than `byte_cap` — is clipped to make forward progress, and the continuation
`offset` advances *past* it (accepting that one line's tail is unreachable except via an explicit
larger `max_bytes`); this is documented and prevents an infinite paging loop.

```python
def _read_line_window(
    fh: IO[bytes], offset: int, limit: int, byte_cap: int
) -> tuple[str, bool, int, int]:
    """Return (content, truncated, lines_returned, total_lines) for a 1-based line window.

    Streams the file counting every line so total_lines is exact, while retaining only the
    lines inside [offset, offset + limit). The byte cap is enforced on whole-line boundaries:
    a line that would push the output past byte_cap is excluded so the continuation offset can
    re-read it intact. The sole exception is a single line larger than byte_cap on its own,
    which is clipped to guarantee forward progress. truncated is True if more lines follow the
    returned window or a byte clip occurred. lines_returned counts only fully-returned lines
    (or the one clipped line in the degenerate case).
    """
    start = max(offset, 1)
    end = start + limit  # exclusive, 1-based
    window: list[bytes] = []
    total = 0
    for i, raw_line in enumerate(fh, start=1):
        total = i
        if start <= i < end:
            window.append(raw_line)

    out: list[bytes] = []
    used = 0
    byte_clipped = False
    for raw_line in window:
        if used + len(raw_line) <= byte_cap:
            out.append(raw_line)
            used += len(raw_line)
        else:
            if not out:  # single line larger than the cap: clip to make progress
                out.append(raw_line[:byte_cap])
            byte_clipped = True
            break

    content_bytes = b"".join(out)
    lines_returned = len(out)
    more_lines = total > (start - 1) + lines_returned
    truncated = byte_clipped or more_lines
    return content_bytes.decode("utf-8", errors="replace"), truncated, lines_returned, total


def _build_read_marker(start: int, lines_returned: int, total: int) -> str:
    """Marker telling the model how to get more (grep-first nudge).

    Continuation offset is start + lines_returned — the first line NOT fully returned — so
    paging re-reads any whole line the byte cap excluded.
    """
    next_offset = start + (lines_returned if lines_returned else 1)
    last = next_offset - 1
    return (
        f"Showing lines {start}-{last} of {total}. "
        f"Pass offset={next_offset} to continue, or grep the file first "
        f"(bash: grep -n <pattern> <path>) to jump to the relevant section."
    )
```

Note: there is **no dedicated grep tool** — grep is run through the `bash` primitive, so the
marker says `bash: grep -n`. (Primitives present: bash, read, run_python, sandbox, write.)

### 2. Executor signature + flow (`read.py`)

New signature (defaults change: `max_bytes` → `None`; add `offset`/`limit`):

```python
async def read_executor(
    path: str,
    max_bytes: int | None = None,
    tail_lines: int | None = None,
    offset: int = 1,
    limit: int | None = None,
    *,
    ctx: TraceContext,
) -> dict[str, Any]:
```

Flow after path-governance + is_file checks (unchanged):

- **Tail mode** (`tail_lines is not None`): unchanged, except the output cap becomes
  `cap = max_bytes if max_bytes is not None else LEGACY_TAIL_CAP`. This preserves 1 MiB log
  reads (AC #3 — no regression). Keep returning `{success, path, size_bytes, content,
  truncated, tail_lines}`.
- **Line-head mode** (default): remove the `actual_size > max_bytes` → `too_large` branch
  entirely. Instead:

```python
    effective_limit = limit if limit is not None else DEFAULT_LINE_LIMIT
    byte_cap = max_bytes if max_bytes is not None else DEFAULT_HEAD_BYTES
    try:
        with resolved.open("rb") as fh:
            content, truncated, lines_returned, total_lines = _read_line_window(
                fh, offset, effective_limit, byte_cap
            )
    except PermissionError as exc:
        ...  # unchanged permission_denied return
    except OSError as exc:
        ...  # unchanged io_error return

    marker = (
        _build_read_marker(max(offset, 1), lines_returned, total_lines) if truncated else None
    )
    log.info("read_executor_success", path=str(resolved), size_bytes=actual_size,
             offset=offset, limit=effective_limit, lines_returned=lines_returned,
             total_lines=total_lines, truncated=truncated, trace_id=trace_id)
    return {
        "success": True,
        "path": str(resolved),
        "size_bytes": actual_size,
        "content": content,
        "truncated": truncated,
        "offset": max(offset, 1),
        "limit": effective_limit,
        "lines_returned": lines_returned,
        "total_lines": total_lines,
        "marker": marker,
    }
```

Update the executor docstring: remove `too_large` from the error list; document `offset`/`limit`
(1-based offset, line count), the new default head behavior, the `marker` field, and tail-mode
precedence (if both `tail_lines` and `offset`/`limit` are set, `tail_lines` wins).

### 3. `ToolDefinition` description + params (`read.py`)

Rewrite `read_tool.description` to enumerate the modes and nudge grep-first, e.g.:

> Read a file's content. By default returns a **truncated head** (first ~200 lines / ~8 KB) with
> `truncated=true` and a `marker` explaining how to get more — page with `offset`/`limit`, or run
> `grep` via the bash tool to jump to the relevant section first (cheaper than reading the whole
> file). Use `offset` (1-based line) + `limit` (line count) to read a specific range. Use
> `tail_lines` for the end of growing log files. Set `max_bytes` only for a deliberate large read.

Add two `ToolParameter`s (`offset`, number, default 1; `limit`, number, default None → 200) and
change `max_bytes` default to `None` with an updated description ("explicit byte cap; default
~8 KB in normal mode, 1 MiB in tail mode"). Update the `tail_lines` description if needed.

## Tests (TDD — write/adjust first, watch them fail, then implement)

In `tests/test_tools/test_primitives_read.py`:

**New tests:**
1. `test_read_default_truncates_head` — write 500 short lines; default read → `lines_returned == 200`,
   `truncated is True`, `total_lines == 500`, `marker` contains `"offset=201"` and `"grep"`.
2. `test_read_small_file_not_truncated` — small file → full content, `truncated is False`,
   `marker is None`.
3. `test_read_offset_limit_range` — 50 lines; `offset=10, limit=5` → exactly lines 10-14.
4. `test_read_offset_beyond_eof` — 5 lines; `offset=100` → `success`, empty content,
   `lines_returned == 0`, `total_lines == 5`.
5. `test_read_byte_cap_truncates_head` — 50 lines but each very wide so 8 KB hits before 200 lines
   → `truncated is True`, `len(content.encode()) <= 8192`, and the returned content ends on a
   **whole line** (no mid-line clip).
6. `test_read_explicit_max_bytes_allows_large` — file > 8 KB; `max_bytes=200_000` → returns full
   content (within 200-line limit), proving explicit opt-in still allows big byte reads.
7. `test_read_empty_file` — empty file → `success is True`, `content == ""`,
   `lines_returned == 0`, `total_lines == 0`, `truncated is False`, `marker is None`.
8. `test_read_head_no_trailing_newline` — head-mode file whose last line has no trailing newline
   and fits under the cap → full content returned, last line intact, `truncated is False`.
9. `test_read_paging_across_byte_cap_no_loss` (Codex-requested) — file of N wide lines sized so a
   default read byte-clips after K whole lines; read page 1 (default), parse the marker's
   `offset`, read page 2 with that offset; assert the concatenation of page-1 + page-2 lines
   equals the original lines with **no gap and no duplication** across the boundary.

**Rewrites (these currently assert the removed `too_large` error):**
7. `test_read_too_large` → rename to `test_read_no_too_large_truncates`: 1100-byte single-blob file
   read with `max_bytes=1000` → `success is True`, `truncated is True`, `len(content.encode()) <= 1000`
   (no `error` key).
8. `test_read_tail_bypasses_size_gate` → drop the `normal["error"] == "too_large"` assertion;
   replace with `normal["success"] is True and normal["truncated"] is True`. Keep the tail-branch
   assertions as-is.

**Unchanged (must still pass):** `test_read_happy_path` (2-line file < cap → full content),
`test_read_not_a_file`, the two governance tests, and the remaining `tail_lines` tests
(`_basic`, `_more_than_file_has`, `_output_capped_by_max_bytes`, `_no_trailing_newline`). Add
`test_read_tail_default_cap_unchanged` asserting tail mode without `max_bytes` does not clip a
>8 KB-but-<1 MiB log (guards AC #3).

## Quality gates (all must pass before PR)

```bash
make test-file FILE=tests/test_tools/test_primitives_read.py   # module first
make test                                                      # full suite
make mypy
make ruff-check && make ruff-format
pre-commit run --all-files
```

## PR

Branch `starry-plaza-1s/fre-410-read-tool-per-read-token-cap-ranged-offsetlimit-reads-to`
(Linear-suggested). Single PR — no ADR phases to split. Use
`.github/PULL_REQUEST_TEMPLATE.md`; checklist = pre-merge items only. Link FRE-410 and reference
FRE-401 (parent) + the `turn-forensics` skill in the body.

## Post-merge verification (same session as deploy — never deferred)

1. `make deploy` — capture output.
2. **Live behavior probe** — read a large source file through the prod tool path and confirm a
   truncated head, e.g. drive a turn that reads `src/personal_agent/orchestrator/executor.py` and
   confirm the tool result has `truncated=true`, `lines_returned ≈ 200`, and a `marker`.
3. **Turn-forensics (AC #5)** — run the `turn-forensics` skill (`docs/skills/turn-forensics.md`)
   on the new trace and confirm a comparable read no longer shows the ~40K-token single-read jump
   (head read should be ~2K tokens). Paste the before/after token delta.
4. Update `docs/plans/MASTER_PLAN.md` on `main` (header + Last updated); commit + push.
5. Close FRE-410 with PR link, deploy timestamp, and the turn-forensics token-delta evidence.

## Acceptance criteria mapping

- AC1 (large file → ~2K-token truncated head + marker) → executor head mode + `_build_read_marker`,
  test #1.
- AC2 (`offset`/`limit` ranges) → `_read_line_window`, tests #3/#4.
- AC3 (`tail_lines` + explicit `max_bytes` no regression) → tail keeps `LEGACY_TAIL_CAP`, explicit
  `max_bytes` override; tests #6, `test_read_tail_default_cap_unchanged`, unchanged tail tests.
- AC4 (unit tests: default truncation+marker, offset/limit, tail unaffected) → tests #1-#8.
- AC5 (turn-forensics verification) → post-merge step 3.
