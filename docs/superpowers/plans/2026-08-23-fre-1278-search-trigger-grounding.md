# FRE-1278 — verifiability search trigger + grounding rule

Ticket: https://linear.app/frenchforest/issue/FRE-1278
Branch: `fre-1278-search-grounding`

## Scope (from ticket)

Two prompt-engineering defects in `src/personal_agent/orchestrator/prompts.py::_TOOL_RULES`
(shared by `TOOL_USE_NATIVE_PROMPT` and `TOOL_USE_PROMPT_INJECTED`):

1. **Defect 1** — the only search trigger is keyed to recency ("current events... anything
   requiring live web data"). A question the model doesn't *know* (not-live-data,
   named-entity: brands/products/people/prices/shops) never trips it.
2. **Defect 2** — even when search fires, retrieved and parametric names are blended with no
   distinction, so the model still fabricates alongside real results ("Mazola" next to real
   pasta brands).

Deliberately out of scope: temperature (owner's explicit 2026-08-23 decision, left until this
ships), the intent taxonomy / "cannot express not-knowing" ADR work (→ FRE-1279).

## Design intent

No backing ADR beyond ADR-0008/0032 (tool-use prompt structure) referenced at
`prompts.py:39`. `_TOOL_RULES` is DRY-shared text embedded via f-string into both prompt
variants — the fix must land there so it can't land in only one variant (this is exactly the
FRE-383 precedent: same file, same mechanism, added an anti-fabrication bullet).

## Plan (revised after codex plan-review — see Codex findings below)

1. **Add two rules to `_TOOL_RULES`** (`prompts.py:47-59`), keeping existing bullets intact:
   - Verifiability trigger, inserted right after the "no tool needed" bullet (line 49) so it
     directly qualifies that bullet: search before recommending/comparing/identifying/pricing/
     locating specific real-world brands, products, people, organisations, or shops —
     independent of recency. Guarded against over-triggering: naming an entity in the
     *question* doesn't trigger it; only the *answer* introducing/verifying one does.
   - Grounding rule, appended as the last bullet: after web_search/perplexity_query returns,
     name only entities that appear in that turn's tool output; omit anything else rather than
     marking it unverified. Codex caught that my first draft ("mark unverified" as an allowed
     alternative) directly contradicts AC-2, which fails *any* unsupported name even if marked
     — omission, not labeling, is what AC-2/AC-5 actually require.
   - Tighten the existing "If you have enough information to answer, synthesize immediately"
     (line 54) to "enough **verified** information" so it doesn't dilute the new grounding
     condition.
2. **Add one worked example** to `TOOL_USE_PROMPT_INJECTED` (`prompts.py:81-87`) of an
   entity-naming, non-recency question — the ticket flags both existing examples (FastAPI,
   React-vs-Svelte) as technical-only, teaching the wrong intuition. Low-risk since PROMPT_INJECTED
   isn't the deployed strategy for the OVH model, but it's the same shared-example mechanism
   FRE-383 didn't touch, and costs nothing to fix now.
3. **Update `tests/fixtures/routing_token_baselines.json`** — `tool_use_system_prompt_chars`
   (currently 2176) and `tool_use_system_prompt_max_chars` (currently 2200, only 24 chars of
   headroom) will both need to move. Recompute after the edit, review the diff, set both
   deliberately (mirrors the FRE-383 precedent commit `320efc96`).
4. **Unit tests** in `tests/personal_agent/orchestrator/test_prompts.py` (same file/pattern as
   the existing FRE-383 `test_anti_fabrication_rule_in_tool_rules` tests):
   - New rule text present in `_TOOL_RULES`, `TOOL_USE_NATIVE_PROMPT`, and
     `TOOL_USE_PROMPT_INJECTED` (3 tests, mirroring the FRE-383 trio).
   - Existing FRE-383 anti-fabrication fragment and "Do not invent tools" bullet still present
     (regression guard — same pattern as `test_no_invent_tools_rule_unchanged`).
5. **Live verification is a post-deploy runbook for master, not a build-session action.**
   Course-corrected after finding `scripts/eval/fre453_canonical_evalset/harness.py`'s own
   docstring: *"Running this against the live stack is a master post-deploy action (fre481
   precedent); the harness itself plus its unit tests are the build deliverable."*
   `DEFAULT_CHAT_URL = "http://localhost:9001/chat"` is the actual production gateway
   container — not something a build session stands up or fires against. AC-1 through AC-6 all
   require the fix to be *deployed* (or, for AC-4, require querying the currently-deployed
   pre-fix state), so they belong in the handoff's post-deploy runbook for master to execute
   right after deploying (firing the pre-fix baseline immediately before restart and the
   post-fix checks immediately after, in one sitting).
   - `route_traces` (`docker/postgres/init.sql`, assembled in
     `observability/route_trace/assembler.py`) has `tool_iteration_count`/`tools_used` but no
     content — sufficient for AC-1/AC-3/AC-6, NOT for AC-2 (needs response text + retrieved
     tool output side by side). For AC-2, pull the matching Captain's Log capture by
     `trace_id` (`captains_log/capture.py` — holds `assistant_response` and raw `tool_results`
     together) instead.
   - Runbook commands, per-AC expected values, and the exact questions to fire go in the PR/
     handoff comment (Step 8) verbatim, so master can run them without re-deriving anything.
   - Flag honestly: even with the improved prompt, a 27B model at temperature 0.6 may not
     reliably obey a pure prose "omit unsupported names" instruction (codex's concern) —
     closing that completely may need a structured evidence-mapping validator in the executor,
     which is a separate ADR-scale change (~1-3 days: schema, validation, retry/repair pass),
     not a prompt-only fix. If master's post-deploy run shows AC-2/AC-5 still failing
     intermittently, that's a finding for a follow-up ticket (likely feeding FRE-1279's ADR),
     not something to fold into this PR.
6. **Quality gates**: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`.
7. **Self-review**: `feature-dev:code-reviewer` scoped to `git diff origin/main...HEAD`. No
   security-review trigger (no subprocess/auth/network/secrets touched — pure prompt text +
   test fixture).

## Diff class

Not escalated: this is prompt text + a JSON fixture + unit tests, no production write path,
no schema, no cost/governance code. Self-serve review.

## Risk / codex review

Standard tier (touches `src/` behavioural logic that steers every LLM turn in production) →
codex plan-review required before implementation.
