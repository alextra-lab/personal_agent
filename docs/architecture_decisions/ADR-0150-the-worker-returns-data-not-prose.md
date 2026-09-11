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
and let the primary write the story."* FRE-1491 records the measurements behind both. They come
from session `bbc0ddaa` and its sub-agent captures, not from the repository, and this ADR cites
them as FRE-1491's rather than re-deriving them.

**The prompt.** Every worker receives the same 816-character system message and a 322-character
task message: the date block, `Task:`, `Output format:`, `Respond with the result only.` No
examples, no source guidance, no schema, no statement that siblings exist. `skill_index_block` is
a socket on `SubAgentSpec` (`sub_agent_types.py:122`) that `_build_sub_agent_system_prompt`
appends when present (`sub_agent.py:615`). Nothing in the orchestrator sets it. Measured at 0
characters on all four workers of the evidence run.

**The compliance table.** The landing call (ADR-0149 D3 move 5) gives three prose instructions.
Both workers, same run:

| Instruction | Worker 1 | Worker 2 |
|---|---|---|
| "Keep the report under 400 words" | FAIL — 3,315 words | FAIL — 4,056 words |
| "each with the source it came from" | PASS — 87 URLs | PASS — 25 URLs |
| "list what you searched for and did not find" | FAIL | FAIL |

One of three, identically. The two that failed are the two a schema enforces by construction.
The one that passed is a behaviour the model already has. Prose instructions to this worker,
thinking off, carry about a third of what is put in them.

**The combine worker.** The planner prompt mandates "2-3 tasks + 1 synthesis task"
(`expansion_controller.py:133`), and `_validate_plan_json` reserves the slot (`:1084`, `+1 for
synthesis/recommendation task`). The dispatch loop (`:734-770`) then runs that task as an
ordinary sequential worker with `context=messages[-4:]` (`:692`) — the last four conversation
messages, never the sibling reports. It re-does the research from scratch. In the evidence run
it failed twice: worker 3 returned a ledger after 77 characters, worker 4 absorbed 413,326
characters and died on the context window. Then the primary synthesised over all four results
anyway, as `_build_synthesis_context` (`:1006`) has always done. The combine worker is redundant
by construction and was the run's only fatal failure.

**The truncation that reads as completion.** Worker 1's landing call emitted exactly 8,192
tokens and its report ends mid-sentence. The capture records `stop_reason: completed`,
`report_kind: synthesized`, `success: True`. `sub_agent.py` never reads `finish_reason`. The
client sets it (`litellm_client.py:1648`) and nothing downstream consumes it. ADR-0149 D4's
pause cannot fire on a landing it cannot see fail. Which ceiling produced 8,192 is not settled:
`model_roles.yaml:84` declares `max_tokens: 2048` for the role, `factory.py:124` passes the
deployment's own ceiling on the local path, and neither is 8,192.

