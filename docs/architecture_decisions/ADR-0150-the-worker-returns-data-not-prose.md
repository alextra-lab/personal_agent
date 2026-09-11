# ADR-0150: The Worker Returns Data, Not Prose — a Typed Worker Registry, a Report Schema, and the Prompt That Was Never Configured

**Status:** Proposed — 2026-09-11
**Date:** 2026-09-11
**Deciders:** Owner (design direction, the type-registry observation, and the go, 2026-09-11), adr seat at Fable 5.1 (author, owner-directed model), master (the measurements in FRE-1491)
**Tags:** orchestrator, sub-agent, worker-contract, structured-output, prompt-cache, planner, expansion

---

## Context

### What the owner said, and what was measured

Two sentences from the owner on 2026-09-11 frame this ADR: *"I don't think we ever properly
configured the prompts for the subagents"* and *"I think the subagent should return data points
and let the primary write the story."* FRE-1491 records the measurements behind both. None of
them is re-derived here.

**The prompt.** Every worker receives the same 816-character system message and a 322-character
task message: the date block, `Task:`, `Output format:`, `Respond with the result only.` No
examples, no source guidance, no schema, no statement that siblings exist. `skill_index_block` is
a socket on `SubAgentSpec` that `_build_sub_agent_system_prompt` appends when present. Nothing
sets it. Measured at 0 characters on all four workers of the evidence run.

**The compliance table.** The landing call (ADR-0149 D3 move 5) gives three prose instructions.
Both workers of session `bbc0ddaa`, same run:

| Instruction | Worker 1 | Worker 2 |
|---|---|---|
| "Keep the report under 400 words" | FAIL — 3,315 words | FAIL — 4,056 words |
| "each with the source it came from" | PASS — 87 URLs | PASS — 25 URLs |
| "list what you searched for and did not find" | FAIL | FAIL |

One of three, identically. The two that failed are the two a schema enforces by construction.
The one that passed is a behaviour the model already has. Prose instructions to this worker,
thinking off, carry about a third of what is put in them.

**The combine worker.** The planner prompt mandates "2-3 tasks + 1 synthesis task"
(`expansion_controller.py:133`). That task is dispatched as an ordinary sequential worker
(`:1084`, `+1 for synthesis/recommendation task`) with `context=messages[-4:]` — the last four
conversation messages, never the sibling reports. It re-does the research from scratch. In the
evidence run it failed twice: worker 3 returned a ledger after 77 characters, worker 4 absorbed
413,326 characters and died on the context window. Then the primary synthesised over all four
results anyway, as `_build_synthesis_context` has always done. The combine worker is redundant
by construction and was the run's only fatal failure.

**The truncation that reads as completion.** Worker 1's landing call emitted exactly 8,192
tokens, the generation ceiling, and its report ends mid-sentence. The capture records
`stop_reason: completed`, `report_kind: synthesized`, `success: True`. `sub_agent.py` never
reads `finish_reason`. The client sets it (`litellm_client.py:1648`) and nothing downstream
consumes it. ADR-0149 D4's pause cannot fire on a landing it cannot see fail.

**What already works on the live backend.** Master's probes, not re-tested here:
`response_format` with `json_schema` and `strict: true`, alongside `tools` with
`tool_choice: "none"` (the D6 cache form), thinking off — valid JSON matching the schema, zero
tool calls, `finish_reason: stop`. llama.cpp compiles the schema to a grammar. A required `gaps`
array was populated unprompted with three entries. `respond()` already accepts `response_format`
(`litellm_client.py:1009`) and the local path sends it (`:1863`). `sub_agent.py` never passes
one. And the trap: `maxLength: 80` on a claim field returned an 80-character prefix cut
mid-sentence with the remainder spilled into the next array row. The grammar enforces shape and
never quality.

### The survey (owner-directed: "we want SOTA guidance")

Five research passes read primary sources on 2026-09-11. Vendor guidance is marked (V);
independent measured results are marked (I).

**Q1 — What a well-specified worker contract contains.** Anthropic (V): "an objective, an output
format, guidance on the tools and sources to use, and clear task boundaries"; workers return "a
condensed, distilled summary (often 1,000–2,000 tokens)". Open Deep Research (V, LangChain):
"provide complete standalone instructions — sub-agents can't see other agents' work". smolagents
(V): "everything that you do not pass as an argument to final_answer will be lost". Qwen Code's
built-in registry (`packages/core/src/subagents/builtin-agents.ts`, V) adds four lines the rest
of the survey lacked: "Complete only the assigned task... Do not expand the scope" (line 54);
"Do not guess when evidence is unavailable. Report uncertainty or blockers" (62–63); "Another
agent owns every other part of this review; staying inside yours is what makes the whole cover
the change" (376); and "Include code snippets only when the exact text is load-bearing... do not
recap code you merely read" (68). Ours has the objective and a round budget. It lacks source
guidance, the isolation statement, the sibling statement, and a rule for what not to carry back.

