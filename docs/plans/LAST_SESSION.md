# Last session — 2026-09-07

## Doing / discussing  (≤5 sentences)
The owner said **"full steam ahead"** and the ADR-0145 chain finished: eleven tickets merged,
deployed and verified in one day. The afternoon turned into design work the owner drove —
sub-agent approvals, tool grants, `run_python` bounds — and five tickets came out of it. The
owner also ran live turns that produced the session's two most valuable measurements. Master
made two errors, both corrected on the record.

## What was decided and why

**"Unattended" is our implementation, not a property of sub-agents — and this reverses a
master argument.** Master argued the sub-agent tool grant should stay narrow because sub-agents
run with no approver. The owner asked whether Claude Code bubbles subagent approvals to the
user; it does — a subagent there borrows the user's approval channel. `_maybe_pause_for_constraint`
already exists at `executor.py:682` and `sub_agent.py` has **zero** occurrences of pause or
approval. So the gap is wiring, not architecture. **FRE-1461** is the enabler; **FRE-1463** (tool
grant) is blocked on it deliberately, not merely sequenced after.

**The four deferred tools are not one decision.** `search_memory` / `recall_personal_history`
are internal reads whose objection is provenance (FRE-1338/1302/1303). `web_search` / `fetch_url`
bring untrusted content and real spend into an agent that acts on it. FRE-1463's AC-1 refuses a
block grant. Fan-out is the hard part: 6–8 sub-agents per turn means a naive per-call prompt
produces 6–8 prompts, which the owner would switch off — so FRE-1461 AC-4 tests at fan-out scale.

**Grounding is not broken; it was starved.** Every prior turn read `passed_count: 0`, and
FRE-1328 said that was structural. An owner turn using `web_search` produced
`tool_results_admitted=2`, `turn_evidence_class=citable`, and the **first non-zero `passed_spans`**.
The headline is the other number: of 16 spans, **8 came back `not_contained`** — a citation whose
source was found, read, and does not contain the claim. That is FRE-1327's defect measured through
the contract instead of inferred. Caveat recorded: `degraded_extraction=True` on that turn, so
8-of-16 is not yet a rate.

**Span extraction can be skipped, but not on the signal it looks like.** `turn_evidence_class`
is computed at `executor.py:2166`, *after* the verification it would gate. "No sources" is also
wrong — that turn had `source_count=10` and the claims simply matched none. The usable
pre-extraction signal is `tool_results_offered > 0 and tool_results_admitted == 0`. On FRE-1458.

**Master error 1 — gated FRE-1445 off a stale ref.** Fetched the branch at 09:13, the seat pushed
the cost-window fix at 09:21, master analysed the 09:13 ref at 09:32 and merged the head at 09:50.
Master told the owner a cost window was open that the seat had already closed, and asked them to
decide a trade that no longer existed. **The gate must read the commit it is merging.**

**Master error 2 — reproduced a documented trap.** Wrote `until ! pgrep -f 'bin/pytest'`, which
matches its own command line, and blocked itself for 35 minutes. That exact substring flaw is
written up in root `CLAUDE.md` and in FRE-1405's own description. Reading it was not enough.

**`run_python` has no execution bound.** Two sandbox containers ran a non-terminating
agent-written benchmark at 99.9% CPU for **2 days 3 hours** (from 09-05 09:14). They also
silently corrupted an owner benchmark taken while they ran. Master removed them; **FRE-1462**
carries the fix. The owner's generalisation — *all* unattended tool execution wants bounds — is
in the ticket and should not be narrowed to `run_python`.

## Worktrees — anything special
Nothing unpushed. `telemetry/dispatch_state.json.bak-163808` is untracked, pre-existing and the
owner's — leave it. The `adr` stream carries a **stale dispatch record** pointing at parked
FRE-1361 (20+ hours), which makes the daemon log a `no-pr-past-timeout` stall every tick.
Cosmetic; clear it only while dispatch is paused, since the daemon writes that file continuously.

## Sequence position + drift
The model/agent/subagent fast-track directive is **discharged** — ADR-0145 is complete. Three
seats mis-attributed the same stale searxng file in their own worktrees to `main`; master
corrected each time (FRE-1456). The Observability Foundation directive remains deferred by the
owner's own 2026-09-06 decision, not drifted.

## Answers for the fresh start

- **First task?** Adjudicate **FRE-1426**'s thirteen ACs — the chain has landed, so its
  precondition is met. ADR-0145 is still `Proposed`; the stated blocker on Accepted (D7's
  concurrency prediction) was measured by FRE-1449. Full detail is a comment on FRE-1426.
- **What is dispatchable?** Five new tickets are Approved and **deliberately unlabelled**:
  FRE-1461, 1462, 1463 (blocked on 1461), 1464, plus FRE-1458/1459. Both build lanes are clear.
  Recommended order: 1461 and 1462 in parallel, then 1463.
- **Why is FRE-1372 `Verify Failed`?** Master ran its probe; it printed `AC-1 held` and exit 0
  **vacuously** — zero graph nodes, `entity_extraction running_total=0.0`, `extraction_settled=False`
  on both arms. Re-running changes nothing until the probe fails loudly or the eval stack runs
  extraction. Do not re-run it as-is.
- **What is blocked on the owner?** FRE-1448's AC-3/AC-4, FRE-1427's AC-2/AC-3, FRE-1402's three
  probes, FRE-1414's iOS check. All need live turns master will not fire unasked.
- **Is `main` green and healthy?** Yes. Gateway rebuilt at 10:43, all five components connected,
  `check_config: clean` before every deploy today.
