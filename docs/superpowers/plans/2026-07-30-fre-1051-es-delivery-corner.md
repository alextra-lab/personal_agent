# FRE-1051 — Elasticsearch delivery corner: measure it first, then close the real gaps

- **Ticket:** FRE-1051 (Urgent, Tier-1:Opus, stream:build1) — Observability Foundation
- **Backing ADR:** ADR-0090 telemetry surface contract (emit · mapping · dashboard — no delivery corner)
- **Related:** FRE-533, FRE-989, FRE-1039 · **Policy touched:** FRE-375, ADR-0114 AC-5
- **Revision 2** — after codex adversarial plan-review. My rev-1 diagnosis of the
  `captains_log` half was wrong and my rev-1 recommendation was wrong. Both corrected below.

---

## 1. What I measured

Oracle = Postgres `api_costs` (append-only, independent write path). ES = `agent-logs-*`.
Master's figures reproduced exactly: 07-23 25/144 · 07-24 103/103 · 07-25 211/211 ·
07-26 105/201 · 07-27 172/361 · 07-28 283/283.

**The loss is whole-hour, all-or-nothing.** 07-27 PG hours 04(189) 12(18) 18(68) 20(20)
21(66); ES has 12/18/20/21 at exact parity and hour 04 at zero.

**The pipeline was healthy throughout every lost hour** — 2,613 docs in lost 07-27 h04,
`metrics.sampled` at its usual ~1,420/hr.

**The lost traces have zero ES documents cluster-wide.** Verified with individual `term`
queries against `_count` on **all** indices (not just `agent-logs-*`), so this is
agg-independent and closes the "different index prefix" alternative. Distinct-trace
cardinality per day is 4,006 / 1,573 / 3,355 — far under the 30,000 agg size I used, so
no bucket truncation.

**Exact accounting.** Rows whose trace has zero ES presence: **119 / 96 / 189** on
07-23/26/27 — precisely each day's shortfall — and 0 on all three clean days. By purpose:
`study` 285, `captains_log` 119. Total 404.

**The `captains_log` 119 are a backfill, not live traffic.** Splitting by session UUID
version: `captains_log` is 269 rows on v4 (random ⇒ live) and **exactly 119 on v5**
(deterministic ⇒ derived/replayed). 119 is precisely the lost count, and the lost rows'
*sessions* also have zero ES documents. So both families are the same shape: a process
whose telemetry never reached ES at all.

### Every alternative explanation, and why it is out

| Alternative | Verdict |
|---|---|
| Retention / ILM eviction | Out — lost h04 and delivered h18 share one daily index holding 2,613 docs. (Codex notes the checked-in template sets no `index.lifecycle.name`; I will verify live ILM in Step 1 rather than assume.) |
| `trace_id` mapping / `index:false` | Out — `agent-logs-2026.07.27` has 6,894 docs with `trace_id` present and same-day delivered traces query fine. |
| Different index prefix / endpoint | Out — cluster-wide `_count` across all indices returns 0 for lost traces. |
| Log-level filtering | Out for the window — root logger is DEBUG (`logger.py:196`); FRE-989's `debug`→`info` landed **2026-07-29**, after the window, so it cannot skew these counts. |
| Sampling | Out — no sampling logic in handler/logger/cost-tracker. `metrics.sampled` is an event name, not evidence. |
| One-time level change explaining the pattern | Out by shape — loss on 23, clean 24–25, loss 26–27, clean 28 is non-monotonic. |

**Producer-population caveat (codex, verified):** `dspy_gate.py` — the DSPy cost-ledger
path — landed 2026-07-29 in commit `594f8afc`. It did not exist during the measured window,
so post-FRE-989 days are not like-for-like with it and I will not compare across that seam.

### Cause: M1 only

**M1 — the process never attaches the handler.** `add_elasticsearch_handler` has exactly
one call site: `service/app.py:665`, inside the FastAPI lifespan, and it is *conditional on
`connect()` succeeding* (line 664). `gateway/app.py:148` builds a handler purely to harvest
`.es_logger.client` for queries and never attaches it. No script attaches it. That accounts
for both families: the study harness (`scripts/study/categorizer.py`) and the
captains_log backfill are standalone processes.

**M2 (`asyncio.to_thread` emit) is falsified as a cause of this loss.** `emit()` does
silently drop off-loop records (`es_handler.py:170-175`) and the codebase documents that
hazard in four places, but the reflection cost event is **not** emitted in the thread: the
worker only fills `_cost_sink`, and `gated_dspy_job.__aexit__` → `_settle_completed_job` →
`record_vendor_cost` runs on the caller's loop (`dspy_gate.py:281,347,416`); the manual
fallback is awaited on the main loop too (`reflection.py:510`). And `dspy_gate.py` postdates
the window. **M2 stays on the list as a confirmed latent defect, not as this cause.**

**Both original ticket hypotheses measure zero loss.** Unreferenced-`create_task` GC would
scatter loss within a process; buffered-shutdown drop would truncate a tail. Observed loss
is entire processes with zero documents. Both remain real latent hazards.

### New finding, and it reframes half this ticket

