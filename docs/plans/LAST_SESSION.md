# Last session — 2026-09-05 (full day, into 2026-09-06)

## Doing / discussing  (≤5 sentences)
The owner asked Seshat to compare three ways to compute a median. The answer was confident, well
structured, and fabricated. That question consumed the rest of the day and produced the first
reproducible model-capability result this project has: on `qwen3.6-35b` the sub-agents invoke no
tools, and on `qwen3.8-flash-next` they do. The live binding is `qwen3.8-flash-next` for both
`primary` and `sub_agent`, and the deployed container matches `main` in every value. Nothing is
half-merged.

## What was decided and why

**A fresh nonce is the only honest test of execution.** Telemetry cannot separate a model that ran
code from one that described running code. `tool_call_completed` carries `success` and `tool_name`.
`run_python_finished` carries `exit_code`. The scratch directory is empty. Master spent about an
hour in that data and could not answer the owner's question. The probe answers it in ten minutes:
generate a string that never existed, ask for its SHA-256, and hold the true digest. Priors are
useless against a fresh input, so there is no partial credit. This is now FRE-1416, merged.

**A passing primary says nothing about the sub-agent path, and the difference is the whole point.**
The first probe routed SINGLE and passed. The owner said "but it did not use a subagent". The
second probe uses the "three different ways, compare and evaluate" phrasing that routes HYBRID, and
asks for three algorithms, so **each digest becomes a per-sub-agent proof**. A result lost between
a worker and synthesis becomes visible instead of silent. Both shapes are needed. Do not infer one
from the other.

**The model is the variable, not the code.** The same grant, the same loop and an equally
well-formed 4-task plan produced zero tool calls on `qwen3.6-35b` and three real executions on
`qwen3.8-flash-next`. FRE-1388 and FRE-1389 work. The owner caught the fabrication by comparing
quoted benchmark durations against the sub-agents' own lifetimes: the reported times were longer
than the workers that reported them. **Nothing in the system made that comparison.** That check is
FRE-1417, scoped narrowly on the owner's instruction, and it is the only open item from the
incident that is not yet built.

**A catalog correction is not a binding change, and merging them together cost a day.** The swap to
`qwen3.6-35b` needed two independent things: the bindings, and a corrected catalog description of
what the backend serves. They travelled in one PR. When the swap was reverted, the correction was
stranded with it, and `main` kept a stale model id that names a backend which no longer exists —
FRE-1363's recorded blocker, and the exact shape of FRE-1317. PR #1061 was rescoped to the catalog
alone and merged. **Split them next time.** The catalog says what the server serves; the bindings
say what we point at. Only the second is a decision.

**A test must not re-pin a constant it does not own.** Three tests broke on the corrected catalog.
Two needed a genuinely smaller-window deployment, and PR #1067 had already chosen well:
`gpt-5.4-mini` at 128000, and `embedding` at 32768 for Stage 7. That work was taken verbatim and
credited. The third was re-pinned in #1067 from 131072 to 262144; it now reads the value from the
catalog instead, the way its own sibling already did. **The distinction that governs this: a
*binding* pin must stay literal, because catching the next swap is its entire job. A *catalog* value
must not be, because it is not that test's subject.** The binding pin at
`test_turn_status_context_max.py:69` is untouched and still bites.

**`run_python` is the one tool that produces facts and the one we cannot audit.** The probe works
around this by making the answer carry its own proof. It does not close the gap, and it only works
on a question built for it. An ordinary turn remains unauditable after the fact. ADR-0138 D2
compounds it: tool output is not an admissible citable source, so a fabricated "I executed it"
passes the grounding contract untouched. Filed as FRE-1419, with the three decisions it needs —
where the payload lives, the truncation policy, and the redaction rule — named rather than assumed.

## Where master was wrong, and what the shape was

Four errors, and three share one shape.

**A truncated view read as an absence.** Master reported "no DONE row exists, the `finally` never
ran" from `ORDER BY seq DESC LIMIT 8`, which returned seq 33 down to 26. The DONE row is at seq 23.
The whole FRE-1403 escalation rested on that. **This is the second time this exact shape has caught
master**, and the ADR seat found it by checking rather than building on master's account. FRE-1403
returned to Medium and the record was corrected in the description, not buried in a comment.

**A seat reported as working while wedged, three times.** Master read the resolver and
`dispatch_state.json`. The truth was in `dispatch_execute`'s `launched=False outcome=seat-busy`.
About two hours of Urgent work was lost. The instrument that answers "is this stream running" is
the execute record, not the intent record. FRE-1405 exists because a stall the daemon detects only
reaches a log line.

**A credential declared dead without checking `pass`.** The owner asked "Why not?". The password
was in `seshat/POSTGRES_PASSWORD` and worked. Master had used the false conclusion to skip the
ADR-0074 joinability gate on a migration. The probe then ran green.

**A cause named twice, wrong both times.** First "the swap broke the planner", then "not the model,
it is FRE-1390". The truth is both and neither: FRE-1390 created a latent 1024-token cap, and
`qwen3.6-35b` was the first model whose plan did not fit inside it. **A latent defect plus a
model-sensitive trigger reads as two competing causes and is one.** FRE-1413 removed the cap rather
than raising it, and raised `planner_timeout_seconds` with it — otherwise token truncation becomes
timeout truncation.

The common shape in the first, second and fourth: **master answered from the instrument nearest to
hand rather than the one that could see the failure.** That is the same finding as yesterday's
health probe, one layer up.

## State at handoff

`primary` and `sub_agent` both resolve to `unsloth/qwen3.8-flash-next`. The container and `main`
agree in every configuration value; the only differences are trailing comments. Health is 200 with
database, Elasticsearch, Neo4j, second brain and MCP gateway all connected. The PWA serves
`seshat-v55-context-meter-cold-lane`.

Open and needing the owner: FRE-1406, FRE-1407, FRE-1408, FRE-1409, FRE-1410 and FRE-1419 are all
in `Needs Approval` and all change `src/`. FRE-1408, FRE-1409 and FRE-1410 are the ADR-0143
implementation chain and FRE-1403 is blocked on all three.

PR #1067 is a draft on purpose. It holds 18 correct binding pins for whenever the swap returns.
Do not merge it while the binding is `qwen3.8-flash-next`.
