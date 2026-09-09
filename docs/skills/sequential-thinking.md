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
  When the problem has a genuine false start, show the step you tried and
  abandoned, alongside the correct path. Never invent a wrong step you did
  not actually take. An unremarkable linear chain of reasoning with no false
  start must stay invisible to the reader.
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

**Owner decision, 2026-09-05 — not always, and not never.** Do not surface anything for an
ordinary linear chain of reasoning that needed no revision or branch — that case must **stay
clean**, exactly as invisible as it was under the removed tool, which never surfaced anything to
the model or the user either way.

**Owner decision, 2026-09-08 — the mechanism changed; the "not always, not never" boundary did
not.** Master's live verification of the 2026-09-05 design ("surface only on a spontaneous
reversal") found it structurally unmeetable: the primary runs with a 32,768-token thinking
budget, so a reversal happens inside the invisible thinking block, and by the time the visible
answer is written the question is already settled. There is no live reversal left to mark.

The owner ruled: "not achievable. We can force the model to think through a problem and include
a step that was wrong." The replacement mechanism is a **false start**, not a spontaneous
reversal — when the problem genuinely has one:

- If the obvious first line of attack on this problem fails, show that **tried and abandoned**
  step in the answer, alongside the correct path, and name what was wrong with it — for example:
  "First attempt: X — that fails because Y, so this answer instead does Z."
- Show it only when a false start actually exists. An unremarkable linear chain of reasoning
  with no false start available must stay clean, per the 2026-09-05 boundary above.
- Do not renumber the whole answer into an essay around it — one short line at the point in the
  answer where the false start belongs.

**Never invent a false start you did not actually take.** A plausible-looking wrong step
manufactured for presentation, rather than reasoning the model genuinely did, is confabulation —
the same failure shape FRE-1327 is open on. If the problem has no real false start, show none;
a fabricated one is worse than showing nothing.

Rationale: a false start the reader cannot see is what native thinking already discards once the
question is settled, so it is the part worth surfacing. It stays rare — most problems this skill
engages on have no wrong turn at all — so ordinary answers do not turn into numbered essays.

## When NOT to engage

- A short factual question with one obvious answer.
- A single-step lookup or command.
- Any turn where no assumption is at risk of being wrong and no real alternative is being
  weighed. Engaging here just manufactures a numbered essay out of a simple answer, which is a
  net loss over saying nothing.
