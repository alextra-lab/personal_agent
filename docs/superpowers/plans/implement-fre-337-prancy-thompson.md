# FRE-337 — Skill Nudge Injection

**Status:** Approved · Tier-1 (Opus design + Sonnet impl) · Wave G
**Linear:** [FRE-337](https://linear.app/frenchforest/issue/FRE-337)
**Branch:** `starry-plaza-1s/fre-337-skill-nudge-injection-per-skill-behavioral-directives-to`
**Related:** ADR-0066 (skill routing defaults), FRE-334 (expanded3 baseline), FRE-226 (self-updating skills), FRE-328 (missing-skill loop)

---

## Context

FRE-334's expanded3 eval (2026-05-08) showed the Haiku skill router correctly returns `[query-elasticsearch, system-metrics]` for ambiguous prompts like "The agent has been acting weird lately — can you figure out what's going on?" (recall = 1.0), yet the primary model answers from training-data priors instead of running the ES query. **Routing is fine; the model treats injected skills as optional context, not a directive.**

The fix: **two** deterministic, code-assembled directive blocks appended at the end of the skill section (immediately before the user message). Both blocks are wrapped in semantic XML tags — Anthropic's prompt-engineering guidance explicitly recommends XML wrappers to separate instructions from context, and Claude attends to them more reliably than plain Markdown ([docs](https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/use-xml-tags)). We deliberately do **not** use `<system-reminder>`-style tags (no documented benefit; imitates internal scaffolding).

**Two blocks, separate triggers** (revised after second-opinion review):

- `<skill_index_directive>` — emitted whenever the compact skill index is present (always in hybrid mode). Tells the model that the index lists capabilities it can lazy-load via `read_skill(name)`, and to prefer loading over improvising on operational requests. Weaker wording: it must not push tool use on conceptual questions where no skill applies.
- `<skill_usage_directives>` — emitted only when ≥1 skill **body** is loaded. Stronger conditional-obligation wording, plus per-skill bullets sourced from a new `nudge:` field in each skill's YAML frontmatter (only when set and that body is loaded for this request).

Earlier draft omitted the index-only directive entirely; the reviewer pushed back: omitting it preserves the current failure mode where the index is decorative. Split design fixes that without bleeding strong directives into prompts where no body was loaded.

**Placement:** after all skill content, immediately before the user message. Claude weights instructions adjacent to the user turn higher than equivalent system-prompt text once a conversation has any depth. Inline-per-body (Option C) was rejected: couples docs to directives and blocks independent A/B iteration of the directive copy. Before-bodies (Option B) was rejected: decays with turn accumulation.

**Feature flag:** `AGENT_SKILL_NUDGE_ENABLED`, default `True`. Single flag toggles both XML blocks together (per the eval design — the reviewer's two-sided metric requires baseline-everything-off and treatment-everything-on). Quick disable path if regressions surface.

---

## Approach (summary)

1. Add optional `nudge: str | None` to `SkillDoc` and the frontmatter parser — single canonical site.
2. Add two helpers in `skills.py`:
   - `assemble_skill_index_directive() -> str` — returns the `<skill_index_directive>` XML block (constant text; no per-skill content).
   - `assemble_skill_usage_directives(loaded_skill_names: Sequence[str], all_skills: Mapping[str, SkillDoc]) -> str` — returns the `<skill_usage_directives>` XML block with optional per-skill bullets.
3. In `executor.py`'s skill-injection assembly, append the directive blocks *last*, after both `_skill_index` and `_keyword_block` / `_preloaded_bodies`, gated by `settings.skill_nudge_enabled`. Order within the appended tail: `<skill_index_directive>` then `<skill_usage_directives>` (so the strongest, body-specific directives sit closest to the user turn).
4. **Trigger rules:**
   - `<skill_index_directive>`: emitted whenever the compact skill index is present in the prompt (always in hybrid mode; never in pure `keyword` mode when no index is assembled).
   - `<skill_usage_directives>`: emitted only when `ctx.loaded_skills` contains ≥1 skill whose body is in the prompt (keyword-matched, router-selected, or read via `read_skill`).
   - Both blocks together: assembled only when `settings.skill_nudge_enabled=True`.
5. Seed `nudge:` content on 4 skills initially: `query-elasticsearch`, `system-metrics`, `infrastructure-health`, `self-telemetry`. Other 11 skills get the wrapper text only.
6. New ADR documenting placement, XML wrapper choice, split-block design, flag, and the FRE-226/FRE-328 gating constraint.
7. Tests: parser, assembly (both blocks), null-skill-list, hybrid index-only emits only the index directive, flag-off path, sub-agent path.
8. **Two-sided eval**: Family A ambiguous prompts (treatment metric) + Family negative-control prompts (regression-guard metric) in the same run.

---

## Files to modify

### Implementation

| File | Change |
|------|--------|
| `src/personal_agent/orchestrator/skills.py` | Add `nudge: str \| None = None` to `SkillDoc` (L49–60). Parse `fm.get("nudge")` in `_load_all_skills` (L133–142). Two new helpers near `assemble_skill_index` (~L187): `assemble_skill_index_directive() -> str` and `assemble_skill_usage_directives(loaded: Sequence[str], skills: Mapping[str, SkillDoc]) -> str`. Constants: `_SKILL_INDEX_DIRECTIVE_TAG = "skill_index_directive"`, `_SKILL_USAGE_DIRECTIVES_TAG = "skill_usage_directives"`. |
| `src/personal_agent/orchestrator/executor.py` | In the assembly block at L1504–1535 (after both keyword + hybrid branches compute `_skill_injection`), when `settings.skill_nudge_enabled`: (a) if `_skill_index` is non-empty, append `assemble_skill_index_directive()`; (b) if `ctx.loaded_skills` is non-empty, append `assemble_skill_usage_directives(ctx.loaded_skills, get_all_skills())`. Use the existing `_SEPARATOR` join. Mirror this for sub-agents at L1870–1875 / `sub_agent_types.py:50` (add `spec.skill_index_directive_block`, `spec.skill_usage_directives_block`). |
| `src/personal_agent/config/settings.py` | New field `skill_nudge_enabled: bool = Field(default=True, alias="AGENT_SKILL_NUDGE_ENABLED")` near the existing `skill_routing_*` fields (around L1046–1067). Docstring: deterministic, code-assembled directive block; baseline-disabled for eval comparison. |

### Skill frontmatter seeds

| File | Add `nudge:` |
|------|--------------|
| `docs/skills/query-elasticsearch.md` | `These results must come from a live ES query — never answer from training-data priors about what the logs might contain.` |
| `docs/skills/system-metrics.md` | `Always run the metrics command before answering. Do not estimate or approximate values.` |
| `docs/skills/infrastructure-health.md` | `Probe the live endpoints with `bash` curl before concluding a service is healthy or degraded.` |
| `docs/skills/self-telemetry.md` | `Cite real ES counts/traces in your answer — do not paraphrase what the telemetry "probably" shows.` |

Remaining 11 skills get the base wrapper line only (no per-skill directive).

### Tests

| File | Coverage |
|------|----------|
| `tests/personal_agent/orchestrator/test_skills.py` | Parser reads `nudge:` field; `None` when absent; preserves multi-line YAML strings. |
| `tests/personal_agent/orchestrator/test_skill_injection.py` | (1) Both XML blocks appended *after* skill bodies and *after* the compact index; (2) `<skill_usage_directives>` includes the wrapper text plus one bullet per loaded body with non-empty `nudge:`; (3) bodies without a `nudge:` field contribute no bullet; (4) **hybrid mode, no bodies loaded** → only `<skill_index_directive>` emitted (this is the case the second-opinion review fixed); (5) keyword mode with no index assembled and no bodies → neither block emitted; (6) flag off → neither block emitted; (7) tag names exactly `skill_index_directive` / `skill_usage_directives` (no `<system-reminder>` aliasing). |
| `tests/personal_agent/orchestrator/test_route_skills.py` | Smoke: routing path still passes nudge-eligible names through to assembly. |
| `tests/personal_agent/orchestrator/test_skill_contract.py` | Schema test: `SkillDoc.nudge` is optional, doesn't break existing fixtures. |

### Documentation

| File | Change |
|------|--------|
| `docs/architecture_decisions/ADR-0067-skill-nudge-injection.md` | New ADR (Accepted). Sections: Context (FRE-334 finding), Decision (deterministic block, after-bodies placement, hybrid rule, flag), Consequences (eval expectation, behavior overhead, FRE-226/328 gating constraint), Alternatives Considered (B / C). |
| `docs/skills/SKILL_TEMPLATE.md` | Add `nudge:` as an optional frontmatter field with one-line guidance: "Only set when behavior is custom and specific to this skill; generic skills don't need it." |
| `docs/plans/MASTER_PLAN.md` | After PR merges, move FRE-337 from "Immediately Actionable" → "Recently Completed" with PR# and date; update header `Last updated` line. |

---

## Block format (exact)

Both blocks are wrapped in semantic XML tags and join the rest of `_skill_injection` via the existing `_SEPARATOR = "\n\n---\n\n"`. Order in the appended tail (closest to the user turn last): `<skill_index_directive>` → `<skill_usage_directives>`.

### `<skill_index_directive>` — always when the compact index is present

```xml
<skill_index_directive>
The skill index lists capabilities you may load with read_skill(name).
For requests about live systems, logs, metrics, infrastructure, files, or
project-specific procedures, load the matching skill before answering.
For general knowledge questions, answer normally.
</skill_index_directive>
```

Constant text — no per-skill content. Deliberately weak wording so it does **not** push tool use on conceptual prompts where no skill applies.

### `<skill_usage_directives>` — only when ≥1 body is loaded

```xml
<skill_usage_directives>
Loaded skill bodies are actionable instructions for this turn.
If a loaded skill describes how to investigate or answer the user's request,
follow it and use its tools before giving a substantive answer.
If no loaded skill applies, answer normally.
If an indexed skill appears necessary but was not loaded, call read_skill(name)
before improvising.
Do not speculate about live system state when an available skill can check it.

- query-elasticsearch: These results must come from a live ES query — never
  answer from training-data priors about what the logs might contain.
- system-metrics: Always run the metrics command before answering. Do not
  estimate or approximate values.
</skill_usage_directives>
```

- Wrapper paragraph is always present in this block.
- One bullet per loaded body whose `SkillDoc.nudge` is non-empty. Bodies without a `nudge:` field contribute no bullet.
- If zero loaded bodies have `nudge:`, the wrapper paragraph still emits — the directive itself is the value, even without per-skill specialisations.
- Conditional-obligation phrasing ("If … follow it and use its tools") with explicit escape hatch ("If no loaded skill applies, answer normally") was chosen over a flat "prefer executing" — the reviewer flagged the latter as too easily treated as optional by Claude.

---

## Reused existing code

| Reuse | Path |
|-------|------|
| `SkillDoc` dataclass | `src/personal_agent/orchestrator/skills.py:49–60` |
| `_parse_frontmatter`, `_load_all_skills`, mtime cache | `skills.py:76–144` |
| `get_all_skills()` accessor | `skills.py:292` |
| `_SEPARATOR` join | `skills.py:35` |
| `if _skill_injection:` empty-skip guard | `executor.py:1531` (extend, don't duplicate) |
| `ctx.loaded_skills` (already tracked per request) | populated in `executor.py:1487` |
| `spec.skill_index_block` plumbing for sub-agents | `sub_agent_types.py:50`; mirror with `skill_nudge_block` |

---

## Eval plan (two-sided: 5 treatment prompts + ≥10 control prompts × 2 cells)

The reviewer flagged `n=5 × 2` as insufficient: it measures lift on the failure mode but not regression on prompts where tool use would be wrong. Eval is therefore two-sided — same prompt set runs in both cells.

**Cells:**

| Cell | Routing mode | `AGENT_SKILL_NUDGE_ENABLED` | Baseline source |
|------|--------------|-----------------------------|-----------------|
| `cloud-model-decided-nudge-off` | `model_decided` | `false` | Reuse `EVAL-skill-routing-2026-05/cloud-model-decided-2026-05-08-expanded3/` for the Family A prompts if config-parity holds; otherwise rerun. Control prompts re-run fresh. |
| `cloud-model-decided-nudge-on` | `model_decided` | `true` | New run. |

**Treatment prompts (5 Family A ambiguous, from `telemetry/evaluation/EVAL-skill-routing-2026-05/prompts.yaml`):**

- `ambiguous_acting_weird`
- `ambiguous_what_went_wrong`
- `ambiguous_recent_failures`
- `ambiguous_backend_health`
- `ambiguous_something_slow`

**Control prompts (≥10 should-not-tool, mixed sources):** start from any `negative-control` / `conceptual` / `no_skill_needed` prompts already tagged in `prompts.yaml`; if fewer than 10 exist, add new ones in the same file before running. Mix should include: (a) general knowledge ("What does an ORM do?"), (b) chit-chat ("How are you today?"), (c) prompts that pattern-match skill keywords but don't need execution ("Explain what Elasticsearch is — I'm new to it"), (d) hypothetical / planning ("If we wanted to add Redis, what tradeoffs would we weigh?"). Tag set in `prompts.yaml`: `tags: [family-negative-control, should-not-tool]`.

**Run command (one prompt at a time — harness has no `--tag` filter today; add a follow-up issue):**

```bash
TREAT="ambiguous_acting_weird ambiguous_what_went_wrong ambiguous_recent_failures
       ambiguous_backend_health ambiguous_something_slow"
CONTROL="<the 10 control prompt IDs>"

for FLAG in on off; do
  CELL=cloud-model-decided-nudge-$FLAG
  for P in $TREAT $CONTROL; do
    make eval-skill-routing CELL=$CELL RUN=fre337-$FLAG-$P PROMPT=$P
  done
done
```

**Two-sided metric:**

| Side | Prompts | Metric | Pass criterion |
|------|---------|--------|----------------|
| Treatment (lift) | Family A ambiguous, router-recall ≥ 0.5 | `first_bash_command != ""` rate | Treatment ≥ baseline + **30pp absolute** (equivalent to halving the "no-bash" rate at baseline ≈ 60%). |
| Control (regression guard) | Family negative-control, should-not-tool | `first_bash_command != ""` rate | Treatment ≤ baseline + **5pp absolute**. No semantic regressions (spot-check 3 control transcripts manually). |

If treatment passes but control fails, the wrapper text is too strong → iterate on wording, re-run. If both fail, escalate to a design review before merging.

Persist `COMPARISON.md` alongside the cells under `telemetry/evaluation/EVAL-skill-routing-2026-05/fre337-2026-05-13/` with: per-prompt treatment vs baseline `first_bash_command`, recall/precision, latency, and the two-sided summary table.

---

## Failure modes to watch (from second-opinion review)

The eval's control side is sized to catch these, but each warrants a named follow-up if observed:

1. **False-positive body load → unnecessary bash.** Router or keyword matcher loads a body for a prompt that didn't need execution (e.g. "Why are agents slow in general?" pulling `system-metrics`). With the nudge on, the model will then run the metrics. **Mitigation:** caught by the control side; precision-focused routing follow-up if recurrent.
2. **`read_skill` fishing loops.** The `<skill_index_directive>` invites lazy-loading; pathological prompts could trigger 3+ sequential `read_skill` calls before any user-visible output. **Mitigation:** instrument `read_skill` call count per turn (extend existing executor telemetry); if p95 > 2, file a follow-up to add a per-turn cap. Out-of-scope for FRE-337 implementation; in-scope to watch in eval.
3. **Unavailable-tool confusion.** Nudge says "use its tools"; if a tool is unavailable (governance-blocked, infrastructure down), Sonnet may either retry or hallucinate output. **Mitigation:** ensure tool-error surfaces produce a clear `"I attempted X but the tool is unavailable — <reason>"` reply path. Spot-check at least one trace where the tool layer returns an error.

---

## Safety boundary for FRE-226 / FRE-328

The `nudge` field lives in YAML frontmatter, parsed via `_parse_frontmatter` — the same structured path subject to skill-authorship review. When FRE-226 (self-updating skills) lands, any agent-proposed change to `nudge:` is gated by the same "Needs Approval" Linear ticket flow as the rest of the frontmatter. **The nudge is never read from a skill's freeform body, and the agent must never edit `nudge:` without a human-approved Linear ticket.** This constraint is captured explicitly in ADR-0067 §Consequences and cross-linked from FRE-226 and FRE-328 acceptance criteria.

---

## Verification

1. **Unit tests:** `make test-file FILE=tests/personal_agent/orchestrator/test_skill_injection.py` and `…/test_skills.py` — all green; the 7 new cases above pass.
2. **Type + lint:** `make mypy` and `make ruff-check` clean (no new `Any` introduced; new field is `str | None`).
3. **Local smoke:** with `AGENT_SKILL_NUDGE_ENABLED=true`, `uv run agent "What went wrong yesterday?" --new` — observe assistant issues an ES `bash` curl rather than narrating prior knowledge. Repeat with the flag false to confirm baseline behavior is preserved bit-for-bit.
4. **Telemetry check:** in Kibana / `query-elasticsearch`, filter `agent-logs-*` on `trace_id` from the smoke run; confirm two structured-log events with `loaded_skill_names`, `nudge_bullets_count`, and `index_directive_emitted`/`usage_directives_emitted` flags: `skill_index_directive_assembled` (whenever the index was present) and `skill_usage_directives_assembled` (only when ≥1 body was loaded). Traces with neither index nor bodies carry neither event.
5. **Two-sided eval:** run the 2-cell × (5 treatment + ≥10 control) matrix above; treatment lift ≥ 30pp absolute on `first_bash_command != ""`; control regression ≤ 5pp absolute; spot-check 3 control transcripts for semantic regressions; write `COMPARISON.md`.
6. **Sub-agent parity:** integration test or manual smoke that a hybrid sub-agent dispatch also receives both directive blocks when its dispatch loads bodies (`SubAgentSpec.skill_index_directive_block` and `SubAgentSpec.skill_usage_directives_block` populated).
7. **PR description** links the ADR, the eval `COMPARISON.md`, and the FRE-334 baseline run; calls out the two-sided pass criteria explicitly.

---

## Out of scope

- Tuning per-skill nudge wording beyond the 4 seeds (further iteration after eval data lands).
- Self-updating skills (FRE-226) — only the gating constraint is documented here.
- Local-profile (Qwen3.6) eval cell — cloud cells first; if treatment holds on Sonnet, mirror on the next local eval pass.
- Adding a `--tag` filter to the eval harness — note as a future ergonomic improvement; not blocking. File as a Tier-3 Haiku follow-up after FRE-337 merges.
- Per-turn `read_skill` call cap — wait for eval data before deciding scope. If p95 > 2 in the FRE-337 treatment cell, file a separate Tier-2 Sonnet ticket.
