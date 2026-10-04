# FRE-1514 — Scope the planner's briefs to the question as asked

Ticket: FRE-1514 (FRE-1498 P7). Design authority: ADR-0154 D7 (a planner prompt change is a routing
change and needs a passing probe run).

## Scope

- Add one rule to the planner system prompt: a worker fetches the specific facts the answer needs. It
  does not review the field.
- Commit a brief scorer (AC-2) with its counting rule **before** either probe arm runs.
- Run the committed D7 probe twice on the default local primary (`qwen3.8-flash-next`, `--mode planner`):
  the baseline arm on the `main` prompt, then the rule arm. AC-1 reads the rule arm. AC-2 compares both.
- AC-3: the rule renders only when the planner client dispatches in a `planner` mode. Under D7 a
  deployment receives that mode only with a passing probe result. Every other deployment keeps today's
  prompt byte for byte, on every route (the forced HYBRID/DECOMPOSE routes too). A catalog test pins the
  set of deployments with a `planner` mode, so a new one cannot land without its probe result.

## The rule (5th entry of `_PLANNER_BRIEFING_RULES`)

> Scope each task to the question as asked. A worker fetches the specific facts the answer needs. It
> does not review the field: do not ask for background, an overview or a survey of the topic.

## The AC-2 counting rule (committed in `scripts/eval/fre1537/briefs.py`)

- **Brief:** one task of an expansion draw (`HYBRID`/`DECOMPOSE` with at least one task), parsed in full
  from the raw `content` of each row of `decide.jsonl`. Rows are de-duplicated as `score.py` does.
- **Reference text:** the user's message, that is the fixture's current query (`prompts.json` `query`).
  Follow-up history is not reference text, as AC-2 states.
- **Term:** a lower-case run of letters and digits with at least one letter, 3 or more characters, not in a fixed stop list
  (English function words + a fixed list of instruction words such as `find`, `list`, `return`).
- **Stem:** strip a possessive `'s`, then `ies`→`y`, then a final `s` (not `ss`), then a final `e`.
- **Known term:** its stem equals a reference stem, or one stem is a prefix of the other and the shorter
  has 5 or more characters.
- **A goal introduces an entity** when it holds at least one term that is not known.
- **Verdict:** PASS when the rule arm's count of such goals is lower than the baseline arm's AND the rate
  (such goals / all goals) is lower. The rate guards against a count that falls only because the rule arm
  expands less.
- **Provenance in the report:** the SHA-256 of `briefs.py`, both prompt hashes, the system-prompt diff
  between the arms, and the first row time of each arm. The report
  also prints, per arm: expansions, tasks per expansion, the rate of such goals, novel terms per goal,
  goal characters and brief characters (goal plus constraints), split single-turn / follow-up.

## Steps

1. Refactor `llama.parse_plan` to share a `load_plan_json` helper. Add `briefs.py` + tests
   (`tests/test_eval/test_fre1537_briefs.py`). → `uv run pytest tests/test_eval/test_fre1537_*.py -q`.
   Commit (the counting rule is now committed).
2. Baseline capture from a clean tree at this commit (`git status --porcelain` empty, HEAD recorded in
   `$RUN_BASE/provenance.txt`): `gateway up` / `capture` / `render` / `down`. No model call.
3. Add the rule behind `scope_rule` (true only when `_planner_mode_name(llm_client) == "planner"`),
   pass `scope_rule=True` in `render.py`, renumber the FRE-1521 comments, update `tests/personal_agent/orchestrator/test_expansion_controller.py` (count 4→5,
   rule text asserted in the prompt) + the AC-3 test. → `make test-file` on both. Commit.
4. Rule capture: steps 1–4 of the probe into `$RUN_RULE`. No model call.
5. **(owner OK needed — llama.cpp)** `replay --mode planner` on both run dirs (~100 calls each), then
   `longhist --mode planner` on `$RUN_RULE` (9 calls).
6. `score --tag planner` on `$RUN_RULE` (AC-1). `briefs --rule $RUN_RULE --baseline $RUN_BASE` (AC-2).
7. Update the comment in `config/models.yaml` (planner mode now qualified on FRE-1514's probe result).
8. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.
   Self-review, PR, handoff comment with both reports and the fingerprint.

## Acceptance criteria → evidence

| AC | Evidence |
|----|----------|
| AC-1 | `score` report on `$RUN_RULE/rows/planner`, 9/9 D7 thresholds + fingerprint PASS, posted on the ticket |
| AC-2 | `briefs` report, rule-arm count < baseline count, scorer commit SHA precedes both run timestamps |
| AC-3 | tests: a client in the `planner` mode gets the rule; a client in any other mode gets the prompt without it (identical to `main`); the set of deployments with a `planner` mode is `{qwen3.8-flash-next}` |

If the rule arm fails any D7 threshold, the rule does not ship (stop and report to the owner).
