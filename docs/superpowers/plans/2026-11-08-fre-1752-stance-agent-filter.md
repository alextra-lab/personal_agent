# FRE-1752 — `query_current_stances` drops agent-labeled stances

Ticket: FRE-1752 (Approved, Tier-2). Related: FRE-1742 (study), FRE-1299 (`asserted_by` on Stance), FRE-1739 (same fix for Claims). Design intent: ADR-0126 (stance push surfaces).

## Problem

`MemoryService.query_current_stances` (`src/personal_agent/memory/service.py`) returns every current `HAS_STANCE` edge. It maps a missing or non-`user` `asserted_by` to `"agent"`. A stance that only the assistant asserted therefore reaches the owner's memory push surfaces (topic enrichment and the behavioural profile).

## Decision

Filter in the Cypher `WHERE` clause, not in Python:

```
WHERE s.valid_to IS NULL AND s.invalid_at IS NULL
  AND (s.asserted_by IS NULL OR s.asserted_by = 'user')
```

- `'user'` rows: returned (as today).
- `'agent'` rows: dropped (AC-1). Rows stay in the graph. Nothing is deleted or changed.
- Missing label (`IS NULL`): returned and still read back as `"agent"` (AC-2). Reason, stated in code: the store cannot show who said them (52 rows older than FRE-1299). Dropping them would silently remove 52 of the owner's 85 stances.
- Any other off-vocabulary value: dropped, on purpose. The only writer in `src` (the consolidator, through `_attribute_authorship`) stores `user` or `agent`. `assert_stance` itself stores the value verbatim, so a direct caller could store another string. Returning it would show it as `"agent"`. The prod count (4 user + 29 agent + 52 unlabeled = 85) leaves no room for such a row. A live test guards the choice.

Cypher note: `NULL = 'user'` is `null`, so the `IS NULL` branch must be explicit.

## Scope

- Only `query_current_stances`. `query_claims*` (FRE-1739) is not touched.
- `query_stance_history` (pull-only audit chain, ADR-0126 D5) is not touched. It does not select `asserted_by`, so `search_memory(include_history=True)` can still return an agent-labeled stance. A filter would break the supersession links, so this is a design decision for the owner. The handoff flags it. AC-3 counts what `query_current_stances` returns.
- No schema, no backfill, no change to the returned dict shape.

## Steps

1. **Failing tests first.**
   - Unit (CI, fake driver): `tests/personal_agent/memory/test_query_current_stances.py` — assert the query text carries the `asserted_by IS NULL OR ... = 'user'` predicate. Fails on current main.
   - Live (local, test stack :7688): new class in the same file, `pytest.mark.integration`, rows written by direct Cypher with `FRE1752_` entity names: `asserted_by: 'user'`, `'agent'`, and no property. Assert: user returned, agent absent, unlabeled returned and read back as `"agent"`. Assert the agent edge still exists after the query (no delete).
   - Run: `uv run python -m pytest tests/personal_agent/memory/test_query_current_stances.py -m integration -q` and confirm the agent case fails.
2. **Implement.** Edit the Cypher `WHERE` and the docstring/comment in `query_current_stances`. State the reason for the unlabeled rule in a comment.
3. **Existing test.** `test_returns_and_canonicalizes_asserted_by` feeds an `"agent"` row through a fake driver. The Python canonicalization stays, so the test stays valid. Its docstring gets one line saying the Cypher drops agent rows before this mapping.
4. **Docs.** Update the `query_current_stances` mention in `protocol.py` / `protocol_adapter.py` docstrings only if they claim agent rows are returned.
5. **Gates.** `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`. Then the live test file again.
6. **Self-review.** Commit, then `feature-dev:code-reviewer` on `git diff origin/main...HEAD`.

## Acceptance criteria and proof

| AC | Proof |
|----|-------|
| AC-1 | Live test: agent-labeled stance absent from result. Unit test: predicate in query text. |
| AC-2 | Live test: unlabeled stance returned. Code comment states why. |
| AC-3 | Post-deploy, master runs a count against prod Neo4j: active agent-labeled stances returned by `query_current_stances` = 0, and the same `MATCH` without the filter still counts the 29 rows. Command goes in the handoff. |

## Risk

- Diff class: self-serve. Read path only. No write path, no schema, no cost or governance code.
- Behaviour change: up to 29 stances (agent-labeled, as counted 2026-11-05) leave the push surfaces. This is the ticket's goal.
- Test gap: CI does not run live Neo4j tests. The unit test on query text is the CI guard. The live run is local evidence.
