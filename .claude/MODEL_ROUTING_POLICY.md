# Model Routing Policy

> Last updated: 2026-03-19

Development model selection criteria. Optimizes for **quality first, then cost** — use the most capable model where it matters, cheaper models where Opus reasoning depth is wasted.

Applies to all projects. Invoked via the Superpowers plugin (`writing-plans`, `brainstorming`, `executing-plans` skills).

---

## The Three Tiers

### Tier 1: Opus — "The Architect"

**When the task requires judgment, design, or original thinking.**

| Use for | Why Opus |
|---------|----------|
| Spec writing | Structural decisions, trade-off reasoning, system design |
| Implementation plans | Chunk decomposition, dependency ordering, TDD step design |
| Plan review (subagents) | Catching design flaws, cross-chunk consistency, spec compliance |
| ADRs / architecture decisions | Weighing alternatives, documenting rationale |
| Complex debugging (escalated) | When Sonnet failed — needs fresh reasoning about root cause |
| Ambiguous requirements | Anything requiring "what should we build?" thinking |

**Signal:** The task requires making a design decision that isn't already written down.

### Tier 2: Sonnet — "The Implementer"

**When a detailed plan exists but the work isn't purely mechanical.**

| Use for | Why Sonnet |
|---------|-----------|
| Feature implementation from plans | Follows TDD steps, writes real code, adapts to minor surprises |
| First-pass debugging | Test failures, type errors, import issues — up to 3 attempts |
| Code review responses | Applying feedback that's already specific |
| Refactoring with clear scope | "Move X to Y, update imports" with some judgment needed |
| Integration wiring | Connecting modules where interfaces are defined but glue code isn't |

**Signal:** The plan has complete code and exact steps, but the executor may need to adapt to what the codebase actually looks like (import paths shifted, a method was renamed, test needs a new fixture). Sonnet handles that. Haiku doesn't.

**Note on spec quality test:** The five-criteria spec quality test (see below) applies specifically to **plan-driven feature implementation**. Other Tier 2 tasks (refactoring, code review responses, integration wiring) are ready for Sonnet when scope is clearly defined and boundaries are explicit — they don't require complete pre-written code.

### Tier 3: Haiku / Qwen 3.5-35B — "The Executor"

**When the task is fully mechanical — zero judgment required.**

| Use for | Why cheap model |
|---------|----------------|
| Linear issue creation from a list | Fill in template fields, no design thinking |
| Git operations | Commits, merges, pushes, branch cleanup |
| Running quality checks | `pytest`, `mypy`, `ruff` — report results |
| Formatting/linting fixes | `ruff format`, fix what ruff tells you to fix |
| File moves / renames | Mechanical restructuring with explicit instructions |
| Documentation from templates | Fill in a template where all content is provided |
| Boilerplate generation | `__init__.py` files, re-exports, config stubs |

**Signal:** Could a bash script do 80% of this?

**Haiku vs Qwen 3.5-35B:** Haiku when you need reliable instruction-following at scale (parallel subagents, API calls). Qwen when you want zero cost and the task is simple enough for the local SLM Server.

---

## Decision Tree

```
Does this task require a design decision?
  YES → Tier-1: Opus
  NO ↓

Is there a detailed plan with complete code?
  NO → Tier-1: Opus (write the plan first)
  YES ↓

Might the executor need to adapt to surprises?
  YES → Tier-2: Sonnet
  NO ↓

Is it purely mechanical (copy/paste/run)?
  YES → Tier-3: Haiku / Qwen
  NO  → Tier-2: Sonnet (safe default — surprises are easy to underestimate)
```

---

## "Detailed Enough" — The Spec Quality Test

**Applies to plan-driven feature implementation tasks.** A plan is ready for Sonnet execution when **all five** are true:

1. **Complete code** — not pseudocode, not "implement something like this"
2. **Exact file paths** — no "find the right file"
3. **Exact test commands** with expected output
4. **Atomic steps** — each step is 2-5 minutes, one thing
5. **No design decisions deferred** — no "choose the best approach"

If any of these fail → Opus writes or improves the plan first, then Sonnet executes.

---

## Cross-Tier Tasks