**Q2 — Structured or prose.** The harnesses whose workers run a multi-round tool loop before
reporting — Open Deep Research, smolagents, Qwen Code, Tongyi DeepResearch — all chose
**template-shaped prose with named sections**. Their stated reason is recall: Open Deep
Research's compress step demands findings be kept "verbatim... don't summarize it, don't
paraphrase it", because a fixed schema forces premature relevance judgement. CrewAI and Google
ADK offer a schema as opt-in. ADK (V) warns that `output_schema` together with tools in one
request "is only supported by specific models"; our landing call is tools-off by D6, so that
caveat does not bind. Tongyi DeepResearch (V) is the instructive split: a strict-JSON extractor
(`rational` / `evidence` / `summary`) per fetched page, prose inside `<answer>` tags at the end.
Structure where the input is one document; freedom where the model must synthesise.

**Q3 — Effort scaling.** Hard caps everywhere (Qwen-Agent 20 calls, DeepResearch 100 calls and
150 minutes, CrewAI `max_iter` 20). Anthropic (V) tiers by query shape: 1 agent at 3–10 calls,
2–4 agents at 10–15, 10+ for broad research. Open Deep Research tiers inside the worker prompt
("Simple queries: 2-3 search tool calls maximum. Complex queries: up to 5") and adds a self-stop
rule: "Your last 2 searches returned similar information". Claude Code's and Qwen Code's
`Explore` agent takes a **named thoroughness level from the caller** — "quick", "medium", "very
thorough" — and interprets it. No harness computes a budget from a complexity score.

**Q4 — Sibling duplication.** Two mechanisms exist. Isolation by partition: the planner writes
standalone, non-overlapping briefs, and the worker is told siblings exist (Anthropic, Open Deep
Research, Qwen Code line 376). Central loop detection over a shared transcript (Magentic-One's
`is_in_loop` ledger field). Only the first fits a parallel fan-out.

**Q5 — Reporting what was not found.** No harness carries a negative-finding field. The one
public rule is Qwen Code line 384: "If you found nothing, say so AND say what you examined — a
report that names nothing you read is indistinguishable from never having read anything." Our
`gaps` array is that rule as a required field. Tongyi DeepResearch's context-overflow message
goes the other way — "provide what you consider the most likely answer" — which is the FRE-1118
failure written into a vendor harness, and is not copied.

