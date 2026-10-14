---
name: sequential-thinking
description: 'Gives a procedure for multi-step reasoning: estimate the step count, state the current
  step, mark each revision, name each branch, and check that the answer holds. Use when a problem
  needs several dependent steps or compares real alternatives. Not for what the answer shows, which
  the system prompt already sets, or for single-step lookups, which `web_search` and `search_memory`
  answer.'
---

# sequential-thinking — Multi-Step Reasoning Procedure

## The procedure

Apply these steps in your own working, for the whole turn. The system prompt sets what the answer shows.

- **Estimate first.** Before working the problem, form a rough count of the steps it will take.
  Revise that count as you go — do not silently overrun it.
- **State the current step.** Know which step you are on and how many you believe remain.
- **Mark a revision as a revision.** When a later step contradicts an earlier one, that is a
  revision — name what is being reconsidered and why, not just the new conclusion.
- **Name a branch when you take one.** When two genuine alternatives are worth pursuing, name
  the branch point and which line you are following.
- **Gate completion on done-ness.** Only treat the problem as finished when the answer actually
  holds up — not when you have produced a plausible-looking response.

## Success

Check these points before the answer is final:

- The answer holds up. It covers every part of the question, and no later step contradicts an
  earlier one without a marked revision.
- The step count that was estimated first matches the work done, or the change was noted.

## Recovery

What to do on each failure, with a stop rule:

| Failure | Cause | Next step |
|---|---|---|
| A later step contradicts an earlier conclusion | An assumption was wrong | Mark it as a revision: name what is reconsidered and why. Redo each step that depended on it. |
| The work passes the estimated step count | The task is larger than estimated | Note the new count. Do not continue as if the first count held. |
| The answer does not hold up on the final check | A step was skipped, or a conclusion was assumed | Name the point that failed. Redo the work from the step that holds it. |
| The same check fails twice | — | Stop. Tell the user what is not settled and what was tried. |