When a task has components at different tiers (e.g., "write a script to automate X" — the logic is Tier 2 but wiring it up is Tier 3):

- **Prefer (A): Decompose** into separate subtasks with their own tier labels, when the components are independently executable
- **Use (B): Run at highest tier** when the components are tightly coupled and can't be cleanly separated

Apply the "if unsure, use the higher tier" rule to the whole task when in doubt.

---

## Escalation Model (Debugging)

```
Start: Sonnet attempts fix
  ↓
Tests pass? → Done
  ↓ NO
Attempt 2 (Sonnet)
  ↓
Tests pass? → Done
  ↓ NO
Attempt 3 (Sonnet)
  ↓
Tests pass? → Done
  ↓ NO — OR — model is floundering (see signals below)
Escalate to Opus with full error context
```

### Floundering Signals (escalate immediately, don't wait for attempt 3)

- **Same error twice:** The same error message appears after the model's "fix"
- **Self-revert:** Model reverts its own changes
- **Circular reasoning:** Trying the same approach with minor variations
- **Wrong layer:** Model is fixing symptoms instead of root cause (e.g., suppressing the error rather than fixing the cause)

### When Escalating to Opus, Include

- The original error message
- What Sonnet tried (all attempts)
- Why each attempt failed
- Files modified (with diffs or summary of changes)
- Current state of the code

---

## Linear Labeling

Three labels on the FrenchForest team:

| Label | Color | Applied when |
|-------|-------|-------------|
| `Tier-1:Opus` | Purple (#BB87FC) | Task requires design decisions, architecture, spec writing |
| `Tier-2:Sonnet` | Blue (#4EA7FC) | Task has detailed plan, implementation work |
| `Tier-3:Haiku` | Gray (#95a2b3) | Task is mechanical execution |

### Rules

- **Every new Linear issue** gets exactly one Tier label
- **Plans assign tiers** to each task in the summary table
- **Subagent dispatch** considers the tier when choosing the model parameter
- If unsure between two tiers, **use the higher tier** — quality over cost

---

## Impact on Workflows

### When Writing Plans (Superpowers `writing-plans` skill)

Each task in the plan gets a `**Model:** Tier-X` annotation. The summary table includes a Model column:

```markdown
| Task | Key File | Model |
|------|----------|-------|
| 1. Config entries | config/settings.py | Tier-3:Haiku |
| 2. Decomposition assessment | request_gateway/decomposition.py | Tier-2:Sonnet |
| 3. Context budget | request_gateway/budget.py | Tier-2:Sonnet |
```

### When Creating Linear Issues

Each issue gets the appropriate `Tier-X:Model` label based on the decision tree. The description includes the tier assignment.

### When Dispatching Subagents

Use the `model` parameter on Agent tool calls:
- Plan review subagents → `model: "opus"` (Tier 1)
- Implementation subagents → `model: "sonnet"` (Tier 2)
- Mechanical subagents → `model: "haiku"` (Tier 3)

### When Debugging

Start with Sonnet. Escalate per the escalation model. Never start debugging with Haiku — debugging always requires at least Tier 2.

---

## Examples

### "Add a new config field with a default value"
→ **Tier 3: Haiku.** Exact location known, no design decisions, mechanical insertion.

### "Implement Task 7 from the Slice 2 plan"
→ **Tier 2: Sonnet.** Plan has complete code, but Sonnet may need to adapt imports or fixture setup.

### "Write the Slice 3 implementation plan"
→ **Tier 1: Opus.** Requires reading the spec, decomposing into chunks, designing TDD steps, ordering dependencies.

### "Tests are failing after the executor refactor"
→ **Tier 2: Sonnet first.** If still failing after 3 attempts or floundering → **Tier 1: Opus.**

### "Create 15 Linear issues from this plan"
→ **Tier 3: Haiku.** Template fill, no judgment. (Though the first time we defined the template, that was Opus.)

### "Review the Slice 2 plan for spec compliance"
→ **Tier 1: Opus.** Requires cross-referencing spec sections, catching design flaws, consistency checking.

### "Write a script to automate Linear issue creation, then run it"
→ **Cross-tier.** Writing the script = Tier 2: Sonnet. Running it = Tier 3: Haiku. Decompose if independently executable.
