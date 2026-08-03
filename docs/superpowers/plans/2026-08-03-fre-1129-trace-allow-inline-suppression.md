# FRE-1129: Replace line-keyed identity-threading allowlist with inline `# trace-allow:` suppression

## Scope

`scripts/check_identity_threaded.py` (the ADR-0074 §I3/§I5 pre-commit lint) suppresses known
false-positives via `scripts/identity_threading_allowlist.yaml`, keyed by `(path, line)`. Any
insertion above an entry shifts its line number, and the hook then fails on unrelated code. The
`memory/service.py` entry alone has recorded eleven shifts; the build seat hit this twice in one
session (FRE-1115).

**Design intent is already on record.** ADR-0074 line 68 specifies the escape hatch as it was meant
to exist from the start: `Explicit opt-out: # trace-allow: <reason> for genuine exceptions`. The
2026-05-22 implementation (FRE-376 Phase 3) never built that — it built a path+line YAML file
instead. This ticket is not inventing a new mechanism; it is implementing the one the ADR already
named, and retiring the one that diverged from it.

**Out of scope:** `scripts/check_evidence_truncation.py` shares the identical `(path, line)`
allowlist pattern (modeled directly on this script per FRE-1002) but its allowlist
(`scripts/evidence_truncation_allowlist.yaml`) currently has zero entries — no live pain, no ticket
AC touches it. Flagged as a candidate follow-up, not folded in here (different file, separate hook,
not required to make this ticket's fix work).

## Current allowlist entries (baseline enumeration — AC-2)

Verified against the real tree via `--strict` (bypasses the allowlist, so its output is the ground
truth of what actually violates today):

| # | Path | Line | Pattern | Reason | Status |
|---|------|------|---------|--------|--------|
| 1 | `captains_log/backfill.py` | 206 | log | run_backfill scan start | **live** — `--strict` flags it |
| 2 | `captains_log/backfill.py` | 253 | log | run_backfill scan warning (file loop A) | **live** |
| 3 | `captains_log/backfill.py` | 304 | log | run_backfill scan warning (file loop B) | **live** |
| 4 | `captains_log/backfill.py` | 315 | log | run_backfill scan summary | **live** |
| 5 | `events/consumer.py` | 211 | log | consumer_message_missing_data (pre-parse guard) | **live** |
| 6 | `events/consumer.py` | 223 | log | consumer_deserialize_error (pre-parse guard) | **live** |
| 7 | `events/consumer.py` | 172 | log | consumer_claim_swept (XAUTOCLAIM sweep) | **stale** — see below |
| 8 | `events/consumer.py` | 186 | log | consumer_claim_stuck_messages_error | **stale** — see below |
| 9 | `telemetry/es_logger.py` | 217 | log | elasticsearch_bulk_failed (batch-level) | **live** |
| 10 | `memory/service.py` | 2206 | cypher_merge | dynamic on_create_clauses set origination (FRE-657 FP) | **live** (flagged 5× at the same line by redundant AST paths — pre-existing, harmless, one marker suppresses all 5) |

**Entries 7 and 8 are stale, confirmed by direct run**:
```
$ uv run python scripts/check_identity_threaded.py --strict src/personal_agent/events/consumer.py
src/personal_agent/events/consumer.py:211: log_missing_trace_id
src/personal_agent/events/consumer.py:223: log_missing_trace_id
```
Only 211/223 appear — `_claim_stuck_messages()` (the enclosing function for both 172 and 186) has
no `trace_id`/`ctx`/`session_id` identifier reachable in its own scope, so the lint's scope-aware
exemption (`check_identity_threaded.py`'s `_LogScopeVisitor`) already treats both as exempt on its
own. Codex plan-review (2026-08-03) caught that these two YAML entries have been dead weight since
whatever refactor removed the identity carrier from that function — the allowlist was never
resynced. **Decision (deliberate, per AC "recorded as a deliberate decision"): drop both, add no
inline marker for them.** This is 8 real entries to migrate, not 10.

All 8 live entries must still be exempted after migration (AC-2); the 2 stale entries are retired,
not carried forward.

## Plan

1. **Failing tests first** (`tests/scripts/test_check_identity_threaded.py`):
   - `test_trace_allow_marker_suppresses_violation` — a genuinely-violating fixture (request-scoped
     fn with a `ctx` carrier, `log.info(...)` missing `trace_id`) with `# trace-allow: <reason>`
     inline on the violating line has zero violations. (The prior `test_allowlisted_violations_are_suppressed`
     fixture used a carrier-free `lifecycle()` function that produces no violation regardless of
     allowlisting — vacuous; Codex plan-review flagged this. Don't repeat that mistake.)
   - `test_insertion_above_marker_does_not_break_suppression` — the **AC-1 proof**: same fixture,
     N lines inserted above the marked call, still suppressed.
   - `test_unmarked_violation_of_same_kind_still_flagged` — a second, unmarked call site of the
     same kind in the same file is NOT suppressed by the neighboring marker (AC-3: guard not
     weakened / no bleed-over).
   - `test_strict_ignores_trace_allow_marker` — `lint_file(src, strict=True)` still reports the
     marked violation (preserves current `--strict` semantics: "show me the real state").
   - `test_marker_inside_string_literal_is_not_a_suppression` — a log message *string* containing
     the literal text `# trace-allow: reason` (not an actual comment) does NOT suppress the
     violation. This is the regression test for the false-positive Codex caught: naive
     regex-over-raw-line-text would match text inside a string; only a genuine comment token counts.
   - Update all existing `lint_file(src, allowlist=[])` calls → `lint_file(src)` (signature drops
     `allowlist`).
   - Replace `test_allowlisted_violations_are_suppressed` with the marker-based equivalent above.
   Run: confirm each new test fails against current code before implementing.

2. **Implement** in `scripts/check_identity_threaded.py`:
   - Add a tokenize-based real-comment scanner — **not** a regex over raw source line text (Codex
     plan-review: a naive `re.search` against `source_lines[v.line - 1]` would match the marker text
     even inside a string literal, silently suppressing a genuine violation). Use `tokenize` to
     collect only actual `tokenize.COMMENT` tokens:
     ```python
     TRACE_ALLOW_RE = re.compile(r"^trace-allow:\s*(\S.*)$")

     def _trace_allow_lines(src: str) -> dict[int, str]:
         """1-based line -> reason, for every genuine `# trace-allow: <reason>` comment token."""
         marked: dict[int, str] = {}
         for tok in tokenize.generate_tokens(io.StringIO(src).readline):
             if tok.type == tokenize.COMMENT:
                 m = TRACE_ALLOW_RE.match(tok.string.lstrip("#").strip())
                 if m:
                     marked[tok.start[0]] = m.group(1)
         return marked
     ```
   - `lint_file(path: Path, *, strict: bool = False) -> list[Violation]` — drop the `allowlist`
     param and YAML matching; compute violations as today, then (unless `strict`) drop any violation
     whose `.line` is a key in `_trace_allow_lines(src)`.
   - `main()`: drop `--allowlist`; keep `--strict` wired to the same flag.
   - Drop the now-unused `import yaml`; add `import io`, `import tokenize`.
   - Update the module docstring / header comment describing the suppression mechanism.
   - **Marker placement rule** (document in the module docstring): the comment must sit on the
     exact source line the tool reports for that violation — for a multi-line `log.*()`/`bus.publish()`
     call this is the opening line (`log.warning(  # trace-allow: ...`), for a Cypher string built via
     concatenation this is the line of the *first* string operand, matching the AST node's `lineno`.
     Getting this wrong just means the violation isn't suppressed (fails loud, not silently) — running
     the hook immediately shows if the marker landed on the wrong line.
   - **Owner-approved fold-in**: fix the pre-existing unreachable `elif isinstance(node, ast.Call)`
     branch in `lint_file`'s node-dispatch loop. The preceding `if isinstance(node, ast.Call): ...`
     already consumes every `ast.Call`, so the later `elif isinstance(node, ast.Call) and ... attr
     == "join"` branch (standalone `session.run("...".join(...))` with no surrounding `BinOp`) is
     dead — never checked. Fix: check for the `.join()` shape *inside* the existing `ast.Call`
     branch (alongside the `_is_bus_publish` check), not as a separate unreachable `elif`. Add a
     regression test with a standalone `.join()`-built Cypher MERGE missing origination (no `BinOp`
     wrapper) and confirm it's flagged pre-fix-failing / post-fix-passing.

3. **Migrate the 8 live sites** — add `# trace-allow: <reason>` (condensed from the table above) to
   the exact source line `check_identity_threaded.py` currently flags, in:
   - `src/personal_agent/captains_log/backfill.py` (4 sites)
   - `src/personal_agent/events/consumer.py` (2 sites: lines 211, 223 only — **not** 172/186, retired)
   - `src/personal_agent/telemetry/es_logger.py` (1 site)
   - `src/personal_agent/memory/service.py` (1 site — comment goes on the `"MERGE (e:Entity..."`
     line, i.e. the first operand of the concatenation, since that's the AST node's `lineno`).
   Delete `scripts/identity_threading_allowlist.yaml` — the 11-shift comment trail (AC-4) lives
   entirely inside that YAML file, not in the source, so deleting the file satisfies AC-4 directly;
   no separate in-source comment to remove.

4. **Demonstrate AC-1 live** (not committed, just run and record in the ticket comment):
   ```
   uv run python scripts/check_identity_threaded.py src/personal_agent/; echo "before=$?"
   # insert 3 blank lines above one of the 8 marked sites
   uv run python scripts/check_identity_threaded.py src/personal_agent/; echo "after=$?"
   # revert the blank-line insertion
   ```
   Both must be `0`.

5. **Demonstrate AC-3 live**: temporarily add a genuinely unthreaded `log.warning("x")` inside a
   request-scoped function somewhere in `src/personal_agent/`, run the hook, confirm non-zero exit
   and the new violation listed, then revert.

6. **Quality gates**: `make test` (module: `uv run pytest tests/scripts/test_check_identity_threaded.py -v`,
   then full suite) · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`
   (this exercises the real hook against the real tree — the strongest form of AC-2).

## Files touched

- `scripts/check_identity_threaded.py` (modify)
- `scripts/identity_threading_allowlist.yaml` (delete)
- `src/personal_agent/captains_log/backfill.py` (4 inline comments)
- `src/personal_agent/events/consumer.py` (2 inline comments — 211, 223; 172/186 retired, not marked)
- `src/personal_agent/telemetry/es_logger.py` (1 inline comment)
- `src/personal_agent/memory/service.py` (1 inline comment + delete shift-history comment)
- `tests/scripts/test_check_identity_threaded.py` (modify + new tests)

## Discovered, out of scope (mention in ticket handoff only)

- `lint_file`'s AST dispatch has an unreachable `elif isinstance(node, ast.Call)` branch (standalone
  `.join()` calls never checked) — pre-existing, not caused by or required to fix for this ticket.
- `scripts/check_evidence_truncation.py` shares the same `(path, line)` allowlist pattern but has
  zero live entries today — candidate follow-up ticket, not urgent.
- `events/consumer.py:172,186` YAML entries were stale (see above) — retired during this migration.

No `.pre-commit-config.yaml` change needed — its `check-identity-threaded` entry has no
`--allowlist` reference to remove.