The 285 `purpose='study'` rows are in **prod** `api_costs`. **ADR-0114 AC-5 forbids this**:
the study "must never touch prod Neo4j/ES/Postgres (the FRE-375 substrate-isolation line
applies unchanged)", with the named risk *"Study writes to prod substrate by accident |
High"* and a two-sided zero-prod-writes proof obligation. So **~70% of the "lost events"
is not a telemetry-delivery bug at all — it is a substrate-isolation violation.** The
correct remedy there is isolation, not delivery. Severity as measured: 285 ledger rows over
three days; I have not audited whether the same study also wrote prod Neo4j/ES, which is
the question that sets the real severity.

---

## 2. Where I was wrong in rev 1

1. **Attributed `captains_log` to M2.** Wrong — corrected above, by my own v4/v5 evidence
   and codex's `_cost_sink` trace.
2. **Recommended attaching ES delivery inside study/eval processes (option A).** Wrong —
   it contradicts FRE-375 and ADR-0114 AC-5, and would have made prod ES mirror a
   violation instead of surfacing it. `process_role` tagging makes pollution *filterable*,
   not *absent*, and every existing dashboard/probe would silently include it.
3. **Claimed "abrupt termination → no loss" as provable.** Wrong — an in-memory queue
   cannot survive process death. Only graceful drain is provable; a kill test must document
   the loss boundary instead.
4. **Claimed the probe could read `dropped_overflow`.** Unfounded — an in-process counter
   is not observability without an export path.

---

## 3. Revised recommendation on the design question

**Do not ship study/eval logs to prod `agent-logs-*`.** Instead:

- **Attach + drain** in the two genuine production processes: `service` (replacing the
  current attach with a drained lifecycle) and the standalone `gateway` (which attaches
  nothing today).
- **Scripts/evals declare a delivery mode** — isolated substrate by default, explicit
  opt-in with a distinct prefix and provenance if prod is ever intended.
- **Replace the proposed "handler missing" guard** with a guard that checks the *configured
  delivery policy*. "No prod handler" is correct behaviour in an isolated script.
- **File the ADR-0114 AC-5 violation separately** — it is a policy breach with its own
  owner decision, not a fold-in here.

---

## 4. Scope — this PR is the measurement corner only

