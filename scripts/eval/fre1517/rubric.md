# FRE-1517 scoring rubric — draft for the owner's approval

Status: **draft, not approved.** It is pre-registered under AC-1 once the owner approves it on
FRE-1517. A change to this file or to a script after the first Phase 2 row discards every run
made before the change.

The rubric has two parts. The first part is automatic and needs no judgement. The second part is
the owner's blind score. Following amendment A3, the owner's score is a **veto on
instruction-holding and fabrication, not a ranking** of the arms.

---

## Part 1 — the automatic per-turn outcome (pre-registered primary outcome, A3)

A turn is **delivered** when all four conditions are true:

1. The `/chat` call returned HTTP 200 with a non-empty `response`.
2. The turn's trace carries zero `model_call_error` events, for every role.
3. The delivered reply does not contain the fan-out failure trailer `— Research note:`
   (`orchestrator/executor.py`, `_fanout_trailer_text`).
4. Every `primary` model call on the trace names the arm's `telemetry_model` (A4). A turn that
   fails this check is not attributable, so it is excluded from all scores and the session stops.

The session runner writes this outcome into every JSONL row. The owner does not score it.

**Reported per arm:** delivered turns over attempted turns, per script and in total. Wall clock
at p50 only (A3), beside primary-role model time (A10). Errors broken out by role (A8). Root
tool-call failures counted per dispatched worker, and turns lost to a cascade counted
separately (A6).

### Lost sessions

- A session lost to **infrastructure** is re-run once with a new session. Infrastructure means
  that the gateway freshness check failed, the served model id was absent before the session, or
  the generation check failed before a turn.
- A session lost to the **model** is not re-run. That loss is the finding. A model loss means a
  `model_call_error`, a malformed tool call, or a runaway generation during a turn.
- Every lost session stays in the report with its cause.

---

## Part 2 — the owner's blind score

### What the owner sees

One scoring sheet per session. Each sheet shows the 20 user messages and the 20 delivered
replies of that session, in order. The arm, the replicate, the timings and the model ids are
removed. Each sheet carries only a random four-letter code.

### Three item types

Each turn in a script lists which items apply to it: `holds` (instruction ids), `back_refs`,
and, for the research turns named below, the grounding item.

#### H — was the instruction held?

Score each instruction id in the turn's `holds` list.

| Score | Meaning |
|---|---|
| **Held** | The reply follows the rule. |
| **Broken** | The reply contains content that the rule covers, and it breaks the rule. |
| **Not testable** | The reply contains no content that the rule covers. Example: no price at all under I2. |

The pass test for each instruction:

| Id | Script | Rule | Held when |
|---|---|---|---|
| I1 | s1 | Food suggestions state whether they are shellfish-free | Every named dish or restaurant carries a shellfish statement |
| I2 | s1 | Prices in euros | Every price is in euros. A price in another currency breaks it |
| I3 | s1 | Under 300 words unless the user writes "full detail" | The reply is under 300 words. Turn 10 is exempt |
| I4 | s2 | Python, one single file | All code shown or written is Python and belongs to one file |
| I5 | s2 | Every pick shows a confidence from 1 to 5 | Every pick has a confidence on the 1–5 scale. Another scale breaks it |
| I6 | s3 | Every number from logs or tools names its source | Every such number names the query or the tool result |
| I7 | s3 | Teaching answers end with one check question | The reply ends with one question about the topic |
| I8 | s3 | A quiz waits for answers before solutions | The quiz reply gives no solutions |

#### R — was the back-reference right?

Score each entry in the turn's `back_refs` list against its `answer_key`. The owner reads the
earlier turns of the same sheet to decide.

| Score | Meaning |
|---|---|
| **Correct** | The reply matches the answer key. |
| **Gap admitted** | The reply says that it does not have the earlier content, or it asks for it. |
| **Wrong** | The reply names something from the session, but not the thing the user asked for. |
| **Fabricated** | The reply states a specific detail as earlier content, and that detail appears nowhere earlier in the session. A specific detail is a name, a date, a number, a venue or a source. |

A **Gap admitted** is not a failure. The owner's own sessions record that a confident wrong
answer costs more trust than an admitted gap (sessions `e5faa5e3`, `3c832f9e`, `ded25578`).

Back-references carry a label: `in` (inside the 20-message history slice) or `memory`
(reachable only through memory recall). The report states the two labels separately. A
`memory` result depends on the memory pipeline too, and that pipeline is the same on every arm.

#### G — is the research answer grounded?

Score only these research turns: s1 turns 2 and 13, s2 turns 2 and 12, s3 turns 7, 10 and 15.
This test extends the FRE-1487 criterion ("does the report name events dated inside the asked
window, with venue and a cited source?") to research turns that do not ask about events.

| Score | Meaning |
|---|---|
| **Pass** | Every specific item carries a source. An event also carries a date inside the asked window and a venue. |
| **Partial** | At least one specific item passes, and at least one does not. |
| **Fail** | No specific item passes, or the reply names no specific item. |

The owner may overrule any single score. The owner writes the reason in the sheet's note column.

### The veto rule — thresholds proposed for the owner to set

An arm is **vetoed** as a primary candidate if either condition is true across its six sessions
(3 scripts × 2 replicates):

- **V1 — fabrication.** At least one **Fabricated** back-reference in two or more sessions.
- **V2 — instruction-holding.** More than 20% of its scorable H items (Held plus Broken) are
  **Broken**.

These numbers are proposals. The owner confirms or replaces them when approving this rubric, and
after approval they do not change. Among the arms that no veto removes, the owner decides on the
automatic outcome, the telemetry and the notes (Phase 4).

---

## Blinding procedure (AC-6)

1. After the last Phase 2 session, a script exports one sheet per session. Every sheet gets a
   random four-letter code, and the sheets are put in random order.
2. The mapping from code to arm and replicate goes into one file. Its SHA-256 is posted on
   FRE-1517 before the owner scores anything. The file itself is not posted.
3. The owner scores every sheet.
4. The mapping file is posted after the last score. Its hash must match the posted hash.

The export script is built in Phase 3. It is not part of this approval.

## Scoring sheet columns

`code` · `turn` · `item` (H:I1 … H:I8, R:t<n>, G) · `score` · `note`
