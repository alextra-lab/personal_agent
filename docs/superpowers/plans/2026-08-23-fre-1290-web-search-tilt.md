# FRE-1290 — The injected surface is tilted toward recall

Branch: `fre-1290-web-search-tilt` · ADR: [ADR-0138](../architecture_decisions/ADR-0138-the-model-may-generate-but-may-not-assert.md) (design intent, not implemented by this ticket — D6/entity-block is explicitly out of scope here)

## Scope (verified against source 2026-08-23)

Three surfaces, confirmed live in the running `cloud-sim-seshat-gateway` config:

1. **No web-search skill exists.** `docs/skills/*.md` has 24 files; the loader skips files with no `name` frontmatter key (`SKILL_TEMPLATE.md`, `EMPIRICAL_TEST_RESULTS.md`), leaving **20 loaded skills** (verified via `get_all_skills()` directly, corrected from an earlier "22" guess). Zero teach web search. `personal-history-recall.md`'s own description reads "for general questions, use search_memory" — the one skill that routes by question type routes everything inward.
2. **`get_tool_awareness_prompt()` truncates the network category.** `orchestrator/prompts.py:151-156` — `if len(tool_names) <= 3: full list else: first 3 + "..."`. 8 tools carry `category="network"` (`web.py`, `perplexity.py`, `context7.py`, `location.py`, `linear.py` ×4) **in the currently-deployed config** (`AGENT_LOCATION_ENABLED=true`; `location.py`'s tool is conditional and the count would be 7 with it off — confirmed both facts against the live `.env` and `settings.py:1458`, not assumed). So the model reads `network (8): web_search, perplexity_query, get_library_docs, ...` — `web_search` survives the ellipsis by luck of dict-sort order, not by design, and a 4th network tool added later (or `location_enabled` toggled off, changing the sort) could push it past the cut.
3. **`web_search`'s tool description reads as a utility and redirects away.** `tools/web.py:56-70` — opens with SearXNG/plugin framing (timezone, unit conversion, calculator), never states "use this when you don't know a fact," and closes with `"Prefer perplexity_query for synthesized answers with citations."`

**Out of scope (per ticket):** the Known-Entities block / "Do NOT say you have no memory" string (ADR-0138 D6, FRE-1283). Diff must not touch `executor.py`'s Known-Entities assembly — verified by AC-6.

## A gap found and deliberately NOT folded in

While tracing how a skill actually gets into `route_traces.skills_loaded`, found: in `keyword`/`hybrid` routing mode, `get_skill_bodies()`-matched skill names never get merged into `ctx.loaded_skills` (`executor.py:4329-4336`), so keyword-matched skills silently don't count as "loaded" for telemetry, and — reading `assemble_skill_usage_directives(list(ctx.loaded_skills), ...)` at `executor.py:4351-4353` — their `nudge` bullets never render either.

**This does not affect this ticket's own measurement, but is not "provably dead in production" either — corrected after Codex's review.** The worktree `.env` (mirroring the primary checkout's) sets `AGENT_SKILL_ROUTING_MODE=model_decided`, which is a structurally different path: a separate Haiku-tier routing call (`route_skills()`, `skills.py:415-543`) picks skill names from a compact index and writes them straight into `ctx.loaded_skills` (`executor.py:4283-4287`) — confirmed directly that `model_decided` mode branches away from `get_skill_bodies()` entirely (`executor.py:4316` onward). But `settings.skill_routing_mode`'s **tracked default is `hybrid`** (`settings.py:2166`, `.env.example`), and `/chat` accepts a **per-request `skill_routing_mode` override** (`app.py:1983`, read at `executor.py:4249`, taking priority over the global setting) — so the keyword/hybrid path, and this gap, remain reachable on any request that passes the override, regardless of the deployed default. Correct framing: **out of scope for AC-1/AC-2's own measurement run** (which uses no override, so `model_decided` governs), **not** globally dead code. Not folding it in here because the ticket's "what to build" doesn't ask for it and it isn't needed for this ticket's ACs to be measurable — but it should be named as a real, separately-fixable bug in the handoff, not asserted as inert.

**Consequence for the skill file (surface 1):** `model_decided` mode's router sees *only* `- name: description` per skill (`assemble_skill_index()`, `skills.py:191-221` — not `when_to_use`, not `keywords`). So **`description` is the field that is empirically load-bearing** for whether the router selects the new skill; `keywords`/`when_to_use`/`nudge` are written for format-completeness and for `hybrid`/`keyword` mode (and for a human/model reading the body directly via `read_skill`), but they are not what AC-3 measures.

## Code changes

### 1. `docs/skills/web-search.md` (new)

Frontmatter modeled on `personal-history-recall.md`:
```yaml
---
name: web-search
description: Search the live web via web_search when a question needs a fact about the real world — a specific brand, product, shop, price, current name, or availability — that cannot be answered reliably from training data alone. Complements search_memory (the shared graph) and recall_personal_history (the user's own history); use this when the answer isn't in either.
when_to_use: >
  When the user asks you to recommend, identify, compare, price, or locate a specific
  real-world brand, product, person, organisation, or shop — even when nothing about the
  question is time-sensitive. Also for factual claims about the world you are not certain
  of from training alone ("is X high in Y", "does X cause Y"). Not for questions already
  answered by search_memory or recall_personal_history, and not for pure reasoning/math
  that needs no outside fact.
tools: [web_search]
nudge: "A question about the world, not about this conversation or the user's own history, goes to web_search — not search_memory. Naming a brand or product in your answer without a tool result behind it is a guess, not an answer."
keywords:
  - which brand
  - where can i buy
  - where to buy
  - is it still
  - is x still
  - what is the best
  - what's the best
  - which is better
  - recommend a
  - is it true that
  - is it high in
  - does it cause
---
```
Body (full text, since a skill body is injected verbatim into prompts and Codex correctly flagged "short, 2-3 examples" as unreviewable):

```markdown
# SKILL: web-search

> **Tier:** 1 — native tool
> **Tool:** `web_search`
> **ADR:** [ADR-0034](../architecture_decisions/ADR-0034-searxng-self-hosted-web-search.md) (self-hosted SearXNG) · [ADR-0138](../architecture_decisions/ADR-0138-the-model-may-generate-but-may-not-assert.md) (grounding contract this skill implements one leg of)

---

## What this skill does

Search the live web for a fact about the real world that you cannot answer reliably from
training data alone — a specific brand, product, shop, price, current name, or availability.
This is the outward-facing counterpart to `search_memory` (the shared graph) and
`recall_personal_history` (the user's own history): those two answer "what has this user or
this system already been told"; `web_search` answers "what is actually true about the world
right now."

---

## When to use vs `search_memory` / `recall_personal_history`

<when_to_use>
  Use web_search when the question needs a specific real-world fact:
    - "which brand of X should I buy", "where can I buy X", "is X still available"
    - "what's the best X for Y" (a live recommendation, not a stored preference)
    - a factual claim you are not certain of ("is X high in Y", "does X cause Y")

  Use search_memory when the question is about what's already known in the shared graph:
    - "what do we know about X", "have we discussed X before"

  Use recall_personal_history when the user scopes to their own history:
    - "what did I ask you last week"

  Naming a specific brand, product, or shop in your answer without a tool result behind it
  is a guess, not an answer — even when the question names an entity itself. The question
  containing a name does not mean you already know the answer; it means you know what to
  search for.
</when_to_use>

---

## Worked examples

<example>
  User: Which brand of tinned tuna should I buy in a French supermarket?
  No stored preference exists, and no training-data snapshot can be current on French
  supermarket stock. Call: web_search(query="best tinned tuna brand France supermarket", categories="general")
</example>

<example>
  User: Is Aldi Süd still selling their reusable produce bags in Germany?
  Availability claims decay — search rather than assert from a training-data snapshot.
  Call: web_search(query="Aldi Süd reusable produce bags Germany 2026", categories="general")
</example>

<anti_example>
  User: What's 15% of 240?
  Pure arithmetic — no outside fact needed. Do NOT call web_search; answer directly (36).
</anti_example>

<anti_example>
  User: What do we know about the Acropolis?
  A shared-graph recall question, not a live-web question.
  Call: search_memory(query_text="Acropolis") — do NOT call web_search here.
</anti_example>

See also: [personal-history-recall](personal-history-recall.md), [seshat-knowledge](seshat-knowledge.md)
```

### 2. `orchestrator/prompts.py` — `get_tool_awareness_prompt()`

Codex flagged that removing the cap entirely is unbounded against future growth (MCP-discovered tools share the `mcp` category and are user-configurable, not a fixed repo constant). Fix: raise the truncation threshold from `<= 3` to a generous fixed cap (`<= 25`) rather than removing it — every category in the current registry (largest is well under 25) renders in full, satisfying AC-5 ("every network tool is named") without removing the safety valve for a category that could grow arbitrarily large via MCP discovery. Comment the cap's purpose so a future MCP-driven category isn't silently re-hidden.

### 3. `tools/web.py` — `web_search_tool.description`

Rewrite to lead with when-to-reach-for-it framing and drop the perplexity redirect:
```python
description=(
    "Search the live web when you need a fact about the real world that you are not "
    "certain of from training data — a specific brand, product, shop, price, current "
    "name, or availability. Returns titles, URLs, snippets, infoboxes, and plugin answers "
    "from a private self-hosted metasearch engine.\n\n"
    "Plugin capabilities (returned in the 'answers' field — no engine needed):\n"
    "  - Timezone: query 'time Berlin' or 'clock Tokyo' → current local time\n"
    "  - Unit conversion: query '20 °C in °F' or '10 EUR in USD' (use symbols, not words) → converted value\n"
    "  - Calculator: query '2^10 * 3' → computed result\n"
    "\nWeather: use engines='openmeteo' or categories='weather' for current conditions + hourly forecast.\n"
    "\nCategories: general (default), it, science, news, weather, social_media, files, images, music, videos, recipes.\n"
    "Use 'it' for programming questions, 'science' for academic research, "
    "'news' for current events, 'social_media' for Reddit/Lemmy discussions."
),
```
(Plugin/category detail preserved verbatim; only the opening sentence and the closing `perplexity_query` redirect change.)

Measured directly (not assumed): old description is 842 chars, new is 944 — **grows by 102 chars (~25 tokens)**, not "shrinks slightly" as an earlier draft of this plan claimed (Codex caught this). `tests/fixtures/routing_token_baselines.json` pins `tool_use_system_prompt_chars` for the **static** `TOOL_USE_SYSTEM_PROMPT` constant — native tool schemas (including `web_search`'s description) are a separate, dynamic prompt component not covered by that fixture, so it is not expected to need a bump from this change; confirm via `make test` rather than assuming either way.

## TDD plan

1. **`tests/personal_agent/orchestrator/test_skills.py`** (or a new `test_web_search_skill.py`) — `web-search.md` parses via `_load_all_skills`, has non-empty `description`/`nudge`/`keywords`/`tools`, and its `name` appears in `assemble_skill_index()`'s output.
2. **`tests/test_orchestrator/test_prompts.py`** — extend `test_tool_awareness_returns_string`: with a stub registry of >3 tools in one category, assert the returned string contains every tool name and no `"..."` truncation marker.
3. **`tests/test_tools/test_web.py`** (find/extend existing) — assert `web_search_tool.description` contains a "when to reach for it" opening (e.g. starts with "Search the live web when") and does **not** contain `"Prefer perplexity_query"`.
4. `make test`, `make mypy`, `make ruff-check`/`format`, `pre-commit run --all-files`.

**Cumulative per-request prompt growth** (Codex's estimate, worth stating rather than hand-waving): the new skill's `description` (~170 tokens, injected into both the `model_decided` routing call and, when selected, the primary prompt), the un-truncated tool-awareness categories (small — 20-25 tokens for the current registry), and the `web_search` description growth (~25 tokens) sum to roughly 200-250 tokens added when the new skill is *not* selected, more when it is. This is the direct, intended cost of the fix (visibility costs tokens); no budget/cost-gate change is in scope, and `config/governance/budget.yaml` caps are per-role spend ceilings, not per-prompt token limits, so this growth doesn't interact with them.

## Live measurement protocol (AC-1 / AC-2)

**Substrate:** the real `cloud-sim-postgres` / `cloud-sim-neo4j` / `cloud-sim-elasticsearch` containers (the same ones the deployed `cloud-sim-seshat-gateway` uses) — confirmed by the smoke test below, not the isolated FRE-375 test stack (this measures real deployed behavior per the ticket, not unit-level correctness).

**Runtime:** a local `uv run uvicorn personal_agent.service.app:app --port 9000` process in this worktree, `.env` populated from the primary checkout's secrets (`pass show seshat/AGENT_*`) so config matches the deployed gateway exactly (`AGENT_SKILL_ROUTING_MODE=model_decided`, `AGENT_ENABLE_MEMORY_GRAPH=true`, `AGENT_PREFER_PRIMITIVES=true`, real model/API keys). This is *not* a deploy — no container is rebuilt or restarted; it's an ephemeral dev process pointed at prod-equivalent substrate, the same class `tests/CLAUDE.md`'s escape hatch describes for acceptance-style measurement. AC-1 runs on this process **before** any `src/` edit (current `origin/main` code); AC-2 runs on the same process after the 3 edits + a clean restart — same substrate, same config, only the code differs, so the two runs are as close to a controlled A/B as this environment allows.

**Verified working** (smoke test 2026-08-23, cleaned up immediately after — see Cleanup below): `POST /chat?message=...&channel=CHAT` with header `cf-access-authenticated-user-email: <novel-email>` creates a fresh `user_id` on first use (`auth.py:176-218`, header always wins regardless of `gateway_auth_enabled`), returns `{session_id, response, trace_id}`, and a matching row lands in the `route_traces` Postgres table with `channel`, `tool_iteration_count`, `tools_used`, `skills_loaded` populated.

**Note on the ticket's EVAL-suppression claim:** verified directly against `executor.py:2815-2926` — `write_capture()` and `route_traces` persistence run unconditionally regardless of channel; only `captains_log/promotion.py:255-257` (entity/fact promotion to the KG) skips `eval_mode` captures. Since AC-1/2 read `route_traces` (unaffected by channel) rather than promoted KG facts, and `channel` does not gate tool availability or prompt content (only `tools/linear.py:437` and capture/reflection stamping read `eval_mode`), the choice of CHAT vs EVAL does not change what's being measured here — but the ticket's protocol (CHAT, fresh identity) is followed as written regardless, since it's specified as construction.

### Probe sets — pre-registered before any run

Each question is asked **exactly once, ever**, under a fresh `fre1290-<arm>-<n>-<random>@internal.probe` identity (one identity per question, to keep each turn's memory context genuinely empty). 12 questions per arm: 5 entity-naming (varied phrasing), 4 entity-free factual claims, 3 reasoning-only controls — per the ticket's required composition.

**Codex review flagged two underspecified questions in the original draft** (`Is Aldi still selling their reusable produce bags?` — no market scoped; `best instant coffee substitute for a French press` — undefined comparison criterion) as having no single clean truth condition. Both are replaced below with a specific, unambiguous claim/target.

**Codex also flagged that these sets are not immutably locked by anything in the plan doc itself** — asking a question only once prevents *reuse*, not post-hoc editing of Set B after seeing Set A's results. Lock mechanism: **this plan doc, with both full sets and the bar below, is committed to the `fre-1290-web-search-tilt` branch before Set A is run.** The commit hash is the pre-registration timestamp; Set B is not edited after that commit, and the AC-1/AC-2 write-up in the PR cites it.

**Set A — baseline (AC-1), run before any code change:**
1. Which brand of olive oil is best for high-heat cooking?
2. Where can I buy a good mechanical keyboard in Berlin?
3. Is the IKEA BILLY bookcase still part of their current catalogue in Germany?
4. What's the best budget soldering iron for hobby electronics right now?
5. Which running shoe brand is best for flat feet?
6. Is tinned tuna high in mercury?
7. Does drinking coffee dehydrate you?
8. Is it true that microwaving food destroys most of its nutrients?
9. Can you get scurvy from a modern diet?
10. If a train leaves at 14:20 and travels for 2 hours 35 minutes, what time does it arrive?
11. What's 15% of 240?
12. If all bloops are razzles and all razzles are lazzles, are all bloops lazzles?

**Set B — post-change (AC-2), run after the 3 edits, never overlapping Set A or any prior FRE-1278/master probe:**
1. Which brand of pre-ground coffee is best for a moka pot?
2. Where's a reliable shop to buy vintage vinyl records in Amsterdam?
3. Is the IKEA POÄNG chair still part of their current catalogue?
4. What's the best affordable action camera for underwater diving this year?
5. Which sunscreen brand is best for sensitive skin?
6. Is it true that eating carrots significantly improves night vision?
7. Does cracking your knuckles cause arthritis?
8. Is brown rice actually healthier than white rice?
9. Can plants really grow better if you talk to them?
10. A rectangle has a perimeter of 30cm and a length of 9cm. What's its width?
11. What's 8 squared minus 15?
12. If every zorb is a fintle, and no fintle is a warble, can a zorb be a warble?

### Pre-registered bar (AC-2), stated before either run — a strict conjunction

Codex flagged the original draft's bar as a gap: an aggregate-only comparison let the entity-naming subset regress (e.g. 4/5 → 3/5) while the combined rate still passed on the strength of the entity-free questions. **All three of the following must hold** — this is one gate, not a primary bar plus an optional note:

1. **Entity-naming floor:** post-change items 1-5 reach `web_search` on at least 3/5.
2. **Entity-naming no-regression:** post-change items 1-5 rate `>=` baseline items 1-5 rate (baseline is whatever Set A actually measures; if baseline is already 3/5 or higher, condition 1's floor is not enough on its own — post-change must not be *lower*).
3. **Aggregate:** post-change web_search rate across all 9 factual questions (items 1-9) exceeds baseline's rate on its own 9 factual questions.

If any of the three fails, AC-2 is not met, full stop — no averaging across the three to declare a partial pass.

- **AC-4 (no regression):** items 10-12 in *both* arms must show `tool_iteration_count == 0` and a correct final answer (verifiable by hand — arithmetic/logic).
- **AC-3:** on Set B, `skills_loaded` (Postgres `route_traces.skills_loaded`) must be non-empty on at least one entity-naming question (items 1-5) — direct evidence the `model_decided` router selected the new skill.

**Accepted limitation (not fixable under the ticket's own constraint):** Set A and Set B use different questions because the ticket bans ever repeating a probe question — so code-change effect and question-difficulty are not perfectly separable; composition-matching (same 5/4/3 split, same phrasing variety) is the best available control, not a true randomized A/B. Recorded here so the PR doesn't overstate what a two-arm, never-repeat measurement can prove.

### Cleanup — per arm, with a wait for async consolidation, verified by identity

**Codex's most important finding:** the earlier draft's cleanup (delete right after the turn returns, verify zero rows) is not safe. `write_capture()`/`route_traces` land synchronously, but entity extraction / KG consolidation runs **in the background** (`run_in_background`, fire-and-forget) — the smoke test's own Neo4j check found *zero* `:Turn`/`:Entity` nodes about a minute after the turn, meaning a "verified clean" check done too early gives a false negative: consolidation could still create nodes *after* cleanup declares the identity clean. Revised procedure, per arm:

1. Ask all 12 questions in the arm, one fresh identity per question, recording each `{email, user_id, session_id, trace_id}`.
2. **Wait ~2 minutes** after the arm's last question before any cleanup pass (generous margin over the smoke test's ~1-minute negative result; reflection's own cadence gate — `captains_log_reflection_min_interval_seconds`, default 1800s — governs *re*-reflection on the same session, not the first reflection, so this is a latency buffer, not a cadence wait).
3. Pull `route_traces` rows for the arm's 12 `session_id`s (this is the actual measurement — do this **before** cleanup, not after, so cleanup can't accidentally remove the evidence being reported).
4. Postgres cleanup, FK order, per identity: `session_events` → `route_traces` → `api_costs` → `captains_log_captures` (Codex flagged this table was missing from the original draft) → `sessions` → `users`.
5. Neo4j cleanup, per identity: `MATCH (n) WHERE n.user_id = $uid OR n.originating_session_id = $sid DETACH DELETE n` — covers the auto-provisioned `:Person` self-node **and** any `:Turn`/`:Entity`/`:Claim` nodes consolidation created in the wait window.
6. **Verify zero** — re-run the same `MATCH ... RETURN count(n)` per identity and confirm 0, plus a Postgres existence check across all 6 tables above, before starting the next arm (or, for Set B, before closing out the ticket).
7. **Accepted residual risk (Codex's point, not fully closable):** if a probe's answer happens to reference an entity that already exists in the shared graph independent of this run (not the case today — the graph is confirmed empty going in — but possible if concurrent unrelated traffic touches the same entity during the measurement window, since this is the one shared graph, not an isolated per-run copy), consolidation's `MERGE` semantics update that pre-existing node's `mention_count`/`last_seen` in place rather than creating a fresh one, and a user_id/session_id-scoped delete cannot roll that back. Given the graph is verified empty before this run starts and Set A is fully cleaned (step 6) before Set B begins, cross-arm contamination of *this measurement's own* entities is prevented; contamination from unrelated concurrent traffic is a standing, accepted limitation of measuring against a live single shared environment, not something this protocol can engineer away.

## FRE-1278's fate

Decided from the Set B result, not before:
- If the full AC-2 conjunction (all three bar conditions above) is met: record that the tilt alone accounts for the behavior, leave FRE-1278 closed (already `Verify Failed`/reverted), state the evidence in this ticket's close.
- If not met: record the miss, and note FRE-1278's revert-analysis hypothesis (the anti-over-triggering carve-out likely reads "entity named in the question → trigger doesn't apply" backwards on a 27B model) as a candidate follow-up — filed as a **separate** ticket only if genuinely warranted, not re-implemented inside this PR. Reintroducing a previously-reverted, regression-causing prompt change is its own design/measurement cycle, not a fold-in.

## Risk tier

**Standard/Complex** — touches `orchestrator/prompts.py` (prompt construction), `tools/web.py` (tool contract read by the model every turn), and involves live writes to the shared production-equivalent Neo4j/Postgres. Codex plan-review required before implementation.
