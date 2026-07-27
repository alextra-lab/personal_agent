# FRE-1002 — Evidence-path boundary + no-silent-truncation guard

**Backing:** ADR-0125 D5, AC-5. Umbrella: FRE-999.
**Branch:** `fre-1002-evidence-path-boundary-guard`
**Status:** Implemented. See "Final implementation notes" at the end for what
changed between this plan and the shipped code — read that section first if
reconciling the two.

## Scope (from the ticket)

1. Define the evidence-path boundary explicitly — which modules/functions feed a
   durable artifact or assembled context — as a written deliverable in its own right.
2. Add a guard that fails CI when content on such a path is shortened without an
   explicit marker recording that it was shortened and by how much.
3. Fix the sites the guard surfaces, including the context-assembly episode fallback
   (`request_gateway/context.py:294`) and the two codex-found sites
   (`orchestrator/executor.py`, `captains_log/reflection_dspy.py`).
4. Prove item 2 (assistant response) is captured whole end to end.

Acceptance criteria (ADR-0125 AC-5, verbatim check): feed the guard one known-bad
mutation per shortening mechanism (bare slice, helper/utility clip, byte/char limit)
on an evidence path → CI fails for each; feed a correctly-marked truncation → CI
passes; run a turn with an assistant response > 200 chars → stored byte length
equals emitted byte length.

## What "CI" means in this repo (verified, not assumed)

`.github/workflows/ci.yml` runs `mypy`, `ruff check`/`format`, and `pytest` jobs
directly. **Pre-commit is not invoked by CI** — `.pre-commit-config.yaml` hooks
(e.g. `check-identity-threaded`) only run locally and are a manual PR-checklist
item (`.github/PULL_REQUEST_TEMPLATE.md`). So a pre-commit-only guard does not
satisfy AC-5's "confirm CI fails" — the guard must be exercised by a **pytest
test that scans the real `src/personal_agent/` tree** and asserts zero
violations, which runs under `backend-unit` in CI. Pre-commit wiring is added too,
for local-loop speed, but it is not the thing that makes AC-5 true.

## Evidence-path boundary (the deliverable)

Definition, adopted directly from ADR-0125 D5: **evidence path = code that
constructs the content value of a D3 record (user message, assistant response,
reasoning trace, tool-call data, a recalled memory item's text, or an
assembled-context entry) before that value reaches a durable capture/graph write
or the context assembled for the model.**

**Superseded by the codex-review revision below**: the boundary is *not*
expressed as a fixed `(module, function)` anchor list (that was this plan's
first draft and codex found it non-verifiable — see "Revision" section). It is
expressed instead as **content identifiers** (the values a shortening
mechanism must never silently touch) plus **one fully-audited whole-file
inclusion**. The table below is retained as the evidence base — every verified
site the boundary must cover — not as the guard's scoping mechanism.

Verified sites (module :: function), each role stated so the boundary is
auditable, not just asserted:

