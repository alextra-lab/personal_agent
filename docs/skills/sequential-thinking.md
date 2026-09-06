---
name: sequential-thinking
description: Structured multi-step reasoning discipline — estimate steps up front, mark revisions, name branch points, and gate completion on real done-ness.
when_to_use: A problem needs several dependent steps, an early assumption may need revising mid-way, or two or more real alternatives are being weighed. Do not use for short factual questions or single-step lookups.
keywords:
  - think step by step
  - walk me through this step by step
  - reason through this
  - break this problem down
  - think this through carefully
nudge: |
  Keep the discipline below internally on every turn this skill engages.
  Write a visible marker into the answer ONLY when you reverse an earlier
  conclusion or abandon one line of reasoning for another. An unremarkable
  linear chain of reasoning must stay invisible to the reader.
---

# sequential-thinking — Structured Reasoning Discipline

> **Tier:** N/A — prompt technique, not a tool.
> **Provenance:** `docs/architecture_decisions/ADR-0028-external-tool-cli-migration.md:83` ruled
> `mcp_sequentialthinking` "Not a real tool — prompt technique", recommendation 5 said to remove
> it, and FRE-1358 executed that removal. This skill replaces the discipline the removed tool's
> instruction text imposed, without re-adding a tool ADR-0028 already ruled against.

## What this replaces

The removed MCP server did no reasoning. It type-checked its inputs, extended its own step
estimate by one line of logic, appended to two in-memory arrays, and printed a formatted box to
**stderr** that neither the model nor the user ever saw. What it actually bought was:

1. **The instruction text** — a discipline for estimating steps, marking revisions, and naming
   branch points. This skill reproduces that text.
2. **A forcing function** — calling the tool required committing to a step number and a
   continue/stop decision each time. A skill cannot force a call-per-thought loop; it can only
   ask for the discipline. Follow it because it is asked for, not because anything enforces it.

The reasoning itself is not the gap. The primary model already runs with a
32,768-token thinking budget (`config/models.yaml`, `thinking_budget_tokens`) — native thinking
is unstructured and invisible, and does not itself oblige a model to admit it is revising an
earlier conclusion. This skill supplies the discipline, not more thinking.

## The discipline

When this skill engages, hold yourself to the following, internally, for the whole turn:

- **Estimate first.** Before working the problem, form a rough count of the steps it will take.
  Revise that count as you go — do not silently overrun it.
- **State the current step.** Know which step you are on and how many you believe remain.
- **Mark a revision as a revision.** When a later step contradicts an earlier one, that is a
  revision — name what is being reconsidered and why, not just the new conclusion.
- **Name a branch when you take one.** When two genuine alternatives are worth pursuing, name
  the branch point and which line you are following.
- **Gate completion on done-ness.** Only treat the problem as finished when the answer actually
  holds up — not when you have produced a plausible-looking response.

## When to surface structure in the answer

**Owner decision, 2026-09-05 — not always, and not never.** Surface a visible marker in the
answer **only** when you actually reverse an earlier conclusion or abandon one line of reasoning
for another. Do not surface anything for an ordinary linear chain of reasoning that needed no
revision or branch — that case stays exactly as invisible as it was under the removed tool,
which never surfaced anything to the model or the user either way.

Rationale: a revision the reader cannot see is precisely what native thinking already fails to
give, so it is the part worth surfacing. It is also the rarest of the three cases, so ordinary
answers do not turn into numbered essays.

A visible marker names what was reconsidered, in one short line, at the point in the answer
where the reversal happened — for example: "Correction: the first read of the ticket assumed
X; that is wrong because Y, so this answer instead does Z." Do not renumber the whole answer
into an essay around it.

## When NOT to engage

- A short factual question with one obvious answer.
- A single-step lookup or command.
- Any turn where no assumption is at risk of being wrong and no real alternative is being
  weighed. Engaging here just manufactures a numbered essay out of a simple answer, which is a
  net loss over saying nothing.