**The counter-hypothesis, resolved by evidence.** Tam et al. 2024 (I, arXiv 2408.02442)
measured 25–63 point drops on GSM8K under JSON mode across GPT-3.5, Claude 3 Haiku and Llama 3 8B.
Two replications overturn the generalisation: dottxt's "Say What You Mean" (V for Outlines, with
its own measurement) and the independent JSONSchemaBench (I, Geng et al. 2025, arXiv 2501.10868)
held the prompt constant and placed a `reasoning` field first in the schema; constrained decoding
then **matched or beat** free generation on the same tasks (JSONSchemaBench: GSM8K 83.8% vs
80.1%, Last-Letter 54.0% vs 50.7%). CRANE (I, arXiv 2502.09061) and "Thinking Before
Constraining" (I, arXiv 2601.07525, up to +27 points recovered) locate the damage precisely: the
grammar forces the model to commit before it reasons. Tam's own two-stage NL-to-Format regime
recovers to within 0–7 points of free generation. The design rule that follows: **give the model
free-text room before the constrained fields**, in the same call. Two mechanical facts bind the
schema: llama.cpp compiles `maxLength` to a hard character repetition and a value near 2,000
breaks the grammar outright (ggml-org/llama.cpp #25746); and the model never knows its token
budget, so `max_tokens` must exceed the schema's worst case or the JSON is cut and invalid
(vLLM #8350, Outlines #1173). Nothing in the literature tested a ~30B-active MoE with thinking
off under GBNF; that is the measurement AC-2 commits to.

### The owner's observation that shaped D2

Reading Qwen Code's registry, the owner noted: *"Interestingly, they use different sub agent
types."* A type there is a bundle: a description the parent reads to pick it, a system prompt, a
closed tool list, and optionally an executor. The parent picks the type and writes the brief. Our
harness has one type, so the role, the tools and the report shape are re-invented by the planner
on every turn, by a local model, with no examples. That is where "the prompts were never
configured" comes from: there is no place to configure them. The comment block at lines 288–337
of that file also records why a closed tool list matters — 21,178 prompt tokens of tool
declarations per turn cut to 3,447 by closing it — and why a worker must not widen its list at
runtime, which is FRE-1389's rule.

### Constraints carried in

- **No limit changes** (owner directive 2026-09-10; ADR-0149 AC-8; FRE-1487 AC-6). The study
  owns every number. This ADR ships mechanisms whose initial constants equal today's values.
- **The system prompt is byte-identical across the workers that share a prefix** (ADR-0149
  AC-4a). D2 re-scopes "share a prefix" to "share a type", for a reason D6 already established.
- **The worker is a bounded function** (FRE-1389). It reports a gap; it never acquires a tool.
- **Worker findings are not citable** under ADR-0138 D2 until the shared source registry lands
  (ADR-0149 T4, FRE-1486). D1's `source_url` per finding is the handle that follow-on needs.

---

## Decision

### D1 — The worker returns a schema, not prose

A typed worker's landing call — every path that today reaches `_forced_synthesis`, and the
completed path when the type declares a schema — carries `response_format` of type
`json_schema`, `strict: true`, alongside the retained tools with `tool_choice: "none"` (D6 form
unchanged). The schema is `worker_report_v1`:

```
{
  "working_notes":  string   ≤ 1,500 chars   — free text; what was found, what conflicts, what is uncertain
  "findings": [ ≤ 30 items, each:
      "claim":           string ≤ 500 chars   — one fact, self-contained
      "source_url":      string ≤ 300 chars
      "date_or_period":  string ≤ 80 chars    — "" when the claim is not time-bound
      "why_it_matters":  string ≤ 200 chars   — the worker's relevance judgement for the task
  ],
  "gaps": [ ≤ 15 items, each:
      "looked_for":      string ≤ 200 chars   — what the task asked for that was not found
      "where":           string ≤ 300 chars   — the queries or sources tried for it
  ]
}
```

All three top-level keys are required. `additionalProperties: false` on every object.
`findings` and `gaps` may be empty arrays: a worker that found everything has no gaps, and one
that found nothing has no findings, and both are true reports. What a required key removes is
the option to omit the section, which is the instruction both workers ignored.

**Why these fields and no others.** `working_notes` comes first and is free text: it is the
reasoning room the evidence demands, in one call, with the cached prefix. It is bounded because
it is not the report. `why_it_matters` stays per finding: the worker read 241,525 characters the
primary never sees, and its relevance judgement is the one thing that cannot be recovered
downstream. There is no `summary` or `key_findings` prose field — that reintroduces what this
removes. There is no `searches_run` list — the ledger already records every tool call
deterministically, and a model's account of its own actions is not evidence. The section list
mirrors Qwen Code line 67 and smolagents' three-part template: result with evidence, what is
missing, and the notes.

**The length limits are runaway guards, not targets.** Every limit is under 2,000 characters
(the llama.cpp grammar bound). The task message states them in words so the model plans for
them. The worst-case serialised report under these limits is about 34,000 characters (~8,500
tokens); the implementing ticket sets the landing call's `max_tokens` above the worst case it
measures with a full-fill probe, and records the ceiling actually in effect for the local
deployment (the 8,192 observed on 2026-09-11 is not explained by `model_roles.yaml`'s
`max_tokens: 2048` for the role, and the ticket resolves which value binds).

**Validity is deterministic.** A typed landing is `report_kind = synthesized` only when
`finish_reason == "stop"`, the content parses as JSON, and it validates against the declared
schema. `finish_reason == "length"` is a cut report: `report_kind = narration`, the partial
followed by the ledger, `success = False`. A parse or validation failure on a `stop` finish is a
configuration defect (the grammar makes it impossible on llama.cpp; a cloud provider that
ignores `strict` can produce it): `report_kind = ledger` with the reason naming the provider and
the validation error, `success = False`, no retry. `SubAgentResult` and `SubAgentCapture` gain
`report_schema: str | None` (the schema name, `None` for an untyped worker) and `finish_reason`
per terminal call. `SubAgentResult` gains `report: WorkerReport | None`, the parsed frozen model.

**What crosses to the primary.** `summary` holds a deterministic markdown rendering of the
report, not the JSON: the findings as a list of `claim — why_it_matters [source_url] (date)`,
then `Not found:` with each gap and where it was looked for, then the notes. `full_output` holds
the JSON. The primary writes the narrative from data it can read; the capture holds the data.

### D2 — A typed worker registry, closed, with two types

`WorkerType` is an enum. Each entry declares five things:

| Field | `researcher` | `general` |
|---|---|---|
| description (rendered into the planner prompt) | Finds facts on the open web for one bounded question and reports them as data with sources and gaps | Answers a bounded question from its own knowledge or with the tools it is granted, and reports in text |
| prompt block (appended to the base system prompt) | Source guidance, the not-carry-back rule, the negative-finding rule, the self-stop rule (D5) | none |
| tools (closed list, still filtered by governance) | `web_search` | the task's requested tools, filtered as today |
| report schema | `worker_report_v1` | `None` (text, today's path) |
| default thoroughness | `standard` | `quick` |

The planner picks a type per task. The plan schema replaces the free-text `expected_output` and
the per-task `tools` list with `type` and `thoroughness`. The planner prompt renders the registry
live — name and description per type, and the thoroughness levels — the way it already renders
the tool surface (FRE-1389 AC-1). `_validate_plan_json` rejects a task whose type is not in the
enum, which invalidates the plan and reaches the fallback planner. The fallback planner assigns
`researcher` when `web_search` is grantable in the current mode and `general` otherwise.

**Governance is unchanged.** A type's tool list is a *request*, filtered by
`_compute_sub_agent_grants` against `config/governance/tools.yaml` as every request is today. A
type cannot grant what governance refuses; a denied tool still lands in `denied_tools`.

**Cache.** The prefix a worker shares is the base system prompt plus the type block plus the
tools array, because Qwen chat templates render the tools inside the system region (ADR-0149
D6). Today AC-4a protects the prompt bytes while the tools vary per task, so two workers of one
fan-out share a prefix only when their grants happen to match. Under a type, every worker of one
type has identical prompt bytes and identical tool bytes, across the fan-out and across turns.
AC-4a is re-scoped: identical across the workers of one **type**. Two types in one fan-out are
two prefixes, and that is stated rather than hidden.

**Why two.** `researcher` is the only worker shape any measured fan-out has needed. `general`
keeps today's behaviour reachable. A `recaller` type over `search_memory` and
`recall_personal_history` is deferred (Alternatives, Option 5): no measured fan-out has needed it,
and FRE-1469 owns what a worker knows about the user.

### D3 — Thoroughness is a named level on the brief; the numbers come from the study

The planner assigns each task one of `quick | standard | thorough`. A new setting,
`sub_agent_rounds_by_thoroughness`, maps each level to a round budget. Each value is bounded
above by `sub_agent_max_tool_iterations`. **At ship, all three values equal that cap.** No
budget changes on merge; the mechanism exists so that FRE-1487's numbers have a field to land in,
and the raise remains the owner's decision on the study's evidence.

**The budget statement moves from the system prompt to the task message.** ADR-0149 move 1
placed it in the system prompt because the budget was one number. Under D3 it varies per task,
and a per-task number in the system prompt would break D2's per-type prefix. The system prompt
keeps the mechanism sentence — a round holds several tool calls, the countdown follows every
round, and the budget is stated in the task — and the task message states the number. Move 3's
countdown and move 5's forced synthesis read the task's budget, not the global cap.

The `researcher` block also carries Open Deep Research's self-stop rule in prose: stop searching
when the last two searches returned the same facts, and write the report. It is advice. The
enforcement is the budget.

### D4 — The combine worker is deleted; the primary is the synthesiser

The planner prompt's "+ 1 synthesis task" (HYBRID) and "+ 1 recommendation task" (DECOMPOSE)
rules are removed, and `_validate_plan_json` stops reserving the extra slot (`_MAX_TASKS` applies
without the `+ 1`). HYBRID is 1–3 tasks; DECOMPOSE is 2–5. No task in a plan consumes a sibling's
output, because no task can: the fan-out is one round of independent workers and one synthesis
call by the primary, which is what `_build_synthesis_context` already implements.

`_build_synthesis_context` renders each typed worker's report as D1 describes, under the header
ADR-0149 D4 already specifies (`stop`, `report`, rounds, characters). The gaps of every worker are
rendered together at the end under one heading so the primary sees the turn's absence in one
place. ADR-0149 D4's pause and trailer are unchanged: they read `success` and `report_kind`, and
D1 sets both.

### D5 — What the worker is told beyond its task

**Base system prompt** (shared by every type, byte-identical, rendered once). Keeps: the
sub-agent role line, "do not ask follow-up questions", and the `TOOL_GAP: <tool_name>` sentinel
with `(one tool name, no other text on that line)` verbatim, because `_extract_stated_tool_gap`
takes the whole remainder of the line as the name. Adds three lines, the first two from Qwen
Code's registry and master's draft:

```
Do not invent missing facts, inputs, tool results, or assumptions. When the evidence is not
there, say what you could not determine.
Complete only the assigned task. Do not broaden its scope or solve the parent task.
Only what you write in your report reaches the parent. Nothing else you read or did survives.
```

The register instruction the owner raised ("reply like a Caveman") is not adopted here. It is
an arm of the A/B in T5, because a register changes the generative distribution in a way a word
count does not, and whether it makes `claim` dense rather than clipped is a measurement.

**The `researcher` type block** (appended to the base prompt for that type only):

```
You research one bounded question on the open web.
Prefer primary sources: the organiser, the venue, the official listing, the publisher. A
news article that names its source is second. An aggregator is last, and never the only source
for a claim.
Record a claim only when a fetched result states it. Quote a source's exact words only when the
wording is load-bearing. Do not recap pages you merely read.
If you find nothing for part of the task, say so and say what you searched — a report that
names nothing you looked for is indistinguishable from never having looked.
Stop searching when your last two searches returned the same facts, and write your report.
```

**The task message** (per worker, after the date block):

```
Task: {spec.task}
Other workers in this turn own: {sibling task names, or "none"}. Stay inside your task.
Thoroughness: {level}. You have a budget of {n} tool round(s).
Report format: {schema description in words, with each field's limit} | "text"
Respond with the report only.
```

The sibling line is the isolation-by-partition mechanism (Q4), stated to the worker as Qwen Code
line 376 states it. Memory in the worker's context (FRE-1469) is not decided here; that ticket
stands, and a `recaller` type is its natural home.

### D6 — A cut landing is never reported as complete

Every worker inference call records `finish_reason` from the response into the round record
(`rounds[].finish_reason` on the capture) and, for the terminal call, onto `SubAgentResult` and
`SubAgentCapture`. On the landing call, `length` yields `report_kind = narration` and
`success = False` as D1 states, for typed and untyped workers alike. On a tool round, `length`
is recorded and the loop continues; a tool call whose arguments were cut fails at dispatch as a
malformed-argument error, which the loop already absorbs and the ledger already lists. This
closes the hole ADR-0149 D4 cannot see through today, and it is the first ticket in the chain
because every quality measurement built on report content is corrupted until it lands.

---

## Alternatives Considered

### Option 1: Template-shaped prose with named sections, as the surveyed harnesses chose
**Description:** Keep the landing call free-form; instruct the three sections (findings with
sources, not found, notes) in prose, as Qwen Code line 67 and smolagents do.
**Pros:** The choice of every harness whose workers run a tool loop. No grammar, no length
limits, no truncation trap. Preserves recall of evidence verbatim.
**Cons:** It is what we have. The compliance table is the measurement: this model, thinking off,
obeyed one of three prose section instructions on both workers. The section it dropped is the
negative finding, which is the one FRE-1118 says the system cannot express. The harnesses that
chose prose run cloud models or thinking-on models, and none published a compliance rate.
**Why Rejected:** The evidence for prose is vendor practice on other models. The evidence against
it is our own measurement on this one. D1 keeps a free-text field for what prose is good at.

### Option 2: Two calls — free report, then a tools-off extraction pass
**Description:** Tam et al.'s NL-to-Format: the landing call writes prose; a second call converts
it to the schema.
**Pros:** Recovers free-generation quality to within 0–7 points in the literature. The extraction
input is small (one report) so the second call is cheap to prefill.
**Cons:** A second inference per worker, on a backend where three serialised workers already do
not fit 900 s (ADR-0149 D5). The prose report is still cut at the ceiling, so the extraction reads
a truncated input. dottxt and JSONSchemaBench show a reasoning-first field in one call achieves the
same recovery.
**Why Rejected:** Same quality, one more call. Recorded as the fallback if AC-2 fails: if the
single constrained call measures worse than prose on the study's criterion, T5 switches the
landing to this form rather than reverting to prose.

### Option 3: A planner-generated schema per task
**Description:** `expected_output` becomes a JSON schema the planner writes for each task.
**Pros:** Maximally task-shaped output.
**Cons:** A local model authoring JSON schemas is a new failure mode bought for nothing. Every
schema is compiled to a grammar on first use (latency). Nothing validates that the schema is
answerable.
**Why Rejected:** Master's assessment in FRE-1491, agreed. The type carries the schema (D2).

### Option 4: A set of named schemas the planner picks from
**Description:** Five or six schemas (findings, comparison table, timeline, ...) selected per
task.
**Pros:** More shapes than one.
**Cons:** Four schemas nobody has measured a need for. Each is a grammar, a renderer, a
validator and a test fixture. A comparison is findings with a `dimension` in the claim.
**Why Rejected:** One schema, on the type. A second schema is added when a measured fan-out needs
it, as its own decision.

### Option 5: A `recaller` type now
**Description:** A third type over `search_memory` and `recall_personal_history`, with a
schema for recalled items.
**Pros:** The obvious third role; it would make FRE-1469's memory question a type question.
**Cons:** No measured fan-out has needed it. Its schema is undefined. FRE-1469 owns what a worker
knows about the user, and ADR-0147 places memory with the planner.
**Why Rejected:** Deferred, not refused. The registry makes it a one-entry addition when its
ticket lands.

### Option 6: Keep the combine worker, feed it the sibling reports
**Description:** Dispatch the synthesis task last, with the other results in its context.
**Pros:** Offloads synthesis from the primary.
**Cons:** It is a second synthesis: the primary synthesises over all results regardless. A
worker holding two 25,000-character reports plus its own searches is the 413,326-character
overflow the run recorded. With structured findings the primary merges by construction.
**Why Rejected:** Redundant by construction. Deleting it removes a worker and its failure class.

### Option 7: Per-task numeric round budgets chosen by the planner
**Description:** The planner writes `rounds: N` per task.
**Pros:** Direct.
**Cons:** A local model choosing numbers with no measured basis. It is a limit change by another
name, decided per turn.
**Why Rejected:** A named level (D3) keeps the numbers in settings, where the study can set them
and AC-8 can guard them.

### Option 8: Central duplication detection across siblings
**Description:** Magentic-One's `is_in_loop`: the primary inspects a shared transcript each step.
**Pros:** Catches duplication at runtime.
**Cons:** Requires a shared, steppable transcript. Our fan-out is one round of independent
workers.
**Why Rejected:** Structurally incompatible with parallel dispatch. Isolation by partition with
the sibling line (D5) is the mechanism that fits.

---

## Consequences

### Positive Consequences

- The two instructions the model ignored become fields it cannot omit. Absence becomes sayable
  at the worker (FRE-1118, ADR-0148 at the recall layer; this at the research layer).
- The prompt has a place to be configured: a type. Source guidance, the not-carry-back rule and
  the negative-finding rule live on `researcher` and nowhere else.
- One worker and its failure class disappear. The evidence run's only fatal failure was the
  combine worker.
- A cut landing is visible. ADR-0149 D4's pause fires on the failure it was built for.
- Same-type workers share one prefix across a fan-out and across turns; the tools bytes are part
  of that guarantee for the first time.
- `source_url` per finding is the handle ADR-0149 T4's shared source registry needs.

### Negative Consequences

- A grammar-constrained landing call. Shape is guaranteed; quality is measured (AC-2), not
  assumed. If it measures worse, Option 2 is the recorded fallback.
- Length limits truncate rather than compress. The limits are generous and stated to the model,
  and a claim cut at its limit is detectable (AC-2's second check).
- The planner prompt changes shape (`type`, `thoroughness`; no `tools`, no `expected_output`).
  The fallback planner and its tests change with it.
- Two types in one fan-out are two prefixes. Stated, not hidden.
- ADR-0149 move 1's placement is revised: the number moves to the task message.
- Additive ES fields on the sub-agent capture template (a reversible deploy class).

### Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| Constrained output is structurally valid and semantically worse | Medium | AC-2 measures it on the study's own quality criterion against the prose baseline before the ADR is Implemented. Option 2 is the recorded fallback. |
| The landing call's `max_tokens` is below the schema's worst case and the JSON is cut | High | D1 requires a full-fill probe and a ceiling above it; D6 makes a cut visible as `narration`, never `synthesized`. |
| A cloud `sub_agent` deployment ignores `strict` and returns invalid JSON | Medium | Validation on `stop` yields `ledger` with the provider named. No retry. Same rule as ADR-0149 D6's rejection path. |
| The planner picks `general` for research tasks | Medium | AC-7 measures type selection on the study's queries. The registry descriptions are the lever; the fallback planner's rule is the floor. |
| `maxLength` near the grammar bound breaks compilation | Low | Every limit is ≤ 1,500. The fixture in AC-1 compiles the schema against the live backend once. |
| A worker names no siblings because the plan holds one task | Low | The line renders "none". The isolation statement still holds. |
| Moving the budget number to the task message loses AC-4a's protection of it | Low | AC-6 checks the task message states the level's budget; AC-5 checks the prefix is shared per type. |

---

## Implementation Notes

| Ticket | Scope | Tier | Depends on |
|---|---|---|---|
| T1 | D6: `finish_reason` on every worker call, onto the round record, `SubAgentResult`, `SubAgentCapture` and the ES template; `length` on the landing → `narration`, `success = False`. AC-3. | Tier-2 | this ADR |
| T2 | D2, D3, D4, D5: `WorkerType` registry with `researcher` and `general`; planner schema `type` + `thoroughness`; `_validate_plan_json` and the fallback planner; `sub_agent_rounds_by_thoroughness` shipped equal to the cap; budget statement moved to the task message; combine task removed; base-prompt lines, the `researcher` block, the sibling line. AC-5, AC-6, AC-7, AC-8, AC-9. | Tier-1 | T1 |
| T3 | D1: `worker_report_v1` schema and its frozen model; `response_format` on the typed landing call and completed path; validation; `report`, `report_schema` on result and capture; the deterministic rendering in `_build_synthesis_context`; the full-fill probe and the landing `max_tokens`. AC-1, AC-4. | Tier-2 | T2 |
| T4 (eval) | AC-2: the A/B on FRE-1487's query set — prose landing vs `worker_report_v1`, plus the register-instruction arm — scored on the study's quality criterion, on `channel=EVAL`. Decides whether Option 2 replaces the single call. | Tier-1 | T3, FRE-1487 |

Files touched: `orchestrator/sub_agent.py` (`_SUB_AGENT_SYSTEM_PROMPT`, `_BUDGET_BLOCK_TEMPLATE`,
`_build_sub_agent_system_prompt`, `_build_task_message`, `_forced_synthesis`, `_run_tool_loop`,
the completed path, `_ToolLoopOutcome`), `orchestrator/sub_agent_types.py` (`SubAgentSpec.worker_type`,
`.thoroughness`, `.sibling_tasks`; `SubAgentResult.report`, `.report_schema`, `.finish_reason`),
a new `orchestrator/worker_types.py` (the registry and `worker_report_v1`),
`orchestrator/expansion_types.py` (`PlanTask.type`, `.thoroughness`),
`orchestrator/expansion_controller.py` (`_build_planner_system_prompt`, `_validate_plan_json`,
`_MAX_TASKS`, `_run_dispatch`, `_build_synthesis_context`), `orchestrator/fallback_planner.py`,
`config/settings.py` (`sub_agent_rounds_by_thoroughness`), `captains_log/capture.py` and the
sub-agent capture ES template.

Not touched: `config/governance/tools.yaml` (grants are unchanged), every limit ADR-0149 AC-8
names, `llm_client/` (the `response_format` path exists).

---

## Verification / Acceptance Criteria

Each criterion states its check and how it fails. Adjudicated on FRE-1491 once T1–T4 have landed
and deployed.

**AC-1 — A typed worker's report carries its findings and its gaps as data.** *Check (seeded
fixture):* a stub tool returns results carrying `K` coined marker tokens, each with a coined
source string, and the task names one item no result contains. A stub model, on the call
carrying `response_format`, returns a report naming every marker in `findings[].claim` with its
source in `source_url`, and the absent item in `gaps[].looked_for`. Assert `report_kind ==
"synthesized"`, `report_schema == "worker_report_v1"`, `report.findings` holds all `K` markers
and sources, `report.gaps` names the absent item, and the request carried `response_format` with
`strict: true` together with the tool definitions and `tool_choice = "none"`. *Live:* on the
owner's 2026-09-11 research query, every `researcher` capture holds a validated report whose
`gaps` array is non-empty or whose `working_notes` states that nothing was missing. *Seeded
negative:* with the schema disabled, the terminal content is prose and no gap list is present —
the recorded 0-of-2. *Fails if* a typed landing returns `synthesized` without a validated report,
or the landing request carried no `response_format`.

**AC-2 — The schema does not cost quality.** *Check:* T4 runs FRE-1487's query set twice on
`channel=EVAL`, same limits, same model: prose landing (today's instruction) and
`worker_report_v1`. Each report is scored on the study's criterion — names events dated inside
the asked window, with venue and a cited source — before any timing figure is read. The schema
arm's pass rate is at least the prose arm's. *Second check:* zero `claim` values in the schema arm
have length equal to their limit (the truncation signature master's probe recorded). *Fails if*
the schema arm scores below the prose arm on the criterion, or any claim is cut at its limit.
If it fails, T4's deliverable is the switch to Option 2, and this criterion is re-run.

**AC-3 — A cut landing is never reported as complete.** *Check (seeded):* a stub client returns
`finish_reason = "length"` on the landing call with content that is a JSON prefix. Assert
`report_kind == "narration"`, `success == False`, the content is the partial followed by the
ledger, and the capture's `finish_reason == "length"`. *Live:* over every sub-agent capture
written after T1 deploys, no record carries `finish_reason == "length"` together with
`report_kind == "synthesized"`. Today's worker 1 record is the seeded negative. *Fails if* such a
record exists.

**AC-4 — The primary receives data it can read, and reads it.** *Check:* `_build_synthesis_context`
on a fixture of two typed results renders every finding with its source and every gap under one
`Not found` heading, and renders no raw JSON. *Live:* on the study's queries, the primary's answer
names at least one gap a worker reported, in the owner's words or the trailer's. *Fails if* the
synthesis context contains the JSON verbatim, or the primary's answer asserts a claim on a topic a
worker listed under gaps without saying it is unverified (the FRE-1484 confabulation shape).

**AC-5 — Same-type workers share one prefix.** *Check:* in a fan-out of two `researcher` workers on
the local backend, the second worker's first call reports `cache_read_tokens` of at least 90% of
its prompt tokens. Baseline: ADR-0149 AC-6's 4,187 of 4,191. *Seeded negative:* change the second
worker's type to `general`; the ratio falls below 50%. *Fails if* two same-type workers miss, or
the system-prompt bytes differ between them.

**AC-6 — The thoroughness level reaches the worker and binds.** *Check (seeded):* with
`sub_agent_rounds_by_thoroughness = {quick: 2, standard: 4, thorough: 5}` in test settings, a
`quick` task's message states "2 tool round(s)", the countdown after round 1 reads "1 of 2", and
the call after round 2 is the forced synthesis with `stop_reason == "cap"`. *Fails if* the worker
runs a third round, or the message states the global cap instead of the level's value.

**AC-7 — The planner picks types the registry declares, and picks `researcher` for research.**
*Check:* on FRE-1487's query set, every plan task carries a type in the enum, and every task on
a research query is `researcher`. A fixture plan with `type: "analyst"` is rejected by
`_validate_plan_json` and reaches the fallback planner, whose tasks carry `researcher` when
`web_search` is grantable. *Fails if* an unknown type reaches dispatch, or a research query's
plan carries a `general` task.

**AC-8 — No limit changed, and no combine task exists.** *Check:* `sub_agent_max_tool_iterations`,
the `sub_agent` role's `default_timeout`, `orchestrator_task_timeout_seconds` and every value of
`sub_agent_rounds_by_thoroughness` equal the cap in the merged diff. On the study's queries, no
plan holds a task named or described as synthesis, combination or recommendation, and no
worker's input context contains another worker's digest. *This is a guard, not a discriminating
criterion.* It is here because ADR-0149 AC-8 and FRE-1487 AC-6 demand it.

**AC-9 — The worker is told its siblings and its isolation.** *Mechanism check for D5.* *Check:*
in a two-task fan-out, each worker's task message names the other task and its system prompt
contains the isolation line; in a one-task fan-out the line reads "none". *Fails if* a worker's
messages name no sibling in a multi-task plan.

**Seeded negatives.** Each mechanism is disabled in turn and the named criterion must fail.

| Mechanism disabled | Criterion that must fail |
|---|---|
| `response_format` on the landing call | AC-1 (prose, no gap list) |
| `finish_reason` read on the landing | AC-3 (the cut report reads `synthesized`) |
| Deterministic rendering in the synthesis context | AC-4 (raw JSON in the context) |
| Type-scoped prompt and tools | AC-5 (cache ratio falls) |
| Per-task budget in the task message | AC-6 (global cap stated; third round runs) |
| Type validation in `_validate_plan_json` | AC-7 (an unknown type reaches dispatch) |
| Combine-task removal | AC-8 (a synthesis task appears in a plan) |
| Sibling line | AC-9 |

---

## References

- FRE-1491 — this ADR's commission; every measurement in Context; the research mandate
- FRE-1487 — the study; owner of every number D3 leaves blank; AC-2 runs on its query set
- FRE-1483 / ADR-0149 — the landing contract this ADR types; D6's cache form; AC-4a re-scoped here
- FRE-1484, FRE-1485 — ADR-0149 T2/T3, unbuilt; D6 here is what T2's pause needs to see
- FRE-1486 — the shared source registry follow-on; D1's `source_url` is its handle
- FRE-1118 / ADR-0148 — absence must be reachable and sayable; `gaps` is the worker-layer form
- FRE-1469 — a sub-agent knows nothing about the user; not decided here
- FRE-1389 — the worker is a bounded function; the closed tool list; `TOOL_GAP` parsing
- FRE-1399, FRE-1379, FRE-1387 — the cap path, the killed path, the digest cap
- ADR-0138 — the citation contract; why worker findings are not yet citable
- ADR-0147 — memory belongs to the planner
- ADR-0028 — skills as Tier 2
- `orchestrator/sub_agent.py:119-158` (prompt constants), `:340-362` (`_extract_stated_tool_gap`), `:593-648` (prompt and task assembly), `:931-1081` (`_forced_synthesis`), `:1258-1275` (completed path)
- `orchestrator/sub_agent_types.py:46-126`, `:129-246`
- `orchestrator/expansion_controller.py:88-139` (planner prompt), `:690-711` (spec construction), `:1006-1056` (`_build_synthesis_context`), `:1060-1120` (`_validate_plan_json`)
- `llm_client/litellm_client.py:1009`, `:1216`, `:1648`, `:1863`
- `llm_client/factory.py:124`, `config/model_roles.yaml:81-84`
- Anthropic, "How we built our multi-agent research system" (2025) — the four-part brief, effort tiers, the duplication failure
- Anthropic, "Effective context engineering for AI agents" (2025) — the condensed return
- OpenAI, Structured Outputs guide — the `json_schema` / `strict` contract this ADR adopts
- llama.cpp `grammars/README.md`; ggml-org/llama.cpp #25746 (`maxLength` bound); vLLM #8350, dottxt-ai/outlines #1173 (budget-unaware truncation)
- LangChain Open Deep Research `prompts.py` — standalone briefs, complexity tiers, self-stop, the verbatim compress step
- Hugging Face smolagents `toolcalling_agent.yaml` — the three-part managed-agent template
- QwenLM/qwen-code `packages/core/src/subagents/builtin-agents.ts` — lines 54, 62–63, 67, 68, 73, 288–337, 346–384
- Alibaba-NLP/DeepResearch `inference/prompt.py`, `react_agent.py`; Tongyi DeepResearch technical report (arXiv 2510.24701) — the extractor JSON, Heavy-mode synthesis, the overflow nudge
- Microsoft Magentic-One (arXiv 2411.04468) — the task and progress ledgers
- Tam et al., "Let Me Speak Freely?" (arXiv 2408.02442); dottxt, "Say What You Mean" (2024); Geng et al., JSONSchemaBench (arXiv 2501.10868); CRANE (arXiv 2502.09061); "Thinking Before Constraining" (arXiv 2601.07525)
- Evidence session `bbc0ddaa-02fc-4414-b9bc-4223f7ca07bf`, 2026-09-11 12:15–13:00 UTC

---

## Status Updates

### 2026-09-11 — Proposed
**Changed By:** adr seat (Fable 5.1), owner-directed
**Reason:** Written after the survey and three discussion rounds with the owner, who set the
direction on all four open questions: the reasoning field in one constrained call, the typed
registry with two types, the deletion of the combine worker, and the thoroughness level whose
numbers the study supplies.