| Module :: function | Feeds | Why it's in scope | Caught by |
|---|---|---|---|
| `request_gateway/context.py :: _query_memory_for_intent` | assembled context | Line 294, the ADR's "worst instance" (episode w/o digest → 200-char user-message clip, zero assistant text). | Rule A (dict key `summary`) |
| `memory/proactive.py :: _build_payload_for_row` | assembled context (candidate) | Builds the episode payload (`summary`) proactive recall serializes toward context. | Rule A (dict key `summary`) |
| `second_brain/consolidator.py :: _process_capture` | durable Turn node | Writes `TurnNode.summary` (stub path) to Neo4j, alongside the *un*truncated `user_message`/`assistant_response` on the same node. | Rule A (attribute `user_message`) |
| `second_brain/entity_extraction.py :: _default_extraction_result` | durable Turn/extraction result | Fallback `summary` written when extraction fails. | Rule A (Name `user_message`) |
| `captains_log/reflection.py :: _extract_failure_excerpt` | durable reflection record | `error_summary` becomes part of the persisted `FailureExcerpt`. | Rule A (Name `last_error`) |
| `captains_log/reflection.py :: generate_reflection_entry` | durable reflection record (input) | `user_message[:200]` truncates the input to the durable-record-producing call. | Rule A (Name `user_message`) |
| `captains_log/reflection_dspy.py :: generate_reflection_dspy` | durable reflection record (input) | Same as above, DSPy path (codex-found site). | Rule A (Name `user_message`) |
| `request_gateway/recall_controller.py :: _scan_session_facts` | assembled context (candidate) | `matching_sentence` is the text of a `RecallCandidate` rendered into context. | Rule A (Name `content` → `matching_sentence`) |
| `request_gateway/state_document.py` (4 `_extract_*` functions) | assembled context | `build_state_document`'s output is prepended to every turn's context. | Rule C (whole-file) |
| `orchestrator/executor.py :: step_llm_call` (line ~4219, **FRE-1010's site**) | assembled context (rendered prompt text) | `mem.get('summary', mem.get('user_message', ''))[:150]` — codex-found *and* independently named in FRE-1010's ticket body. | Rule B (`.get(key)` chain) |

**Excluded, and why (checked, not overlooked):**
- `tools/artifact_tools.py::_truncate_plan`, `tools/primitives/bash.py::_truncate_to_bytes`,
  `llm_client/history_sanitiser.py`, `orchestrator/context_window.py`
  (`TRUNCATION_MARKER`) — bound *tool output* or the *live conversation window*
  for cost/window reasons, not a durable artifact or assembled-context
  construction; several already carry an explicit marker/flag and are the
  reference pattern for "correctly marked."
- `request_gateway/recall_controller.py:169,284` and
  `captains_log/reflection.py::_parse_reflection_response` — diagnostic log
  fields, excluded structurally by the log-call exclusion (see Revision).
- `request_gateway/context.py:52` (`_session_topic_hint`) — feeds a retrieval
  query parameter (`suggest_relevant(session_topic_hint=...)`), not evidence
  content; checked and confirmed neither the sliced expression nor its
  destination name matches any content identifier, so no exclusion rule was
  even needed.
- `captains_log/capture.py` (`TaskCapture.user_message`/`assistant_response`)
  is the sink, already whole end-to-end — verified, not touched (see "AC-5
  tail" below).

This table plus the Rule A/B/C definitions (Revision section) is the boundary —
a reviewer can check any given site against it without needing to trust an
anchor list that could silently go stale.

## Guard design — `scripts/check_evidence_truncation.py`

Modeled directly on the existing `scripts/check_identity_threaded.py` (AST lint,
`Violation` dataclass, `lint_file(path, allowlist)`, YAML allowlist, CLI `main()`)
— same shape the project already uses for an ADR-backed static gate
(ADR-0074 §I3/I5), so this is a second instance of an established pattern, not a
new one.

**Scope (revised per codex review — see "Revision" section above): tree-wide
over `src/personal_agent/`, not anchor-restricted.** Three rules, applied to
every file:

- **Rule A — content-identifier match.** A slice/clip is a candidate violation
  when it targets an expression identified by name as evidence content:
  `EVIDENCE_CONTENT_NAMES = {"user_message", "assistant_response", "summary",
  "content", "last_error", "error_summary", "matching_sentence",
  "stub_summary"}`, matched against an `ast.Name`, an `ast.Attribute.attr`, a
  dict key, or a keyword-argument name (the shape all known sites already
  have: `"summary": user_message[:200]`, `error_summary = last_error[:200]`).
  No file/function restriction — this is what makes relocation-proof detection
  possible (codex Q1/Q2 fix).
- **Rule B — `.get(key, default)` chains.** `X.get(K, ...)` where `K` is a
  string literal in `EVIDENCE_CONTENT_NAMES`, and the call (optionally
  followed by `.strip()`) is itself sliced/clipped. Needed because
  `executor.py:4219`'s `mem.get('summary', mem.get('user_message',
  ''))[:150]` has no named variable for Rule A to match — verified by reading
  the site directly (see FRE-1010 corroboration above).
- **Rule C — whole-file scope for `request_gateway/state_document.py` only.**
  Every `[:N]` in this one file (verified: 5 sites, none feeding a log call or
  a query parameter) becomes part of the assembled state document, so
  generically-named locals (`line`, `first_line`) are still caught. Not a
  general "small files get whole-file treatment" rule — audited individually.

**Two structural exclusions** (both required to avoid false positives,
verified against real call sites, not assumed — see "Revision" for the
specific lines each one prevents from false-flagging):
- **Log-call exclusion.** A match is exempt if its immediate consuming call is
  `log.*`/`logger.*` with method in `{info, debug, warning, error, exception,
  critical}` — mirrors `check_identity_threaded.py::_is_log_call`.
- **Marker exemption** — see below.

**Three shortening-mechanism shapes detected (same shapes regardless of which
rule matched the target identifier):**
1. **Bare slice** — `ast.Subscript` with an `ast.Slice` node on the matched
   expression (`x[:200]`, `x[0:200]`), where the slice's `upper` bound (if a
   constant) is `>= 50`. The `>= 50` floor excludes item-count caps
   (`entity_names[:5]`, `key_entities[:5]`, `errors[:3]`) — verified by
   grepping every `[:N]` across the evidence-boundary files: list caps
   observed are ≤ 15, character-truncation idioms observed are ≥ 80, so any
   threshold in that gap is safe; 50 is chosen with margin on both sides.
2. **Helper/utility clip** — a `Call` whose callee name matches
   `re.compile(r"truncat|clip|excerpt", re.I)` used as (or inside) the matched
   expression, and is **not** the sanctioned marker helper (next point).
3. **Byte/char limit** — the bare-slice shape chained off `.encode(...)`
   (`x.encode("utf-8")[:200]`) — reported as a distinct `kind` for diagnostics,
   same underlying AST detection as (1).

**The marker exemption — what "correctly marked" means:** a new helper,
`captains_log/turn_evidence.mark_truncated(text: str, limit: int, *, unit:
Literal["chars", "bytes"] = "chars") -> str`, which shortens *and* appends a
literal marker (`"...[truncated N chars]"` / `"...[truncated N bytes]"`) —
i.e. shortened-with-an-explicit-marker-of-how-much, per D5's own wording.
**Per codex Q4:** recognized by its *resolved import binding*
(`from personal_agent.captains_log.turn_evidence import mark_truncated`, or the
module-qualified call), not bare callee-name matching — a shadowed or
unrelated local function named `mark_truncated` must not suppress a real
violation. `N` is precisely omitted characters (`unit="chars"`) or omitted
UTF-8 bytes (`unit="bytes"`). Tests cover: shadowed-local-function is still
flagged, multibyte text near the boundary, and the exact-boundary case
(`len(text) == limit` → no-op, no marker appended).

**Files:**
- `scripts/check_evidence_truncation.py` — the guard (`Violation`, `lint_file`,
  `EVIDENCE_CONTENT_NAMES`, `main`).
- `scripts/evidence_truncation_allowlist.yaml` — one entry at merge time:
  `orchestrator/executor.py` line ~4219, reason "deferred to FRE-1010" (see
  "Deferred, on explicit instruction" above). Every other known site is fixed,
  not allowlisted. Present for the same reason
  `identity_threading_allowlist.yaml` is — a future genuine false-positive has
  a place to go without touching the checker.
- `tests/scripts/test_check_evidence_truncation.py` — unit tests against
  synthetic `tmp_path` fixtures (one per mechanism + the marked-pass case +
  the log-call exclusion + the `.get(key)` chain shape, per AC-5), plus one
  test asserting the guard flags the real `executor.py:4219` shape when the
  allowlist is bypassed (`--strict`), proving it catches the FRE-1010 site and
  not just synthetic mutations.
- `tests/personal_agent/events/test_evidence_truncation_gate.py` (mirrors
  `test_bus_publish_carries_identity.py` exactly) — `subprocess.run(["uv",
  "run", "python", "scripts/check_evidence_truncation.py", "src/personal_agent"])`
  (allowlist active) asserts empty stdout. **This is the test that actually
  gates CI** (see "What CI means" above) — it runs under `backend-unit` in
  `ci.yml` because it's ordinary pytest.
- Pre-commit hook entry mirroring `check-identity-threaded`, for local-loop
  parity (not the CI enforcement mechanism).

## Fixing the surfaced sites

Every site the guard surfaces gets its raw slice replaced with
`mark_truncated(...)`, **except** where the full value is cheap and available —
in that case store it whole instead of marking a clip, which is D5's preferred
outcome ("stored whole, **or** shortened with an explicit marker") — **and
except `orchestrator/executor.py:4219`, allowlisted and deferred to FRE-1010**
(see above):

- `request_gateway/context.py:294` (the worst instance) — store `ep.get("summary")
  or ep.get("user_message")` **whole** (no slice at all) if under a sane hard cap
  (e.g. `mark_truncated(..., limit=CONTEXT_ITEM_CHAR_CAP)` where the cap is large,
  not 200) — the ADR's complaint is specifically that 200 chars is far below
  where a session's outcome lives, not that no cap should exist.
- `second_brain/consolidator.py:611` and `entity_extraction.py:1089` — since the
  *full* `user_message`/`assistant_response` is already on the same object,
  reconsider whether `summary` needs a separate clipped copy at all vs. reusing
  the full field; if a short label is genuinely wanted for a UI/index reason,
  use `mark_truncated`.
- `captains_log/reflection.py` (2 sites: `_extract_failure_excerpt`,
  `generate_reflection_entry`) and `reflection_dspy.py` — dimension-1 producer;
  `mark_truncated` is sufficient here since the ADR's retraction (§3.3)
  confirms these consumers are correctly system-side and don't need full
  fidelity, only an honest marker instead of a silent one.
  `_parse_reflection_response`'s log line is **not** fixed — it's outside the
  boundary entirely (log-call exclusion), not merely deprioritized.
- `recall_controller.py`, `state_document.py` (4 `_extract_*` functions) —
  `mark_truncated` with a limit large enough that it stops being the dominant
  case (re-derive the p50/p90 figures per the ticket's instruction — see next
  section — rather than keeping 200/150 verbatim).
- `orchestrator/executor.py:4219` — **not fixed in this PR.** Allowlisted with
  a reason citing FRE-1010. The guard proves it *would* catch this shape via a
  dedicated test that runs the lint against the real file content with the
  allowlist bypassed.

## Re-deriving the docstring's percentile figures (ticket's explicit instruction)

The ticket says the digest producer's docstring (`second_brain/session_summary.py:14-19`)
"asserts these figures without carrying the query behind them, so re-derive
before relying on them." Before picking any new limit value above, run a
one-off query (captures on disk or ES `agent-captains-captures-*`) computing
p50/p90 char length of `user_message` and `assistant_response` across a real
sample, and cite the query + result in the PR/Linear comment. This does not
block the guard's existence (the guard's job is mechanism-detection, not limit
tuning), but it does inform what `limit=` value each fixed site uses — a limit
picked without re-deriving would repeat the exact mistake D5 retires.

## AC-5 tail: assistant-response fidelity end to end (revised per codex Q5)

A synthetic `TaskCapture(assistant_response=long_text)` round trip only proves
serialization is lossless *after* the value already crossed the production
seam — not that the seam itself is. Two tests, not one:

1. **Integration test** — drive a real completed turn (mocked LLM client
   returning a > 200-char response) through `service/app.py`'s response-emission
   path (the `response_content` value cited at `app.py:2099`) into
   `TaskCapture` creation (`executor.py:2520`/`2549`,
   `assistant_response=ctx.final_reply`), and assert the UTF-8 byte length of
   what was emitted equals the byte length of `TaskCapture.assistant_response`.
2. **Storage round trip** — build a `TaskCapture` with `assistant_response` >
   200 chars, round-trip it through `write_capture`/`read_captures` (disk) and
   the ES normalize path (`es_indexer.normalize_capture_doc_for_es`), and
   assert the byte length read back equals the byte length written. Kept as a
   lower-level supplement to (1), not a replacement for it.

## Test plan (TDD order)

1. `tests/scripts/test_check_evidence_truncation.py`:
   - bare-slice mutation on an `EVIDENCE_CONTENT_NAMES` identifier → flagged (Rule A).
   - helper/utility-clip mutation (`_fake_truncate(x, 200)`-shaped) on same → flagged.
   - byte-limit mutation (`x.encode()[:200]`) on same → flagged.
   - `.get("summary", ...)[:150]` chain mutation → flagged (Rule B).
   - the same mutations on a name **not** in `EVIDENCE_CONTENT_NAMES` → clean
     (proves the guard isn't a blanket ban on slicing).
   - the same mutations feeding a `log.info(...)`/`logger.warning(...)` kwarg →
     clean (log-call exclusion).
   - `mark_truncated(x, 200)` (correct import) → clean; a shadowed local
     function also named `mark_truncated` → still flagged.
   - a `[:5]`-shaped list-count cap → clean (threshold exclusion).
   - real-tree scan over `src/personal_agent/` with the allowlist active
     (mirrors `check_identity_threaded.py --strict` usage) → zero violations
     once sites are fixed (this is the TDD red/green for the whole ticket).
   - real-tree scan with `--strict` (allowlist bypassed) → the `executor.py`
     FRE-1010 site is flagged (proves the guard catches the real shape, not
     just synthetic fixtures).
2. `captains_log/turn_evidence.py` — unit tests for `mark_truncated` (chars/bytes,
   under-limit no-op, marker format, exact-boundary case, multibyte text).
3. Per-site regression test confirming the new behavior (whole-or-marked) at
   each fixed site — reuse/extend the existing test files found for each module
   (`tests/personal_agent/request_gateway/test_context.py`,
   `tests/personal_agent/memory/test_proactive.py`,
   `tests/test_second_brain/test_consolidator_stub_turn.py`,
   `tests/test_second_brain/test_entity_extraction.py` /
   `test_entity_extraction_contract.py`,
   `tests/test_captains_log/*reflection*`,
   `tests/personal_agent/request_gateway/test_recall_controller.py`,
   `tests/personal_agent/request_gateway/test_state_document.py`).
   `executor.py:4219` is explicitly **not** touched (FRE-1010's site).
4. `tests/personal_agent/events/test_evidence_truncation_gate.py` — the
   CI-enforcing contract test (mirrors `test_bus_publish_carries_identity.py`).
5. The AC-5 tail: integration test + storage round-trip test (above).

## Risk tier

**Standard** — touches `src/` logic across 8 modules plus a new CI-facing static
analysis script. Codex plan-review required before implementation per the build
skill.

## Revision — codex plan-review findings (folded in before any code was written)

Codex reviewed the plan above (session `019fa39b-0c80-78d2-bb68-592ed5cda06c`)
and returned two **blockers**: the `(module, function)` anchor-list scoping
(Q1/Q2) is not path-general — a fixed anchor list silently misses sites
introduced in a *different* function or file, exactly the failure mode AC-5
warns about ("if the evidence-path boundary is left undefined so the guard's
scope is unverifiable"). Concrete misses it found by reading the actual source:
`state_document.py:126,152,192` (three more `[:150]` clips beyond the one
anchored function, `_extract_goal`) and `executor.py:4219` (a `[:150]` clip in
`step_llm_call`, a function never anchored). **`executor.py:4219` was
independently confirmed** against FRE-1010 (queued next in this stream), whose
own ticket body names this exact site and states the risk in the same terms:
*"if the guard's definition of a shortening mechanism is keyed to the specific
constant rather than to the operation, it will not [catch it]"* — two
independent sources converging on the same finding is a strong signal the
anchor-list design was wrong, not just incomplete.

**Design change: source-name matching, tree-wide, not function-anchored.**
Instead of restricting the AST walk to named `(file, function)` anchors, the
guard scans all of `src/personal_agent/` and flags a shortening shape only when
it targets an identifier — `ast.Name`, `ast.Attribute.attr`, a dict key, or a
keyword-argument name — in `EVIDENCE_CONTENT_NAMES = {"user_message",
"assistant_response", "summary", "content", "last_error", "error_summary",
"matching_sentence", "stub_summary"}`. This is no longer anchored to *where*
the code lives, so relocation or a new call site in an unrelated function is
caught automatically — resolving the blocker without needing dataflow analysis
check_identity_threaded.py doesn't do either.

Two additions on top of tree-wide name matching, each independently verified
against the real files (not assumed):

- **Rule B — `.get(key, default)` chains.** `mem.get("summary",
  mem.get("user_message", ""))[:150]` (`executor.py:4219`) has no named
  variable to match — the slice applies directly to the `.get(...)` call
  result. Matched separately: `X.get(K, ...)` where `K` is a string literal in
  `EVIDENCE_CONTENT_NAMES`, and the call (or an immediate `.strip()`/`.get()`
  chain off it) is the target of a shortening shape. This is what actually
  catches the FRE-1010 site — confirmed by reading `executor.py:4195-4222`
  directly: the render branch takes the top 3 of `ctx.memory_context` and
  applies exactly this shape.
- **Rule C — whole-file scope for `request_gateway/state_document.py` only.**
  `_extract_constraints`/`_extract_recent_actions`/`_extract_open_questions`
  clip through generically-named locals (`line`, `first_line`,
  `first_question_line`) that don't match any content name. Verified by
  grepping every `[:N]` in the file (5 sites, lines 63/101/126/152/192) and
  reading each one: none feed a log call or a query parameter, all become part
  of the document `build_state_document` returns — so whole-file scope here
  has zero false-positive risk, unlike doing the same for `context.py` or
  `recall_controller.py` (see exclusions below). This is a one-file, fully
  audited special case, not a general "whole-file when small" rule.

**Two structural exclusions, verified necessary by reading real call sites —
without them, tree-wide Rule A produces false positives:**

- **Log-call exclusion.** `recall_controller.py:169`
  (`message_excerpt=user_message[:80]`) and `:284`
  (`top_candidate_fact=candidates[0].fact[:100]`) are `logger.info(...)`
  diagnostic fields — `user_message` matches `EVIDENCE_CONTENT_NAMES` but the
  destination is a log line, not a durable artifact or assembled context.
  Codex's "Additional findings" independently flagged the analogous
  `reflection.py::_parse_reflection_response` log line for the same reason.
  Rather than one-off allowlisting each, the guard exempts *any* shortening
  shape whose immediate consuming call is a recognized log call — receiver
  name `log` or `logger` (both conventions are live in this codebase — grepped
  across all 9 files), method in `{info, debug, warning, error, exception,
  critical}` (same shape `check_identity_threaded.py::_is_log_call` already
  checks for). This removes 3 known false positives structurally instead of by
  allowlist, and will keep doing so for future log lines.
- **Not needed after verification: a query-parameter exclusion.**
  `context.py:52` (`_session_topic_hint`, `[:800]`) feeds
  `suggest_relevant(session_topic_hint=...)` — a retrieval query input, not
  evidence content. Checked whether this needs an explicit exemption: it does
  not, because neither `session_topic_hint` nor the anonymous `" ".join(...)`
  expression it slices matches any name in `EVIDENCE_CONTENT_NAMES`. No rule
  change required, but recorded here because it was checked, not assumed.

**Q3 (CI-enforcement mechanism) — verdict low-severity, confirmed correct, but
mirror the established pattern exactly rather than inventing a new one.** Codex
found `tests/personal_agent/events/test_bus_publish_carries_identity.py` already
does precisely this for the identity guard: `subprocess.run(["uv", "run",
"python", "scripts/check_identity_threaded.py", "--strict", "src/personal_agent"])`
and asserts no matching violation lines in stdout. The new guard's CI-enforcing
test copies this shape exactly (`scripts/check_evidence_truncation.py --strict
src/personal_agent`), rather than the plan's original ad hoc "loop `lint_file`
over anchors" approach.

**Q4 (marker semantics) — severity high, folded in.** `mark_truncated` must be
recognized by its resolved import binding
(`personal_agent.captains_log.turn_evidence.mark_truncated`), not by bare
callee name — a shadowed or unrelated function named `mark_truncated` must not
suppress a real violation. `N` is defined precisely as omitted
characters/UTF-8-bytes (matching the `unit` parameter), and tests cover a
shadowed local function, multibyte text, and the exact-boundary case
(`len(text) == limit`).

**Q5 (assistant-response fidelity) — severity high, folded in.** A synthetic
`TaskCapture(...)` round trip only proves serialization *after* the value
already crossed the production seam — it does not prove the seam itself is
lossless. Replaced with an integration test that drives a real completed turn
through response emission (`service/app.py`'s `response_content`, per codex's
citation at `app.py:2099`) into `TaskCapture` creation and the disk + ES
normalize path, asserting byte-for-byte equality between what was emitted and
what is read back. The synthetic serialization test is kept as a lower-level
supplement, not a replacement.

**Additional finding, folded in: drop `reflection.py::_parse_reflection_response`
from the evidence boundary entirely** (not "included for consistency" as the
original plan hedged) — it's a diagnostic log of a failed parse, not a durable
record or assembled-context content, and the log-call exclusion above makes
this automatic rather than a special case.

## Deferred, on explicit instruction: the FRE-1010 site

`executor.py:4219`'s `[:150]` clip is confirmed as a real, guard-surfaced
violation (Rule B). Its *fix* belongs to **FRE-1010** (queued next in this
stream), which redesigns this entire render branch — the score-blind cap of 3
and the entity-empty-bullet defect are the same two lines, and fixing the
truncation here in isolation would conflict with or be made redundant by that
redesign. This ticket's job is to make the guard **catch** it, not fix it.
Add one allowlist entry in `scripts/evidence_truncation_allowlist.yaml` for
this exact `(path, line)`, with a reason citing FRE-1010, so this PR's CI is
green while the violation stays visible in the allowlist diff for whoever
reviews it. `test_check_evidence_truncation.py` includes a regression test
proving the guard *would* flag `executor.py:4219`'s shape if the allowlist
entry were removed (using the real file content, not a synthetic fixture) —
this is the guard's proof it actually catches the site FRE-1010 will fix, not
just synthetic mutations.

## Final implementation notes (post-implementation reconciliation)

Running the implemented guard against the real tree surfaced more than the
plan anticipated, and required narrowing two rules to eliminate genuine false
positives — both corrections, not scope drift, discovered by actually running
the tool rather than reasoning about it in the abstract.

**Genuinely new evidence-path sites found (not in the ADR's 11, not in the
plan's table), fixed in this PR:**
- `tools/personal_history.py` and `tools/memory_search.py` — both silently
  clipped a recalled past `user_message` to 300 chars in their tool-result
  payload. These are tools whose entire purpose is surfacing past-conversation
  text back to the model on request; silently shortening the very thing the
  model asked for is squarely in scope. This is exactly what AC-5 anticipated
  by requiring a guard rather than a fixed list.
- `executor.py:4222`'s FRE-1010 site was originally cited by the plan/codex at
  line 4219; the true line drifted as edits were made. `evidence_truncation_allowlist.yaml`
  and the regression test both use the current line.

**Two rule narrowings, each because the broad version produced a real false
positive when actually run:**
- `EVIDENCE_CONTENT_NAMES` includes `"content"` for Name/Attribute matching
  (precise — a variable literally named `content` holding parsed
  conversational/LLM text), but a separate, narrower `DICT_KEY_CONTENT_NAMES`
  (excludes `"content"`) governs dict-key/kwarg matching, because
  `tools/web.py` builds `{"content": ib.get("content", "")[:500], ...}` for an
  unrelated SearXNG infobox field that merely shares the key name.
- Rule B's `.get(key, ...)` chain match uses its own narrower `GET_CHAIN_KEYS
  = {"summary", "user_message"}` rather than the full content-name set, for
  the same reason (`.get("content", ...)` collides on arbitrary API-shaped
  dicts; `.get("summary"/"user_message")` does not, verified against every
  real site using this shape).

**Two additional structural exclusions, found necessary by reading real call
sites, not assumed up front:**
- A directory exclusion for `ui/` (`ui/memory_cli.py` formats Rich table
  columns for a human operator — display, not a durable artifact or assembled
  context).
- A "cosmetic label" exclusion for assignments to a bare `title` target
  (`captains_log/reflection.py`/`reflection_dspy.py` build `CaptainLogEntry.title`,
  documented in `models.py` as "Short, actionable title" — the full
  `user_message` remains reachable via the entry's `trace_id` join to the
  untouched `TaskCapture`, so shortening the title loses no evidence).
- `_parse_reflection_response`'s log line was dropped from the boundary
  entirely per codex's finding (diagnostic, not the durable record) — the
  log-call exclusion makes this automatic rather than a fix.

**A slice-shape bug caught by the guard's own false positives**, not by
review: the original `_shortening_kind` misclassified any `x[N:]` (no upper
bound — `orchestrator/skills.py`'s frontmatter-delimiter skip,
`entity_extraction.py`'s JSON-brace-finder) as a truncation. Fixed by requiring
an explicit upper bound before classifying anything.

**`second_brain/consolidator.py` had two truncation sites, not the one the
audit listed** (line 587's `is_fallback` comparison and line 611's
`stub_summary` construction) — both had to move to a single shared
`entity_extraction.default_extraction_summary()` together, because the old
comparison (`summary.strip() == capture.user_message.strip()[:200]`) could
never actually match `_default_extraction_result`'s own output for a message
over 200 chars (the producer appended `"..."`, the comparator's inline slice
did not) — a pre-existing, unrelated bug that became load-bearing to fix
correctly once the shared shape changed, not adjacent cleanup.

**Percentiles re-derived** (the ticket's explicit instruction, not the
docstring's uncited figures): queried `agent-captains-captures-*` directly,
N=1864 non-empty user messages — user_message p50=66, p90=151, p99=400 chars;
assistant_response p50=1035, p90=3556, p99=7940 (the digest producer's
docstring claims p50=58/1847 — in the right range but not reproduced exactly,
confirming the ticket's warning not to cite it uncritically). New limits
(400 for recall/reflection excerpts, 800 for the context.py "worst instance")
are sized against this measurement, not carried over from the retired 200.

**The context.py "worst instance" still has no assistant-text fallback** —
the proactive-recall episode payload shape (`memory/proactive.py`) never
carried an `assistant_response` field at all, so there is nothing to fall back
to; restoring that is a separate, deeper schema change than "mark instead of
silently clip" and is out of this ticket's scope. Noted in-code and here so it
isn't mistaken for an oversight.