**What already works on the live backend.** Master's probes, not re-tested here:
`response_format` with `json_schema` and `strict: true`, alongside `tools` with
`tool_choice: "none"` (the D6 cache form), thinking off — valid JSON matching the schema, zero
tool calls, `finish_reason: stop`. llama.cpp compiles the schema to a grammar. A required `gaps`
array was populated unprompted with three entries. `respond()` already accepts `response_format`
(`litellm_client.py:1009`) and both dispatch paths send it (`:1216`, `:1863`). `sub_agent.py`
never passes one. And the trap: `maxLength: 80` on a claim field returned an 80-character prefix
cut mid-sentence with the remainder spilled into the next array row. The grammar enforces shape
and never quality.

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
budget, so the generation ceiling must exceed the schema's worst case or the JSON is cut and
invalid (vLLM #8350, Outlines #1173). Nothing in the literature tested a ~30B-active MoE with
thinking off under GBNF; that is the measurement AC-2 commits to.

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

- **No limit changes** (owner directive 2026-09-10: *"We dont raise limits until we have
  properly implemented the behavior when the limits are reached"*; ADR-0149 AC-8; FRE-1487 AC-6).
  This ADR raises nothing. Where a new mechanism needs a number, the number ships equal to
  today's value (D3), or the mechanism is sized to fit under today's value (D1's schema under
  the landing ceiling), and the at-limit behaviour ships first (D6, T1).
- **The worker is a bounded function** (FRE-1389). It reports a gap; it never acquires a tool.
- **Worker findings are not citable** under ADR-0138 D2 until the shared source registry lands
  (ADR-0149 T4, FRE-1486). D1's `source_url` per finding is the handle that follow-on needs.
- **ADR-0149 is Accepted and stays so.** Two of its clauses are revised here, and the revision
  is named as such in the section "Revisions to ADR-0149" rather than left implicit.

---

## Decision

### D1 — The worker returns a schema, not prose

**Two kinds of worker.** A type whose registry entry names a report schema is
**schema-backed** (`researcher`). A type whose entry names none is **text-reporting**
(`general`), and keeps today's contract on every path.

**When the constrained call happens.** A schema-backed worker's report is written by exactly one
dedicated report-writing call, and that call is constrained when the dialect accepts the schema
(the table below) and unconstrained otherwise. ADR-0149 already makes that call on the cap, the
time reserve, and the per-call timeout with time left (`sub_agent.py:1165`, `:1196`, `:1237`).
The voluntary stop — the model replies with no tool calls (`:1258`) — today returns that reply as
the report. For a schema-backed worker it no longer does: the reply's content, if any, is appended
to the transcript as an assistant message, and the loop makes the same report-writing call with
`stop_reason = "completed"`. The worker cannot be asked to emit JSON on a call that might also
emit tool calls, because whether a call is the landing is known only after it returns; and
constraining the tool rounds would forbid the tool-call syntax. So the landing is always its own
call, tools retained, `tool_choice: "none"`, prefix cached (D6 form). The paths on which ADR-0149
makes no call — the outer deadline, cancellation, an upstream error, a timeout with no time left
— are unchanged: the ledger, with no report-writing call. The `researcher` block tells the worker
to end its search with a reply of the single word `DONE` and no tool calls; if it writes prose
instead, that prose stays in the transcript as notes and costs generation time, and the landing
still runs.

**The schema, `worker_report_v1`.** Every object sets `additionalProperties: false`. Every
property of every object is required — the OpenAI strict subset demands it, and the llama.cpp
converter honours it. `minLength` and `maxLength` are in both subsets for standard models. A
field marked `may be empty` is the only kind that may hold `""`.

```
{
  "working_notes":  string   maxLength 1000, may be empty  — free text first: found, conflicting, uncertain
  "findings": array maxItems 20, items:
      "claim":           string minLength 1, maxLength 400  — one fact, self-contained
      "source_url":      string minLength 1, maxLength 200
      "date_or_period":  string maxLength 60, may be empty   — "" when the claim is not time-bound
      "why_it_matters":  string minLength 1, maxLength 150  — the worker's relevance judgement for the task
  "gaps": array maxItems 10, items:
      "looked_for":      string minLength 1, maxLength 150  — what the task asked for that was not found
      "where":           string minLength 1, maxLength 200  — the queries or sources tried for it
}
```

The grammar enforces `minLength`; a deterministic check after validation strips whitespace from
every `minLength 1` field and treats a whitespace-only value as a validation failure, so a
blank row cannot pass as content on a dialect whose grammar is lax.

Key order is part of the contract: `working_notes` is emitted first so the model writes before
it commits, which is the reasoning room the evidence demands, in one call. `why_it_matters` stays
per finding: the worker read 241,525 characters the primary never sees, and its relevance
judgement is the one thing that cannot be recovered downstream. There is no `summary` or
`key_findings` prose field — that reintroduces what this removes. There is no `searches_run`
list — the ledger already records every tool call deterministically, and a model's account of
its own actions is not evidence. The section list mirrors Qwen Code line 67 and smolagents'
three-part template: result with evidence, what is missing, and the notes.

**The limits fit under the ceiling that exists; the ceiling is not raised.** Every `maxLength`
is under 2,000 characters (the llama.cpp grammar bound). The character budget under these limits
is about 22,500 before JSON escaping: 20 findings × 810 characters of values plus about 66 of
keys each, 10 gaps × 350 plus about 30 each, 1,000 of notes, and the envelope. Characters do not
bound tokens — escaping doubles a quote, and a non-ASCII character can cost several tokens — so
"worst case" is **defined** by a procedure, not a formula: the **full-fill probe** builds a report
with every array at `maxItems` and every string at `maxLength`, filled with English prose that
contains one escaped quote per 100 characters, and counts its tokens on the tokenizer of each
dialect declared `True` in the table below. That count, plus a 25% margin, is the worst case.
T3 records the landing ceiling actually in effect for each such dialect (the unexplained 8,192
above is resolved there). The rule is: the worst case must be at most 90% of the ceiling. If it
is not, T3 **shrinks the limits in this order** until it is: `findings.maxItems` 20 → 15 → 10,
then `claim.maxLength` 400 → 300 → 200, then `working_notes.maxLength` 1000 → 600. If the
report does not fit at the floor of that order (10 findings, 200-character claims, 600-character
notes), the ceiling is too small for a data report, and T3 reports that to the owner as a finding
on FRE-1491 rather than resolving it by a raise. The ceiling is a limit and this ADR does not
raise it; AC-8 names it. The task message states the limits in words so the model plans for
them.

**Validity is deterministic, and every finish reason has a disposition.**

| Landing outcome | `report_kind` | `success` | Note |
|---|---|---|---|
| `finish_reason == "stop"`, content parses, validates, and holds at least one finding or one gap | `synthesized` | `True` only when `stop_reason == "completed"` (ADR-0149 rule) | The report |
| `finish_reason == "stop"`, valid, but `findings` and `gaps` both empty | `ledger` | `False` | `{why}` = "the model reported no findings and no gaps". ADR-0149's "empty content is not a report", at the schema level. |
| `finish_reason == "length"` | `narration` | `False` | The partial JSON followed by the ledger. The cut is visible. This row is D6's and belongs to T1; it applies to every report-writing call, schema-backed or text-reporting, and is checked before the content is read. |
| `finish_reason == "stop"`, content empty, or fails to parse or validate | `ledger` | `False` | `{why}` quotes the first 200 characters of the content, or the provider's refusal when the client surfaces one. On llama.cpp the grammar makes this path unreachable; on a cloud dialect it is a refusal or a provider ignoring `strict`, and the WARNING names the provider. No retry. |
| any other `finish_reason`, or `None` | `ledger` | `False` | `{why}` names the finish reason. |

The client does not surface a provider refusal today (`LLMResponse` has no such field). T3 adds
`refusal: NotRequired[str | None]` to `LLMResponse`, filled from the provider message where one
exists, so the fourth row can quote it.

**Which dialects get the constrained landing.** Following ADR-0149 D6's pattern, a table
`LANDING_ACCEPTS_JSON_SCHEMA: Mapping[Dialect, bool]` in `llm_client/models.py`, read through
`landing_accepts_json_schema(dialect: Dialect | None) -> bool`. `LLAMACPP_QWEN: True` from
master's probe. `OPENAI_GPT5: True` from the documented strict contract. `OVH_QWEN: True`
provisionally — the console lists `json_schema` among its output formats (`models.yaml` comment
at the OVH entry) — and T3 verifies it with one probe before the value is trusted.
`ANTHROPIC_ADAPTIVE` and `ANTHROPIC_BUDGET: False` until probed: Anthropic's structured outputs
use `output_config.format`, and whether litellm translates `response_format` for that dialect is
not known. `None` returns `False` and logs `landing_dialect_unresolved`. A `False` dialect gets
today's prose landing with `report_schema = None` and a WARNING `structured_landing_unsupported_declared`
on every such call. Fine-tuned OpenAI deployments, which do not support `maxLength`, are out of
scope: no such deployment is in the catalog.

**What crosses to the primary.** `SubAgentResult` gains `report: WorkerReport | None`, the parsed
frozen model (`None` on every row of the table but the first), and `report_schema: str | None`.
On the first row, `summary` holds a deterministic markdown rendering of the whole report —
findings as `claim — why_it_matters [source_url] (date)`, then `Not found:` with each gap and
where it was looked for, then the notes — so ADR-0149 D4's `stop_and_show` and the capture digest
are complete per worker, and `full_output` holds the validated JSON. On every other row,
`summary` and `full_output` hold what ADR-0149 assigns to that `report_kind`: the partial plus
the ledger, or the ledger. The synthesis context (D4) renders from `report` when it is present,
and lays the gaps out once.

### D2 — A typed worker registry, closed, with two types

`WorkerType` is an enum. Each entry declares five things, and the planner chooses none of them:

| Field | `researcher` | `general` |
|---|---|---|
| description (rendered into the planner prompt) | Finds facts on the open web for one bounded question and reports them as data with sources and gaps | Answers a bounded question from its own knowledge, a computation, or the user's own memory, and reports in text |
| prompt block (appended to the base system prompt) | Source guidance, the not-carry-back rule, the negative-finding rule, the self-stop rule, the `DONE` rule (D5) | none |
| tools (closed list, still filtered by governance) | `web_search` | `run_python`, `search_memory`, `recall_personal_history` |
| report schema | `worker_report_v1` | `None` (text, today's path) |
| default thoroughness | `standard` | `quick` |

The planner picks a type per task. The plan schema replaces the free-text `expected_output` and
the per-task `tools` list with `type` and `thoroughness`; `PlanTask` loses `tools` and
`expected_output` and gains `type` and `thoroughness`. The planner prompt renders the registry
live — name and description per type, and the thoroughness levels — the way it renders the tool
surface today (FRE-1389 AC-1). `_validate_plan_json` rejects a task whose type is not in the
enum, which invalidates the plan and reaches the fallback planner. The fallback planner assigns
`researcher` when `web_search` is grantable in the current mode and `general` otherwise.

**Governance is unchanged.** A type's tool list is a *request*, filtered by
`_compute_sub_agent_grants` against `config/governance/tools.yaml` as every request is today. A
type cannot grant what governance refuses; a denied tool still lands in `denied_tools`. The union
of the two lists is exactly today's granted set, so no tool becomes unreachable from a fan-out.

**The gap redispatch is kept, and it is closed.** `_maybe_redispatch_on_gap`
(`expansion_controller.py:855-991`) today reads `task.tools`, adds the gap-named tool, and
dispatches one replacement with the widened grant. `task.tools` no longer exists, and a
same-type worker with a widened list would breach the registry. The replacement is instead a
worker of the **type that declares the gap-named tool**: when the tool named by
`stated_tool_gap` or `refused_tool_attempts` is in exactly one other type's list and is
grantable, the controller dispatches that type once with the same task, thoroughness and sibling
list. When no type declares it, or more than one does, no replacement is dispatched and the gap
reaches the primary in the result as today. The worker still never acquires a tool; the
controller still decides once; and every worker that runs is a registry type.

**Cache.** The prefix a worker shares is the base system prompt, the type block and the tools
array, because Qwen chat templates render the tools inside the system region (ADR-0149 D6).
Today ADR-0149 AC-4a protects the prompt bytes while the tools vary per task, so two workers of
one fan-out share a prefix only when their grants happen to match. Under a type, every worker of
one type has identical prompt bytes and identical tool bytes, across the fan-out and across
turns, and — because D3 moves the only per-task number out of the system prompt — across
thoroughness levels. Two types in one fan-out are two prefixes, and that is stated rather than
hidden.

**Why two.** `researcher` is the only worker shape any measured fan-out has needed. `general`
keeps today's behaviour and today's other grants reachable. A `recaller` type over
`search_memory` and `recall_personal_history` with its own schema is deferred (Alternatives,
Option 5).

### D3 — Thoroughness is a named level on the brief; the numbers come from the study

The planner assigns each task one of `quick | standard | thorough`. A new setting,
`sub_agent_rounds_by_thoroughness`, maps each level to a round budget. Each value is validated at
load to be at most `sub_agent_max_tool_iterations`. **At ship, all three values equal that cap.**
No budget changes on merge; the mechanism exists so that FRE-1487's numbers have a field to land
in, and the raise remains the owner's decision on the study's evidence.

**The budget number moves from the system prompt to the task message.** ADR-0149 move 1 placed
it in the system prompt because the budget was one number. Under D3 it varies per task, and a
per-task number in the system prompt would split D2's per-type prefix by level. The system prompt
keeps the mechanism sentence — a round holds several tool calls, the countdown follows every
round, the budget is stated in the task — and the task message states the number. Move 3's
countdown and move 5's forced synthesis read the task's budget, not the global cap. This is a
revision of ADR-0149 move 1 and is recorded below.

The `researcher` block carries Open Deep Research's self-stop rule in prose: stop searching when
the last two searches returned the same facts, and reply `DONE`. It is advice. The enforcement is
the budget.

### D4 — The combine worker is deleted; the primary is the synthesiser

The planner prompt's "+ 1 synthesis task" (HYBRID) and "+ 1 recommendation task" (DECOMPOSE)
rules are removed, and `_validate_plan_json` stops reserving the extra slot (`_MAX_TASKS` applies
without the `+ 1`). HYBRID is 1–3 tasks; DECOMPOSE is 2–5. No task in a plan consumes a sibling's
output, because no task can: the fan-out is one round of independent workers and one synthesis
call by the primary, which is what `_build_synthesis_context` already implements.

`_build_synthesis_context` renders each schema-backed worker's section from `report` under the
header ADR-0149 D4 specifies (`stop`, `report`, rounds, characters): the findings and the notes.
It then renders every worker's gaps once, at the end, under one `Not found` heading, each gap
attributed to its task, so the primary sees the turn's absence in one place and no gap is
rendered twice. A worker with no `report` — text-reporting, or any failure row — renders
`summary` as today. This rendering, per worker and combined, is one obligation and belongs to
T3.

**What this relies on from ADR-0149, and what is unbuilt.** The failed-landing guard — the
`sub_agent_fanout_incomplete` pause, `stop_and_show`, and the trailer — is ADR-0149 D4, ticketed
as FRE-1484 and unbuilt on 2026-09-11. This ADR adds nothing to it and changes nothing in it: D1
sets `success` and `report_kind`, which is what its predicate reads. Removing the combine worker
does not depend on it. The A/B (T4) does: a run whose failed landings are not surfaced cannot be
scored, so T4 depends on FRE-1484.

### D5 — What the worker is told beyond its task

**Base system prompt** (shared by every type, byte-identical, rendered once). Keeps: the
sub-agent role line, "do not ask follow-up questions", and the `TOOL_GAP: <tool_name>` sentinel
with `(one tool name, no other text on that line)` verbatim, because `_extract_stated_tool_gap`
(`sub_agent.py:358`) takes the whole remainder of the line as the name. Replaces ADR-0149 move
1's numbered budget sentence with the mechanism sentence (D3). Adds three lines, the first two
from Qwen Code's registry and master's draft:

```
Do not invent missing facts, inputs, tool results, or assumptions. When the evidence is not
there, say what you could not determine.
Complete only the assigned task. Do not broaden its scope or solve the parent task.
Only what you write in your report reaches the parent. Nothing else you read or did survives.
```

The register instruction the owner raised ("reply like a Caveman") is not adopted here. It is
an arm of the A/B in T4, because a register changes the generative distribution in a way a word
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
Stop searching when your last two searches returned the same facts. To finish, reply with the
single word DONE and no tool calls. You will then be asked for your report.
```

**The task message** (per worker, after the date block):

```
Task: {spec.task}
Other workers in this turn own: {sibling task names, or "none"}. Stay inside your task.
Thoroughness: {level}. You have a budget of {n} tool round(s).
Report: {one sentence per schema field with its limit, rendered from the schema} | "Report in text."
```

The sibling line is the isolation-by-partition mechanism (Q4), stated to the worker as Qwen Code
line 376 states it. Memory in the worker's context (FRE-1469) is not decided here; that ticket
stands, and a `recaller` type is its natural home.

### D6 — A cut landing is never reported as complete

Every worker inference call records `finish_reason` from the response into the round record
(`rounds[].finish_reason` on the capture) and, for the terminal call, onto `SubAgentResult` and
`SubAgentCapture` as `finish_reason: str | None`. On every report-writing call, `length` yields
`report_kind = narration` and `success = False`, for schema-backed and text-reporting workers
alike, checked before the content is read; this row of D1's table is D6's and is T1's. On a tool
round, `length` is recorded and the loop continues; a tool call whose arguments were cut fails at
dispatch as a malformed-argument error, which the loop already absorbs and the ledger already
lists. T1 also corrects the `LLMResponse.finish_reason` docstring (`types.py:124`), which says
the field is cloud-only while the local adapter populates it. This is the at-limit behaviour for
the generation ceiling — the owner's precondition for any later change to it — and it is the
first ticket in the chain, because every quality measurement built on report content is
corrupted until it lands.

### Revisions to ADR-0149

ADR-0149 stays Accepted. Four clauses are revised by this ADR, and master records the revision in
ADR-0149's Status Updates on merge:

| ADR-0149 clause | Revision |
|---|---|
| AC-4a: "the bytes are identical across two workers in one turn" | Identical across the workers of one **type** in one turn, and across turns. The tools array is part of the guarantee. |
| D3 move 1: the round budget "appended to `_SUB_AGENT_SYSTEM_PROMPT`, rendered once from the setting" | The mechanism sentence stays in the system prompt. The number is rendered into the task message from the task's thoroughness level. |
| D3 terminal paths, "Completed": "The content is the report, `report_kind = synthesized`, only if it is non-empty" | For a schema-backed worker the no-tool-call reply is transcript notes, and the report is written by the dedicated report-writing call that follows, with `stop_reason = completed`. For a text-reporting worker the row is unchanged. |
| D3, "What 'report' means": "The validity predicate is deterministic and weak on purpose: non-empty text" | For a schema-backed worker the predicate is D1's table: `finish_reason`, parse, schema validation with `minLength`, the whitespace check, and at least one finding or gap. Still deterministic; no longer weak. For a text-reporting worker the predicate is unchanged, except that D6 adds the `finish_reason == "length"` row for every worker. |

Nothing else in ADR-0149 changes: the countdown, the reserve, the forced synthesis on the cap,
the reserve and the timeout, the ledger and the paths that return it, D4's pause and trailer,
D6's cache form, and AC-8's three values.

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
**Cons:** A second generation per worker, and the prose one is the long one (24,000 characters
on 2026-09-11, about 315 s at 19 tok/s), on a backend where three serialised workers already do
not fit 900 s (ADR-0149 D5). The prose report is still cut at the ceiling, so the extraction reads
a truncated input. dottxt and JSONSchemaBench show a reasoning-first field in one call achieves the
same recovery.
**Why Rejected:** Same quality, one long call more. Recorded as the fallback if AC-2 fails: if the
single constrained call measures worse than prose on the study's criterion, T4 switches the
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
**Why Rejected:** Deferred, not refused. `general` keeps those tools reachable; the registry makes
a `recaller` a one-entry addition when its ticket lands.

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

### Option 9: Constrain every worker call, so the voluntary stop is already JSON
**Description:** Send `response_format` on the tool rounds too.
**Pros:** No extra call on the voluntary-stop path.
**Cons:** The grammar forbids the tool-call syntax, so the worker cannot call tools; and
whether llama.cpp applies a grammar alongside `tool_choice: "auto"` at all is undocumented.
**Why Rejected:** Breaks the tool loop. D1's one extra call on the voluntary path is the cost of
knowing which call is the landing.

---

## Consequences

### Positive Consequences

- The two instructions the model ignored become fields it cannot omit. Absence becomes sayable
  at the worker (FRE-1118; ADR-0148 at the recall layer, this at the research layer).
- The prompt has a place to be configured: a type. Source guidance, the not-carry-back rule and
  the negative-finding rule live on `researcher` and nowhere else.
- One worker and its failure class disappear. The evidence run's only fatal failure was the
  combine worker.
- A cut landing is visible. ADR-0149 D4's pause, once FRE-1484 lands, fires on the failure it
  was built for.
- Same-type workers share one prefix across a fan-out, across turns and across thoroughness
  levels; the tools bytes are part of that guarantee for the first time.
- `source_url` per finding is the handle ADR-0149 T4's shared source registry needs.

### Negative Consequences

- A grammar-constrained landing call. Shape is guaranteed; quality is measured (AC-2), not
  assumed. If it measures worse, Option 2 is the recorded fallback.
- Length limits truncate rather than compress. The limits are generous, stated to the model,
  sized under the ceiling, and a value at its limit is detectable (AC-2's second check).
- A schema-backed worker that stops voluntarily makes one more call than today: the landing. Its
  prefix is cached; its generation is the report the worker owed anyway. ADR-0149 D5's runway
  arithmetic gains that call and FRE-1487 measures it.
- The planner prompt changes shape (`type`, `thoroughness`; no `tools`, no `expected_output`).
  The fallback planner and its tests change with it.
- Two types in one fan-out are two prefixes. Stated, not hidden.
- ADR-0149 AC-4a and move 1 are revised, and the revision must be recorded on ADR-0149.
- Additive ES fields on the sub-agent capture template (a reversible deploy class).

### Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| Constrained output is structurally valid and semantically worse | Medium | AC-2 measures it on the study's own quality criterion against the prose baseline before the ADR is Implemented. Option 2 is the recorded fallback. |
| The schema's worst case exceeds the landing ceiling and the JSON is cut | High | D1 requires a full-fill probe; the limits shrink until the worst case is at most 90% of the ceiling; D6 makes any cut visible as `narration`, never `synthesized`. |
| A cloud `sub_agent` deployment refuses or ignores `strict` | Medium | D1's table: `ledger` with the content or refusal quoted and the provider named. No retry. Dialects that do not accept the schema are declared `False` and get the prose landing with a WARNING. |
| The planner picks `general` for research tasks | Medium | AC-7 measures type selection on the study's labelled queries. The registry descriptions are the lever; the fallback planner's rule is the floor. |
| `maxLength` near the grammar bound breaks compilation | Low | Every limit is ≤ 1,000. AC-1's fixture compiles the schema against the live backend once. |
| The worker writes a prose report instead of `DONE` on the voluntary stop | Low | The landing still runs and the report is still the JSON; the cost is one long generation. T4 records how often it happens. |
| Moving the budget number to the task message loses the system prompt's protection of it | Low | AC-6 checks the task message states the level's budget and that it binds; AC-5 checks the prefix is shared across levels. |

---

## Implementation Notes

| Ticket | Scope | Tier | Depends on |
|---|---|---|---|
| T1 | D6: `finish_reason` on every worker call, onto the round record, `SubAgentResult`, `SubAgentCapture` and the ES template; the `length` row of D1's table — `narration`, `success = False` — applied on every report-writing call before the content is read; the `finish_reason` docstring corrected. AC-3. | Tier-2 | this ADR |
| T2 | D2, D3, D4 (removal), D5: `WorkerType` registry with `researcher` and `general`; `PlanTask.type` and `.thoroughness` replacing `.tools` and `.expected_output`; the planner prompt, `_validate_plan_json` and the fallback planner; the closed gap redispatch by type; `sub_agent_rounds_by_thoroughness` shipped equal to the cap and validated at load; the budget number moved to the task message and read by the countdown and the forced synthesis; the combine task removed from the prompt and `_MAX_TASKS`; the base-prompt lines, the `researcher` block, the sibling line, `SubAgentSpec.worker_type` / `.thoroughness` / `.sibling_tasks`. AC-5, AC-6, AC-7, AC-8, AC-9. | Tier-1 | T1 |
| T3 | D1 and D4 (rendering): `worker_report_v1` schema and its frozen model; the voluntary-stop landing for schema-backed workers; `response_format` on the report-writing call; every row of the validity table except the `length` row; `LANDING_ACCEPTS_JSON_SCHEMA` and the OVH probe; `refusal` on `LLMResponse`; `report` and `report_schema` on result and capture; `summary` rendering and the synthesis-context rendering with the combined `Not found` section; the full-fill probe per `True` dialect, the ceiling in effect recorded, and the limits shrunk in the stated order if needed. AC-1, AC-4. | Tier-2 | T2 |
| T4 (eval) | AC-2: the A/B on the study's query set — prose landing vs `worker_report_v1`, plus the register-instruction arm — scored on the study's quality criterion, on `channel=EVAL`, with the query set and the scoring rubric committed under `tests/eval/` before the first run. Decides whether Option 2 replaces the single call. Also records how often a schema-backed worker writes prose instead of `DONE`. | Tier-1 | T3, FRE-1484, FRE-1487 |

Files touched: `orchestrator/sub_agent.py` (`_SUB_AGENT_SYSTEM_PROMPT`, `_BUDGET_BLOCK_TEMPLATE`,
`_build_sub_agent_system_prompt`, `_build_task_message`, `_forced_synthesis`, `_run_tool_loop`
including the completed path, `_ToolLoopOutcome`), `orchestrator/sub_agent_types.py`
(`SubAgentSpec.worker_type`, `.thoroughness`, `.sibling_tasks`; `SubAgentResult.report`,
`.report_schema`, `.finish_reason`), a new `orchestrator/worker_types.py` (the registry and
`worker_report_v1`), `orchestrator/expansion_types.py` (`PlanTask.type`, `.thoroughness`),
`orchestrator/expansion_controller.py` (`_build_planner_system_prompt`, `_validate_plan_json`,
`_MAX_TASKS`, `_run_dispatch`, `_build_synthesis_context`), `orchestrator/fallback_planner.py`,
`config/settings.py` (`sub_agent_rounds_by_thoroughness`), `llm_client/models.py`
(`LANDING_ACCEPTS_JSON_SCHEMA`), `llm_client/types.py` (`LLMResponse.refusal`),
`captains_log/capture.py` and the sub-agent capture ES template.

Not touched: `config/governance/tools.yaml` (grants are unchanged), every limit ADR-0149 AC-8
names, the landing ceiling, and the `response_format` dispatch path in `llm_client` (it exists).

---

## Verification / Acceptance Criteria

Each criterion states its check and how it fails. Adjudicated on FRE-1491 once T1–T4 have landed
and deployed. Two criteria are mechanism checks and are labelled so; one is a guard and is
labelled so. The outcomes are AC-1 to AC-5.

**The absence probe used by AC-1, AC-2 and AC-4.** One query in T4's committed set asks for
three items in a stated window, one of which does not exist (the FRE-1122 absence-probe shape:
a named event with no listing anywhere). The seeded absent item is the discriminator between a
report that says what it did not find and one that fabricates completeness.

**AC-1 — A schema-backed worker's report carries its findings and its gaps as data, and a
report that is not data is not accepted.** *Check (seeded, four fixtures on the same stub tool
set):* the stub tool returns results carrying `K` coined marker tokens with coined source strings,
and the task names one item no result contains. Fixture A's stub model returns a JSON string the
test built from the markers with the absent item under `gaps`; assert the request carried
`response_format` with `strict: true`, `tools` and `tool_choice = "none"`, and that the result's
`report.findings` holds all `K` markers with their sources, `report.gaps` names the absent item,
`report_kind == "synthesized"`, `report_schema == "worker_report_v1"`, and `summary` renders every
marker and the gap. Fixture B's stub returns prose; assert `report_kind == "ledger"` and
`success == False`. Fixture C's stub returns `{"working_notes":"","findings":[],"gaps":[]}`;
assert `ledger` and the "no findings and no gaps" reason. Fixture D's stub returns one finding
whose four strings are `" "`; assert `ledger` with a validation reason. *Live:* on the absence
probe, every `researcher` capture holds a validated report, and the capture for the task that
owns the absent item names it under `gaps` and not under `findings`. *Seeded negatives:* the
validator and the whitespace check disabled — fixtures B, C and D become `synthesized`. The
`response_format` withheld on the live backend, validator intact — the live check fails: no
`researcher` capture holds a validated report, because the model writes prose (the recorded
0-of-2), and every landing is a `ledger`. The mechanism's effect is only observable with a real
model, so its negative is the live one. *Fails if* a schema-backed landing yields `synthesized`
without a validated non-empty report, if fixture B, C or D passes as `synthesized`, or if the
absent item appears as a finding.

**AC-2 — The schema does not cost quality.** *Check:* T4 runs the committed query set twice on
`channel=EVAL`, same limits, same model, same prompts except the landing form: prose (today's
instruction) and `worker_report_v1`. Each report is scored on the study's criterion — names
events dated inside the asked window, with venue and a cited source — by the committed rubric,
before any timing figure is read. The schema arm's pass rate is at least the prose arm's, and on
the absence probe the schema arm reports the absent item as a gap on every run. *Second check:*
zero string values in the schema arm have length equal to their `maxLength`. A value at its
limit is the guard firing, by definition of the limit as a guard, and is counted as a cut whether
or not the text reads complete. *Fails if* the schema arm scores below the prose arm on the
criterion, fabricates the absent item on any run, or any value is at its limit. If it fails, T4's
deliverable is the switch to Option 2, and this criterion is re-run.

**AC-3 — A cut landing is never reported as complete.** *Check (seeded):* a stub client returns
`finish_reason = "length"` on the landing call with content that is a JSON prefix. Assert
`report_kind == "narration"`, `success == False`, the content is the partial followed by the
ledger, and the capture's `finish_reason == "length"`. A second fixture returns
`finish_reason = None`; assert `ledger`. *Live:* on T4's runs, the number of sub-agent captures
equals the number of workers the plans dispatched (no emission is missing), and no capture
carries `finish_reason == "length"` together with `report_kind == "synthesized"`. Today's worker
1 record is the seeded negative for the live form. *Seeded negative for the fixture:* with the
`finish_reason` check disabled, the cut JSON prefix reaches the content path and becomes a
`ledger` (parse failure) or, on a text-reporting worker, `synthesized`; either way the asserted
`narration` is absent. *Fails if* a capture is missing, or the contradiction exists.

**AC-4 — The primary receives data it can read, and says what was not found.** *Check
(fixture):* `_build_synthesis_context` on two schema-backed results with `F` findings and `G` gaps in
total renders exactly `F` finding lines and exactly `G` gap lines under one `Not found` heading,
and contains no `{` character from the JSON. *Live:* on the absence probe, the primary's final
answer states that the absent item was not found, and does not assert a date, venue or source
for it. *Fails if* a finding or gap is dropped or duplicated in the rendering, if raw JSON reaches
the context, or if the answer asserts the absent item (the FRE-1484 confabulation shape).

**AC-5 — Same-type workers share one prefix across thoroughness levels.** *Setup:* the test pins
`context` to a fixed four-message list for both workers. It measures two token counts on the
local backend once, from a call whose user message is empty: `P`, the prompt tokens of the base
system prompt plus the `researcher` block plus the `researcher` tools; and `B`, the prompt tokens
of the base system prompt alone with no tools. *Check:* in a fan-out of two `researcher` workers,
one `quick` and one `thorough` (test settings giving them different budgets), the second worker's
first call reports `cache_read_tokens` of at least `0.95 × P`. The comparison is against the
static prefix, not the whole prompt, so the per-worker task tail does not dilute it. *Seeded
negative:* render the budget number back into the system prompt; the second worker's
`cache_read_tokens` falls to at most `B`. *Type boundary:* with the second worker typed
`general`, `cache_read_tokens` is at most `B`. *Fails if* two same-type workers of different
levels read fewer than `0.95 × P` cached tokens, or the negative does not fall to at most `B`.

**AC-6 — The thoroughness level binds.** *Mechanism check for D3.* *Check (seeded):* with
`sub_agent_rounds_by_thoroughness = {quick: 2, standard: 4, thorough: 5}` in test settings, a
`quick` task's message states "2 tool round(s)", the countdown after round 1 reads "1 of 2", and
the call after round 2 is the forced synthesis with `stop_reason == "cap"`. A value above
`sub_agent_max_tool_iterations` is refused at settings load. *Fails if* the worker runs a third
round, the message states the global cap, or an over-cap value loads.

**AC-7 — The planner picks types the registry declares, and picks `researcher` for research.**
*Check:* T4's committed query set labels each query `research` or `other` (the owner's label).
On every `research` query, every plan task is `researcher`; on every query, every task type is in
the enum. A fixture plan with `type: "analyst"` is rejected by `_validate_plan_json` and the
fallback planner's tasks carry `researcher` when `web_search` is grantable. *Fails if* an unknown
type reaches dispatch, or a `research` query's plan carries a `general` task.

**AC-8 — No limit changed, no combine task exists.** *Guard, not a discriminating criterion;
here because ADR-0149 AC-8 and FRE-1487 AC-6 demand it.* *Check:* in the merged diff,
`sub_agent_max_tool_iterations`, the `sub_agent` role's `default_timeout`, and
`orchestrator_task_timeout_seconds` are unchanged; every value of
`sub_agent_rounds_by_thoroughness` equals `sub_agent_max_tool_iterations`; and the landing call's
`max_tokens` is not above the ceiling T3 records as in effect before its change. On T4's runs, no
plan holds a task whose name or goal names synthesis, combination or recommendation, and no
worker's input context contains another worker's digest.

**AC-9 — The worker is told its siblings.** *Mechanism check for D5.* *Check:* in a two-task
fan-out, each worker's task message names the other task; in a one-task fan-out the line reads
"none"; the base prompt contains the isolation line. *Fails if* a worker's messages name no
sibling in a multi-task plan.

**Seeded negatives.** Each mechanism is disabled in turn and the named criterion must fail.

| Mechanism disabled | Criterion that must fail |
|---|---|
| `response_format` on the landing call (live) | AC-1 (no `researcher` capture holds a validated report) |
| The validator and the whitespace check | AC-1 (fixtures B, C and D become `synthesized`) |
| `finish_reason` read on the landing | AC-3 (the cut report is not `narration`) |
| Deterministic rendering in the synthesis context | AC-4 (JSON in the context, or a count mismatch) |
| Budget number kept out of the system prompt | AC-5 (cache ratio falls between levels) |
| Per-task budget read by the loop | AC-6 (a third round runs) |
| Type validation in `_validate_plan_json` | AC-7 (an unknown type reaches dispatch) |
| Combine-task removal | AC-8 (a synthesis task appears in a plan) |
| Sibling line | AC-9 |

---

## References

- FRE-1491 — this ADR's commission; every measurement in Context; the research mandate
- FRE-1487 — the study; owner of every number D3 leaves blank; AC-2 runs on its query set
- FRE-1483 / ADR-0149 — the landing contract this ADR types; D6's cache form; AC-4a and move 1 revised here
- FRE-1484, FRE-1485 — ADR-0149 T2/T3, unbuilt; T4 here depends on FRE-1484
- FRE-1486 — the shared source registry follow-on; D1's `source_url` is its handle
- FRE-1118 / ADR-0148 — absence must be reachable and sayable; `gaps` is the worker-layer form
- FRE-1122 — the absence-probe fixture shape AC-1, AC-2 and AC-4 reuse
- FRE-1469 — a sub-agent knows nothing about the user; not decided here
- FRE-1389 — the worker is a bounded function; the closed tool list; `TOOL_GAP` parsing
- FRE-1399, FRE-1379, FRE-1387 — the cap path, the killed path, the digest cap
- ADR-0138 — the citation contract; why worker findings are not yet citable
- ADR-0147 — memory belongs to the planner
- ADR-0028 — skills as Tier 2
- `orchestrator/sub_agent.py:119-158` (prompt constants), `:340-362` (`_extract_stated_tool_gap`), `:593-648` (prompt and task assembly), `:931-1081` (`_forced_synthesis`), `:1151-1275` (the loop and its completed path)
- `orchestrator/sub_agent_types.py:46-126`, `:129-246`
- `orchestrator/expansion_controller.py:88-139` (planner prompt), `:690-711` (spec construction), `:734-770` (dispatch), `:1006-1056` (`_build_synthesis_context`), `:1060-1120` (`_validate_plan_json`)
- `llm_client/litellm_client.py:1009`, `:1216`, `:1648`, `:1863`; `llm_client/types.py:124-139`; `llm_client/models.py:207-241`
- `llm_client/factory.py:124`, `config/model_roles.yaml:81-84`
- Anthropic, "How we built our multi-agent research system" (2025) — the four-part brief, effort tiers, the duplication failure
- Anthropic, "Effective context engineering for AI agents" (2025) — the condensed return
- OpenAI, Structured Outputs guide — the `json_schema` / `strict` contract; all properties required, `additionalProperties: false`
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
numbers the study supplies. Codex round 1 (16 blocking findings) resolved: the landing is always
its own call so the voluntary stop can be constrained; every nested property is required; the
schema's worst case is sized under the existing ceiling rather than the ceiling raised; every
finish reason has a disposition and an empty report is a ledger; `general`'s tools are a closed
list; the gaps are rendered once; the dialect capability is declared; ADR-0149's revised
clauses are named; and the criteria gained the absence probe as their discriminator. Codex round
2 (8 blocking) resolved: the Completed path and the validity predicate are added to the ADR-0149
revisions; the landing semantics are stated per path and per dialect; `minLength` and a
whitespace check with a blank-row fixture; the worst case is a defined procedure with a shrink
order and a floor; the `length` row is T1's alone; the gap redispatch is kept and closed by type;
AC-1's `response_format` negative is the live one; AC-5 compares against the measured static
prefix.
