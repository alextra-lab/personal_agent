# FRE-846 — Wire master and build dispatch resolution to the external NEXT resolver

**Backing ADR:** ADR-0113 §1 (role model — sensor → brain → hands): "The NEXT-ticket dispatch
resolver stays a separate process master shells out to (fresh each call, like `reconcile_board.py`)
— **not** logic master holds in-context — so dispatch mechanics never bloat master's context."
**Related:** FRE-785 (built the resolver, unwired), FRE-832 (documented the gap, explicitly scoped
out the rewire — see its plan's "Not in scope" section and implementation-step 6 follow-up note).
**Ticket:** FRE-846 (stream:build1, Tier-2:Sonnet, context CLEAR)

## Codex plan review (2026-07-10) and how each finding is closed

1. **`/adr` is an unaddressed duplicate resolver caller.** `.claude/skills/adr/SKILL.md` carries the
   exact same inline busy-guard/priority/blocked-by paragraph as build's, for `stream:adr`. The
   ticket names build + master, but lifecycle-rules says role skills "MUST NOT restate or fork"
   the dispatch contract — leaving `/adr` forked while build/master converge on the resolver
   defeats that. **Closed:** fold `/adr`'s stream-selector into this ticket, same mechanism as
   build's (§3 below covers all three).
2. **Skill prose needs explicit shell-out failure handling.** `next_resolver.main()` exits 1 with
   "no AGENT_LINEAR_API_KEY configured" on a missing key, and `fetch_board` raises on a Linear/API
   failure — a bare "null → STOP" description conflates "no eligible ticket" with "the command
   itself failed." **Closed:** each rewritten paragraph states explicitly: a nonzero exit, invalid
   JSON, or a printed error means STOP and surface stderr — never fall back to reconstructing the
   busy-guard/priority/blocked-by logic inline.
3. **Content-contract tests were too loose/brittle.** Asserting only "new command present" doesn't
   prove the *old* inline logic is gone, and an unscoped "eligible set" phrase could pass by
   accident. **Closed:** scope each assertion to the specific paragraph/step being rewritten, assert
   the new invocation + absence of the old inline markers (`list_issues(`, `includeRelations`)
   inside that paragraph, not file-wide.

Reviewed and confirmed sound: the read-only scope split (§ below), `eligible_candidates()`'s shape
(full sorted list, no busy guard) as the right API for master's invariant check, the
`resolve_next`-preserves-behavior refactor, and reading `context:keep` off the resolver's JSON
`labels` field (already present via `_issue_to_json`) instead of an extra `get_issue` call.

## Scope decision (the design call the ticket flags)

The ticket notes master's advance-dispatch step also **mutates** state (moves the priority pin,
deletes satisfied `blockedBy` relations, sets `context:keep`) — which the resolver does not do
today — and leaves it to whoever builds this to either scope to the read-and-resolve half or
extend the resolver with a mutation mode.

**Decision: scope to the read-and-resolve half only.** Reasoning:
- The resolver's actual duplicated logic across both skills is the **read** side: busy guard,
  Approved-head selection, priority ordering, blocked-by skip. That's the mechanical, judgment-free
  part ADR-0113 §1 wants out of master's context.
- The **mutations** (which ticket to pin, when to jump a queue, clearing a relation) are master's
  own sequencing judgment, not resolution mechanics — ADR-0113 draws its automate/human-gate line at
  reversibility and judgment, not at "touches Linear." Folding mutation into the resolver would turn
  a passive query tool into a stateful actuator, a bigger surface than this ticket's stated gap.
- Build's stream-selector is pure read already (it never mutates) — trivially in scope either way.

So: both skills shell out to the resolver for resolution; master's mutation bullets (priority pin,
`removeBlockedBy`, `context:keep`) stay exactly as they are today, inline in `master/SKILL.md`.

## What exists vs. what's missing

`next_resolver.py`'s public `resolve_next(issues, stream)` returns **one** ticket (or `None`) and
applies the busy guard. That maps directly onto **build's** stream-selector (which picks the one
ticket to build next). It does **not** map onto **master's** advance-dispatch "re-derive the
stream's eligible set" step, which needs the **full** eligible list (to check the "exactly one
Urgent-or-High ticket in the eligible set" invariant — a single winner tells you nothing about
whether a *second* eligible ticket exists) and explicitly does **not** apply the busy guard
(advance-dispatch runs right after the merge that just freed the stream — the ticket that occupied
it just left `In Progress`/`In Review`).

**Missing piece:** a pure function returning the full eligible set (Approved + stream label + no
open blocked-by, sorted), busy-guard-free, plus a CLI mode to print it.

## What to build

### 1. `scripts/dispatch/next_resolver.py`

Add `eligible_candidates(issues: Sequence[IssueSnapshot], stream: str) -> list[IssueSnapshot]`:
the same Approved+label filter, blocked-by skip, and priority/oldest-created sort `resolve_next`
already applies, but returning the **full** sorted list and never checking the busy guard. Refactor
`resolve_next` to call it: `candidates = eligible_candidates(issues, stream); return candidates[0]
if candidates else None`, guarded by the existing `_is_occupied` check up front — pure refactor, its
existing tests must still pass unchanged (no behavior change to `resolve_next`).

Add a CLI `--eligible` flag (`main`): when set, ignore `--json`'s single-issue shape and instead
print the full eligible set — JSON: a list of issue dicts (`_issue_to_json` per entry, empty list
`[]` if none); plain: newline-separated identifiers, or `none` if empty. Default (no `--eligible`)
behavior is completely unchanged.

### 2. `tests/scripts/test_next_resolver.py` (TDD — write first, confirm failing)

