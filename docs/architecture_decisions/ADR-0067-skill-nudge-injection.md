# ADR-0067 — Skill Nudge Injection

**Status:** Accepted
**Date:** 2026-05-13
**Author:** Claude Code (Opus/Sonnet)
**Related:** ADR-0066 (skill routing defaults), FRE-337, FRE-334

---

## Context

FRE-334's expanded3 eval (2026-05-08, 26 prompts) showed the Haiku skill router correctly returns `[query-elasticsearch, system-metrics]` for ambiguous prompts like "The agent has been acting weird lately — can you figure out what's going on?" (recall = 1.0), yet the primary model (Claude Sonnet) answers from training-data priors rather than executing a live ES query. Routing is correct; the model treats injected skill bodies as optional reference material rather than actionable directives.

---

## Decision

Inject two deterministic, code-assembled XML directive blocks at the end of the skill section in the system prompt — immediately before the user message.

### D1 — Two blocks with separate triggers

- **`<skill_index_directive>`**: emitted whenever the compact skill index is present in the prompt (always in hybrid mode, never in pure keyword mode with no index). Constant text. Weak wording intentional — must not push tool use on conceptual prompts where no skill applies.

- **`<skill_usage_directives>`**: emitted only when ≥1 skill body is in the prompt (keyword-matched, router-selected, or read via `read_skill`). Carries per-skill nudge bullets sourced from a new `nudge:` YAML frontmatter field.

### D2 — XML wrapper tags, not Markdown headers

Tags: `<skill_index_directive>` and `<skill_usage_directives>`. Anthropic's prompt-engineering documentation recommends XML tags to separate instructions from context, and Claude attends to them more reliably than Markdown structure. We do **not** use `<system-reminder>` — no documented advantage and it imitates internal scaffolding.

### D3 — Placement: after all skill content, before the user message

Claude weights instructions adjacent to the user turn substantially higher than equivalent system-prompt text once a conversation has any depth. Inline-per-body (coupling docs to directives) and before-bodies (decays with turn accumulation) were considered and rejected.

### D4 — Per-skill `nudge:` frontmatter field

Optional field in skill YAML frontmatter. Parsed by `_parse_frontmatter` → `_load_all_skills` at `src/personal_agent/orchestrator/skills.py`. Set only when the behavior is custom and specific to that skill (e.g. query-elasticsearch, system-metrics, infrastructure-health, self-telemetry). Generic skills (bash, list-directory) carry no nudge.

### D5 — Feature flag

`AGENT_SKILL_NUDGE_ENABLED` (default `True`). Single flag toggles both blocks. Enables A/B eval against a baseline-off cell and a quick disable path if regressions surface.

### D6 — Safety gate for FRE-226 / FRE-328

The `nudge:` field lives in YAML frontmatter — the same structured, human-reviewed path as all other frontmatter. When self-updating skills land (FRE-226), any agent-proposed change to `nudge:` is gated by the same "Needs Approval" Linear ticket flow. The nudge value is **never** read from the skill body and **never** generated in-context by the LLM.

---

## Consequences

- **Expected behavioral change**: Family A "no-bash" rate on ambiguous operational prompts should drop substantially (target ≥ 30pp absolute lift on `first_bash_command != ""` rate vs baseline).
- **Regression guard**: control cell (≥10 should-not-tool prompts) must show ≤ 5pp increase in `first_bash_command != ""` rate vs baseline. Wrapper wording uses explicit escape hatch ("If no loaded skill applies, answer normally") to protect conceptual prompts.
- **False-positive body load → unnecessary bash**: if routing loads a body for a prompt that didn't need execution, the nudge will reinforce the wrong body. Mitigation: routing precision is the correct fix; the nudge itself can only fire when the router/keyword matcher already chose a body.
- **`read_skill` fishing loops**: `<skill_index_directive>` invites lazy-loading; watch for p95 > 2 `read_skill` calls per turn in the eval treatment cell. Follow-up ticket if observed.
- **Unavailable-tool confusion**: model may retry or hallucinate when nudged toward a tool that's unavailable. Watch for this in eval transcripts; ensure graceful error paths already present in the tool layer are exercised.

---

## Alternatives Considered

- **Placement before skill bodies**: rejected — decays with turn accumulation, weakest at multi-turn turn N where the failure mode most recurs.
- **Nudge inline per body** (append directive text to each body): rejected — couples documentation to directive, blocks independent A/B iteration, bloats skill files.
- **System-prompt-level nudge** (no XML, plain Markdown): rejected — XML tags reliably separate instruction from context per Anthropic engineering guidance.
- **Omitting index-only directive in hybrid mode**: initially proposed, rejected after review — the index remains decorative without any directive to lazy-load matching skills before answering. The weaker `<skill_index_directive>` text fills this gap without over-instructing.
