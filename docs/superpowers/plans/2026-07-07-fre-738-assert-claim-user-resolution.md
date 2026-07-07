# FRE-738: assert_claim resolves to the acting authenticated User

**Backing ADR:** `docs/architecture_decisions/ADR-0107-user-identity-resolution-and-log-propagation.md` — Decision §2 (assert_claim), §3 (assert_stance unchanged), §7 (one-time backfill, master's job — not this ticket).

**Acceptance criteria carried by this ticket:** AC-1 (non-owner claim attaches to that user's own Person), AC-2 (stance still attaches only to owner). AC-4 (backfill) is master's action, flagged in the PR/ticket comment, not implemented here.

## Scope

1. `src/personal_agent/memory/service.py` — `assert_claim` (currently ~line 1646): add required keyword-only `user_id: UUID` param; both Cypher statements (candidate-fetch at ~1682 and write at ~1726) change `MATCH (o:Person {is_owner: true})` → `MATCH (o:Person {user_id: $user_id})`, mirroring the existing `PARTICIPATED_IN` pattern (~line 1027). Candidate-fetch is now scoped per acting user (each user's claims are matched/superseded independently — Susan's claims must never supersede Alex's).
2. `src/personal_agent/memory/service.py` — `assert_stance` (currently ~line 1555): no behavioral change. Add a short code comment above the Cypher citing ADR-0107 §3 so the deliberate non-change is legible.
3. `src/personal_agent/second_brain/consolidator.py` — line 741: pass `user_id=capture.user_id` into `assert_claim(...)`. `capture.user_id` is already in scope (used at line 669 two lines before the wiring block).
4. Tests:
   - `tests/personal_agent/memory/test_claims_stance_cypher.py` — update the two `assert_claim` tests to pass `user_id=` and assert the Cypher matches `Person {user_id: $user_id}`, not `is_owner: true`; add a new test proving a claim with `user_id=A` never appears in the candidate pool of `user_id=B` (regression guard for the ADR's collision risk).
   - `tests/personal_agent/memory/test_claims_stance_storage.py` (integration, live Neo4j) — fixture creates the owner Person keyed by a fixed test UUID; all `assert_claim(...)` calls pass `user_id=<that uuid>`; `assert_stance` calls unchanged (still resolve `is_owner`).
   - `tests/test_second_brain/test_consolidator_claims_wiring.py` — assert `assert_claim` is awaited with `user_id=capture.user_id`.

## Out of scope (explicitly, per ADR)

- AC-3a/3b/AC-5 (structlog contextvars, es_logger, joinability probe) — separate ticket (FRE-739, seam owner).
- AC-4 one-time backfill of the live Claim — master's action; flag in PR + Linear comment.

## Test plan (TDD)

1. `make test-file FILE=tests/personal_agent/memory/test_claims_stance_cypher.py` — update tests first, confirm they fail against current code (still matching `is_owner`).
2. Implement `assert_claim` signature/Cypher change.
3. Re-run same file — expect pass.
4. `make test-file FILE=tests/test_second_brain/test_consolidator_claims_wiring.py` after updating consolidator call site + test assertion.
5. Full fast suite: `make test`.
6. `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.
7. Integration test (not part of `make test`, run manually to confirm no regression): `PERSONAL_AGENT_INTEGRATION=1` is NOT required here — `test_claims_stance_storage.py` uses `pytest.mark.integration` against the isolated test-Neo4j stack (`make test-infra-up` first), separate from the `PERSONAL_AGENT_INTEGRATION=1` LLM-server gate. Run: `make test-infra-up && uv run pytest tests/personal_agent/memory/test_claims_stance_storage.py -m integration -v`.

## Risk classification

Standard — touches `src/` production logic (memory service Cypher + consolidator wiring) implementing an Accepted ADR. Codex plan-review required before coding.