Codex's split, which I accept. The ticket's own ordering ("WHAT TO WORK OUT FIRST, IN
ORDER") says measure before choosing a fix, and the handler is on the root logger of every
process — that blast radius deserves its own review.

- **PR1 = this ticket.** The delivery probe (the corner ADR-0090 lacks) + the corrected,
  evidence-backed diagnosis. Narrow: the cost family against its real oracle, plus live
  prefix/mapping/ILM verification, reported split by `purpose` and producer. No handler
  rewrite, no attachment change, no script change.
- **PR2 (follow-up ticket).** Handler reliability: single owner-loop consumer, semaphore
  removed, referenced consumer task, graceful drain, internal-diagnostic exclusion to kill
  the `es_logger.py:173` feedback loop, **event-time captured at emission** so a backlog
  crossing midnight cannot land in the next day's index, declared overflow policy, exported
  counters. Framed as closing latent hazards.
- **PR3 (follow-up ticket).** Prod gateway attach + drain; service lifecycle drain before
  `disconnect()`.
- **PR4 (follow-up ticket).** Study/eval isolation — the ADR-0114 AC-5 violation.

**Deliberately cut from PR1:** all-family oracle expansion, queue rewrite, `process_role`
stamping, CostTracker guard, script/gateway attachment, ADR-0090 amendment.

---

## 5. Acceptance criteria for PR1, honestly split

| # | Criterion | Proven by | When |
|---|---|---|---|
| AC1 | Delivery ratio for the cost family measured against the Postgres oracle over a stated window | Probe reproduces §1's table, split by `purpose` | pre-merge (operational run, not CI) |
| AC2 | The probe distinguishes the three causes of a clean zero (no data / wrong field / emitted-and-lost) | Unit tests over fixtures for each case | pre-merge |
| AC3 | Live prefix, `trace_id` mapping, and ILM attachment verified rather than assumed | Recorded `_field_caps` / `_cat/indices` / ILM output in the ticket | pre-merge |
| AC4 | A family with no usable oracle reports `UNVERIFIABLE`, never a silent pass | Unit test | pre-merge |
| AC5 | Probe is runnable as a standing check and exits nonzero on breach | Exit-code test | pre-merge |

**Not claimable pre-merge, and not claimed:** the ~100% after-figure (needs the PR2/PR3
fixes deployed plus real traffic); standing visibility in practice (needs deployed
scheduling + alert routing + one observed run); any statement that delivery is now
complete. AC1 is an operational measurement against live Postgres/ES, reproducible only
while those indices are retained — it is evidence for the ticket, not a CI proof.

---

## 6. Steps

1. **Probe** `scripts/monitors/fre1051_delivery_ratio.py` — cost family vs `api_costs`,
   `--since/--until/--json`, split by `purpose`, `UNVERIFIABLE` verdict, nonzero on breach.
   → verify: reproduces §1's table.
2. **Unit tests first** over fixtures: each of the three zero-causes, the `UNVERIFIABLE`
   path, the exit code. → verify: fail first, for the stated reason.
3. **Live verification** of prefix/mapping/ILM; record outputs. → verify: pasted in ticket.
4. **Diagnosis writeup** — this document, as the durable record that both ticket hypotheses
   and M2 measure zero, and that M1 + the ADR-0114 violation are the real content.
5. **File three follow-up tickets** (PR2/PR3/PR4 above) + the ADR-0090 fourth-corner ticket
   for the adr session.
6. **Gates:** `make test` · `make mypy` · `make ruff-check` + `ruff-format` ·
   `pre-commit run --all-files` · code-review (`low`–`medium`: one new script, no src
   change) · security-review not indicated for a read-only probe unless it grows creds
   handling.

---

## 7. Self-review outcome — six real defects, all fixed on-branch

The `code-review` skill is `disable-model-invocation` and could not be run from this
seat (the owner's own invocation died to an upstream 529, and the authorization does not
persist across turns). Per master's direction the review was done with adversarial
agents instead: one correctness reviewer told to break the code, one mutation-testing
pass on the tests, plus `security-review`. **They found six genuine defects, including
one that undercut this ticket's own thesis.** Every one is fixed here.

1. **The zero-attribution machinery was dead code.** `classify_zero`, `ZeroCause` and
   `field_is_mapped` were written, unit-tested, and never called by `collect_report`. So
   a renamed `event_type` field would have reported "0% delivered, 144 lost" — the exact
   `FIELD_ABSENT`-as-`EMITTED_AND_LOST` conflation the module docstring forbids,
   reproduced inside the probe built to end it. Now wired in, with `field_absent` as a
   distinct status that alarms without blaming delivery, and the mapping lookup is
   skipped on the happy path.
2. **`--json --help` exited 0 with an empty stdout.** Argparse ran inside the
   descriptor redirect, so usage went to stderr. A wrapper capturing stdout and gating
   on the exit code would score that as a silent success — the failure class this probe
   exists to eliminate. Parsing now happens outside the redirect.
3. **Over-delivery was buried in the ranking.** Sorting on the raw ratio ascending put a
   ratio of 1.37 *below* a clean 1.00, so the anomaly the docstring promises to "surface,
   not clamp" appeared under passing families. Ranking is now by status severity then
   deviation from perfect.
4. **A substrate failure was indistinguishable from a breach** — both exited 1. An
   unreachable Postgres now exits 70, so "the probe is broken" and "delivery is broken"
   route to different triage. The documented `64` for bad arguments was also wrong:
   argparse exits 2.
5. **A tautological test.** `test_unverifiable_is_named_not_shown_as_zero_percent`
   asserted `"UNVERIFIABLE" in out.upper()` against the whole report, which the header
   line `Overall: UNVERIFIABLE` satisfied on its own — so a mutation rendering the cell
   as `0.0%` stayed green. Now asserts the family's own row.
6. **An order-dependent test.** The JSON-payload test parsed the entire captured buffer,
   so it passed only when an earlier test had already triggered the config import; run
   alone it failed on conftest's setup logging. Now scoped to `_emit`'s own writes.

Five surviving mutations were closed with tests (over-delivery in the window verdict, the
`lost` floor, the ranking tie-break, `classify_zero` precedence, the floor comparison at
exactly `min_ratio`), and the CLI went from **zero** coverage to 14 tests.

**Two findings I did not act on, deliberately.** A family where the oracle is empty but
ES holds documents collapses to `unverifiable`; that is the conservative direction (it
never claims health) and inventing a status for it now would be speculative. And the
`finally` in `_measure` can mask an original exception with a cleanup error — ordinary
Python semantics, no observed impact, and worth its own change if it ever bites.

**What the reviewers confirmed rather than broke**, which matters because these were my
two stated worries: no combination of families can report `pass` without a genuinely
verified one, and the ES/Postgres windows cover identical instants — checked against a
live Elasticsearch, where a bare `YYYY-MM-DD` on `gte`/`lt` yields exact UTC midnight and
`lt` does not round up. The three days of exact parity (103/103, 211/211, 283/283) are
the empirical corroboration.

**One process note worth carrying.** The first version of the `--help` test used
`capsys`, then `capfd`; both passed with the defect deliberately reinstated, because
neither observes an `os.dup2` swap. Only a real subprocess distinguishes them. The test
is a subprocess for that reason, and I verified it fails with the bug restored — a test
whose failure mode I had not confirmed would have been the seventh defect.

## 8. Risks

- **The probe reads prod Postgres and prod ES.** Read-only, but it must use configured
  settings, never hardcoded URIs (FRE-375 pre-commit guard).
- **AC1 decays.** The historical window is only reproducible while those indices are
  retained; the writeup records the figures so the evidence outlives the data.
- **Splitting delays the fix.** The measured loss is telemetry-only — no user-facing wrong
  answers — and the highest-value output is knowing which conclusions are unsafe, which
  PR1 delivers. But the loss continues until PR2/PR3 land, so they should be sequenced next.