- `test_eligible_candidates_ignores_busy_guard` — one `In Progress` ticket + one `Approved` ticket,
  same stream: `resolve_next` → `None` (busy), `eligible_candidates` → `[the Approved one]`. This is
  the behavior master's step actually needs and `resolve_next` structurally cannot give it.
- `test_eligible_candidates_full_sorted_list` — three eligible tickets at different priorities →
  assert the **full** list comes back in priority/oldest-created order (not just the head) — this is
  what lets master check "exactly one Urgent-or-High" against a real second-place ticket.
- `test_eligible_candidates_empty_board` — `[]` in, `[]` out.
- `test_eligible_candidates_skips_blocked` — reuses an existing blocked-head fixture shape, asserts
  the blocked ticket is excluded from the full list (not just skipped as head).
- `test_resolve_next_still_matches_eligible_candidates_head` — for a handful of the *existing*
  fixture boards already in this file (unoccupied cases), assert
  `resolve_next(issues, stream) == (eligible_candidates(issues, stream) or [None])[0]` — pins that
  the refactor didn't change `resolve_next`'s behavior.
- CLI: `test_cli_eligible_json_lists_full_set`, `test_cli_eligible_plain_lists_identifiers`,
  `test_cli_eligible_empty_prints_none_or_empty_list` (mock `fetch_board`/`urlopen` per the file's
  existing CLI-adjacent pattern, or call `main()` with a monkeypatched `fetch_board`).

### 3. `.claude/skills/build/SKILL.md` — stream-selector paragraph

Replace the inline busy-guard/`list_issues`/priority/blocked-by paragraph with: shell out to
`python -m scripts.dispatch.next_resolver --stream build<N> --json`. A nonzero exit, invalid JSON,
or a printed error (e.g. missing `AGENT_LINEAR_API_KEY`) means STOP and surface stderr — never fall
back to reconstructing the busy-guard/priority/blocked-by logic inline. A `null` result (occupied OR
no eligible candidate — the resolver conflates both, and build's response is STOP-and-ask-master
either way) also means STOP. A non-null result names the ticket, and its `labels` field (already in
the JSON) is read directly for the `context:keep` check — no extra Linear call needed at this step
(Step 1's `get_issue` still fetches the full ticket for scope/body). Cite FRE-785 + ADR-0113 §1 so
the "why shell out, not inline" isn't lost. Keep the CLEAR/KEEP bullets and the explicit-ID path
unchanged — only the resolution mechanism changes, not the contract.

### 4. `.claude/skills/adr/SKILL.md` — stream-selector paragraph (same mechanism as build)

Same rewrite as §3, `--stream adr` in place of `--stream build<N>`, same failure-handling prose.
The rest of the paragraph (context-flag handling, explicit-ID override) is unchanged.

### 5. `.claude/skills/master/SKILL.md` — Step 8 "Advance dispatch"

Replace "Re-derive the stream's eligible set (`Approved` + `stream:*` label + no open blocked-by)"
with an explicit shell-out: `python -m scripts.dispatch.next_resolver --stream <s> --eligible
--json`, with the same failure-handling prose as §3 (nonzero exit / invalid JSON / printed error →
STOP and surface, never reconstruct inline). Note the busy guard doesn't apply here (the step runs
right after the merge that freed the stream) and that this is why `--eligible` exists as a distinct
mode from the default resolve. The binding-rules bullets below it (exactly-one-head pin,
sequence-at-dispatch-time, remove satisfied relations, queue-jumper handling) are **unchanged** —
those are master's mutations, out of scope per the Scope decision above.

### 6. `tests/scripts/test_dispatch_skill_contracts.py` — content-contract guards

Section-scope every assertion to the specific paragraph/step being rewritten (extract that
paragraph's text, not the whole file) so the tests actually prove the old inline logic is gone from
*that* call site, not merely that it's absent somewhere or that the new command appears somewhere
else:
- `test_build_skill_uses_external_resolver_for_stream_selector` — extract build's stream-selector
  paragraph; assert it contains `next_resolver --stream build` and does **not** contain
  `list_issues(` or `includeRelations`.
- `test_adr_skill_uses_external_resolver_for_stream_selector` — same shape for `adr/SKILL.md`,
  `--stream adr`.
- `test_master_skill_uses_external_resolver_for_advance_dispatch` — extract master's Step 8 section;
  assert it contains `next_resolver --stream` and `--eligible` and does **not** contain `list_issues(`
  or `includeRelations` inside that section.

## Out of scope

- Mutation support in the resolver (priority pin moves, `blockedBy` removal, `context:keep`
  labeling) — stays inline in master per the Scope decision.
- `prime-master/SKILL.md` — not a resolution caller; already carries a "Coordinator role" note about
  the resolver's existence (FRE-832) and needs no behavior change here.
- Any change to `AGENT_LINEAR_API_KEY` provisioning — both sessions already have it (used elsewhere,
  e.g. `reconcile_board.py`).

## Test commands

```
uv run python -m pytest tests/scripts/test_next_resolver.py -v
uv run python -m pytest tests/scripts/test_dispatch_skill_contracts.py -v
make test
make mypy
make ruff-check
make ruff-format
```

## Acceptance-criteria proof plan (for the master-facing PR comment)

This is a process/tooling ticket (no `src/` change) with no ADR-numbered ACs of its own beyond
ADR-0113 §1's intent ("dispatch mechanics never bloat master's context") — proof is:
- `eligible_candidates` unit tests (busy-guard-free, full sorted list, empty/blocked cases) +
  `resolve_next`'s existing suite passing unchanged (refactor didn't regress it).
- CLI `--eligible` tests (JSON + plain + empty).
- Skill content-contract tests pinning that both skills' resolution steps actually invoke the
  external resolver (not just doc prose asserting it) and that the old inline busy-guard marker is
  gone from build's skill text.
