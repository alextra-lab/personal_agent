# Evaluation Results Report

**Generated:** 2026-05-11T16:38:17.841950+00:00

## Summary

| Metric | Value |
|--------|-------|
| Paths Passed | 2/4 |
| Assertions Passed | 20/22 |
| Assertion Pass Rate | 90.9% |
| Avg Response Time | 126560 ms |

## Results by Category

| Category | Passed | Failed | Pass Rate |
|----------|--------|--------|-----------|
| Intent Classification | 1 | 1 | 50% |
| Context Management | 0 | 1 | 0% |
| Edge Cases | 1 | 0 | 100% |

## Path Details

### ❌ CP-01: Conversational Intent

**Category:** Intent Classification | **Session:** `febf164c-9057-45d5-943c-06771f5f07e1`
**Assertions:** 7/8 passed

**Turn 1** (8870 ms)
- **Sent:** Hey, how's it going?
- **Trace:** `971358a0-98be-4369-b9a2-e560050c3c93`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational
  - ✅ intent_classified.confidence: expected=0.7, actual=0.7
  - ✅ decomposition_assessed.complexity: expected=simple, actual=simple
  - ✅ decomposition_assessed.strategy: expected=single, actual=single
  - ✅ Event 'tool_call_completed': not found (expected: absent)
  - ✅ Event 'expansion_dispatch_started': not found (expected: absent)

**Turn 2** (20420 ms)
- **Sent:** Tell me something interesting you've learned recently.
- **Trace:** `67c1b347-dd09-4717-bbaf-a3746c552984`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational
  - ❌ Event 'tool_call_completed': found (expected: absent)

**Quality Criteria (Human Eval):**
- [ ] Response is natural and engaging, not robotic
- [ ] Appropriate length (not a one-word answer, not an essay)
- [ ] No unnecessary tool invocations or system introspection
- [ ] Turn 2 response demonstrates personality or knowledge

---

### ✅ CP-05: Delegation Intent (Explicit and Implicit)

**Category:** Intent Classification | **Session:** `49eb7570-3d97-4aef-a142-7a935ee2220e`
**Assertions:** 5/5 passed

**Turn 1** (325314 ms)
- **Sent:** Use Claude Code to write a function that parses nested JSON configuration files with schema validati...
- **Trace:** `336c635d-f8be-4803-9341-0fbe32d1633b`
  - ✅ intent_classified.task_type: expected=delegation, actual=delegation
  - ✅ intent_classified.confidence: expected=0.85, actual=0.85
  - ✅ decomposition_assessed.strategy: expected=delegate, actual=delegate

**Turn 2** (282298 ms)
- **Sent:** Write unit tests for the edge cases — circular references, missing required keys, and deeply nested ...
- **Trace:** `86885632-86f6-4d80-ab74-a3bdfc3e348d`
  - ✅ intent_classified.task_type: expected=delegation, actual=delegation

**Turn 3** (96108 ms)
- **Sent:** What context would you include in the handoff to make sure Claude Code doesn't need to ask follow-up...
- **Trace:** `0e7d048b-f293-464e-a276-0eefcab90be9`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Quality Criteria (Human Eval):**
- [ ] Turn 1: Agent composes a DelegationPackage rather than writing code
- [ ] Turn 1: task_description is clear for an agent with no prior context
- [ ] Turn 2: acceptance_criteria includes the three edge cases
- [ ] Turn 3: Demonstrates awareness of what external agents need
- [ ] Package is sufficient for Claude Code without follow-up questions

---

### ❌ CP-20: Progressive Token Budget Management

**Category:** Context Management | **Session:** `1954d70d-4f1d-46c2-859f-150fdce4de6b`
**Assertions:** 4/5 passed

**Turn 1** (85675 ms)
- **Sent:** Run the system health check.
- **Trace:** `60b6c985-1c9c-41a6-83ac-c3118c5bac5a`
  - ❌ intent_classified.task_type: expected=conversational, actual=tool_use
  - ✅ Event 'tool_call_completed': found (expected: present)

**Turn 2** (117201 ms)
- **Sent:** Now show me the recent error details.
- **Trace:** `ea0ebd54-3707-4533-b563-9520fafeb037`
  - ✅ Event 'tool_call_completed': found (expected: present)

**Turn 3** (37644 ms)
- **Sent:** Also check the system metrics.
- **Trace:** `7ca98f55-1367-4f61-a780-e92750ae2aa3`
  - ✅ Event 'tool_call_completed': found (expected: present)

**Turn 4** (21753 ms)
- **Sent:** Summarize everything you've found — is the system healthy overall?
- **Trace:** `75fbd096-af18-418a-a663-ba856942e7b4`
  - ✅ Event 'context_budget_applied': found (expected: present)

**Quality Criteria (Human Eval):**
- [ ] Each tool call returns valid data
- [ ] Turn 4 synthesizes findings coherently
- [ ] If trimmed, most recent tool results preserved
- [ ] Agent identifies any genuine issues
- [ ] context_budget_applied event fires on Turn 4 with correct trimmed/overflow_action fields

---

### ✅ CP-24: Ambiguous Intent

**Category:** Edge Cases | **Session:** `e0ecb758-e84d-4741-9598-51689a738667`
**Assertions:** 4/4 passed

**Turn 1** (327773 ms)
- **Sent:** Can you look into why our unit tests keep failing and fix the flaky ones in the authentication modul...
- **Trace:** `1598d0c9-72e4-4418-856d-4ddb76f1764b`
  - ✅ intent_classified.task_type: expected=delegation, actual=delegation
  - ✅ intent_classified.confidence: expected=0.85, actual=0.85

**Turn 2** (69104 ms)
- **Sent:** Actually, before fixing anything, just analyze the failure patterns first.
- **Trace:** `174ac471-3ec8-4104-b68e-f591f4611267`
  - ✅ intent_classified.task_type: expected=analysis, actual=analysis
  - ✅ intent_classified.confidence: expected=0.8, actual=0.8

**Quality Criteria (Human Eval):**
- [ ] Turn 1: treats as delegation/coding task
- [ ] Turn 2: shifts to analysis mode — investigates patterns
- [ ] Transition between intents is smooth
- [ ] No carry-over of Turn 1 intent into Turn 2

---
