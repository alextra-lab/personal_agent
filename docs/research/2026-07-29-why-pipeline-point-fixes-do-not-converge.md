# Why point-fixing the delivery pipeline does not converge

**Date:** 2026-07-29 · **Author:** explore session (read-only study) · **Requested by:** master, owner-directed
**Status:** Research finding + recommendation. No code, board or deploy state was mutated in producing this.

---

## The question

Nine point-fixes have shipped against the CI/CD ↔ Linear ↔ GitHub ↔ dispatch surface and it still breaks.
The question posed was not "list the bugs" but: **why does point-fixing not converge?**

Master offered a hypothesis and explicitly asked for it to be attacked rather than confirmed:

> Every failure is a **state claim that nothing verifies**. The board is the one artifact the whole
> process trusts to be true, and it can silently lie.

**That hypothesis is half right, and the half that is wrong is the half that determines the fix.**
The evidence below shows the claims *are* verified — repeatedly, by four separate reconcilers built for
exactly that purpose — and the pipeline breaks anyway. One of the failures live on the box right now is
a case where the verification ran, returned the correct answer, and the decision **discarded it and
logged the opposite**.

---

## Method, and where the evidence stops

| used | not used |
|---|---|
| `journalctl` orchestrator + gating-watcher, 2026-07-08 → 07-29 (27.8k lines) | **CC transcripts (377 files, 1.07 GB) — not read at all** |
| `git log` / `git show` on every fix commit | Elasticsearch — deliberately avoided (FRE-1051: 48–83% event loss) |
| Linear ticket bodies + state histories (14 tickets) | |
| `scripts/dispatch/` source, read directly | |

**Where my evidence stops, stated plainly:**

- I did not read the transcripts. Every claim here is from journald, git, Linear or source. Any claim
  about *what a session believed at the time* is therefore absent from this study — including master's
  own "I said dispatched without verifying" narrative, which I have taken on master's word, not verified.
- I did not establish how many of the 101 `seat-busy` outcomes were legitimate refusals versus wedges.
  That number is quoted in master's brief as an outcome; I make no failure claim about it.
- I checked `manual-continuation` (6 occurrences) before counting it and it is **not** a failure — it is
  the normal `KEEP`-ticket path (`launcher.py:54`). Master's brief lists it neutrally; I confirm that.
- One correction inherited from master and independently re-checked: the 15.2-hour `dispatch_blocked`
  episode on 07-25/26 is `reason=kill-switch`, the owner's deliberate cost-halt. 366 `dispatch_blocked`
  warnings exist in the corpus and they are **not** failures.

---

## 1. The finding master asked for: which fixes held

This is the analysis the brief identified as most useful and noted had not been done. Each shipped fix,
mapped to the failure it addressed, then checked for recurrence *after* its merge date.

| fix | merged | what it did to the failure | held? | evidence |
|---|---|---|---|---|
| **FRE-913** persistent seats | 07-18 | **removed the operation** — never kill a seat, so RC deregistration never has to be raced | ✅ **held** | clean regime change in the outcome series: `launch` dominates to 07-18, then `reuse` from 07-19 onward. **Zero relapses in 11 days.** |
| **FRE-923** delivery atomicity | 07-21 | **removed the failure** — verify each command landed; a partial send is retryable, not terminal | ✅ **held** | `delivery-failed` appears **once in the entire corpus** (07-20, the incident itself). Zero since. |
| **FRE-976** reconcile vs Linear | 07-25 | **made one fact authoritative** (Linear owns "is this in flight") | ⚠️ **partial** | the terminal-release path works. The non-terminal path still stalls — **28 stalls today**, see §2. |
| **FRE-825 → FRE-845 → PR #447** idle predicate | 07-06 → 07-08 | **improved the inference**, three times in 48h, then deleted it | ❌ **did not hold** | see §3 |
| **FRE-939** gating send confirmation | 07-23 | **added an observer** over an inference | ❌ **did not hold** | **24 `gating_send_unconfirmed` since it shipped; 2 escalations, both on one day.** 22 unconfirmed sends went unescalated. On 07-28, 7 of 10 sends were unconfirmed. |
| **FRE-922 + FRE-924** wedge detect + age escalation | 07-20, 07-21 | **added observers** over an inference | ❌ **did not hold** | on 07-29 `dispatch_seat_wedged` counted ticks 3→14 (06:35→07:31, 56 min) and `dispatch_held_too_long` fired **zero times**. |

### The convergence law this produces

> **Fixes that removed the operation that could fail, converged. Fixes that improved or observed an
> inference, did not.**

Two held, permanently, with zero relapse. Four did not. The dividing line is not effort, tier or
author: FRE-923 (held) and FRE-976 (did not) are both Tier-1 Opus tickets shipped four days apart.
The dividing line is *what the fix did to the uncertainty* — eliminated it, or measured it better.

And there is a proof-by-construction already on the board. **FRE-927** records that the two reconcilers
FRE-923 and FRE-924 built both key their clocks to the ticket, so a seat that fails every dispatch
against a churning queue "never accumulates three attempts on any one ticket and never holds any one
card long enough to trip the age threshold." Two observers, and the failure walks between them. Adding
a third observer adds a third blind spot — which is exactly what the 07-29 wedge did, unescalated,
for 56 minutes, nine days after the observers shipped.

---

## 2. The specimen — live on the box while this was written

`build2` has been stall-looping on FRE-1015 since **2026-07-28 21:37:23Z — over eleven hours** —
emitting every five minutes:

```
kind=stall reason=no-pr-past-timeout stream=build2 ticket=FRE-1015
```

**There is a PR.** #738, open, branch `fre-1015-topic-scoped-stance-enrichment`, created 05:21:33Z.

Worse: **the daemon knows.** Tracing `scripts/dispatch/orchestrator.py`:

1. Line 717 — `tracked_pr_open = _open_pr_exists("FRE-1015", runner)`. I ran that exact `gh` query as
   the daemon's own service user, authenticated, from the same working directory: it returns PR #738.
   The fact is determined
   **correctly**.
2. Line 735 — the fact is passed into `decide()`.
3. Line 475 — it is consulted in **exactly one branch**: `if normalized == "in review" and tracked_pr_open`.
4. FRE-1015 is not "in review" — master bounced it back to **Approved** at 06:26:17Z. So control falls
   through to line 487 and emits `reason="no-pr-past-timeout"`.
5. `grep tracked_pr_open orchestrator.py` → lines 387, 398, 420, 449, 475, 714, 717, 735. **It is never
   logged.**

So the pipeline asked the right question, got the right answer, threw the answer away, and printed its
negation 28 times today. The reason string does not describe the state — **it describes the branch that
fired.**

This is the counter-example to master's hypothesis in its purest form. The state claim *was* verified.
Verification was not the missing thing.

*(Adjacent, noted not chased: `_open_pr_exists` ends `... or bool(raw)`, which makes the branch-name
match dead code — any open PR the search returns counts. And a non-zero `gh` exit returns `False`,
making "I could not ask" indistinguishable from "the answer is no". Both are the fail-open-to-False
shape already recorded in memory as `fail_open_defaults_path_sprawl`.)*

---

## 3. The oscillation — five changes to one predicate in seventeen days

The single richest thread in the corpus. "Is this seat ready to receive a message?" has no authoritative
answer, so it has been guessed five different ways:

| # | date | change | outcome |
|---|---|---|---|
| 1 | 07-06 | FRE-823 ships `session_is_idle` with markers `"│ >"`, `"? for shortcuts"` | **markers never occur in a real pane.** Watcher skipped 100% of sends. Shipped with 47 green unit tests over **synthetic fixtures containing text that does not exist in production** |
| 2 | 07-06 | FRE-825 replaces them with the `❯` caret | false-negative fixed → **false-positive introduced** |
| 3 | 07-07 | FRE-845: busy markers substring-matched over the *whole* pane, so ordinary prose containing "Do you want" reads busy. **Master sat idle 3 hours with two PRs ready.** Fix: anchor to the active region | still a scrape |
| 4 | 07-08 | PR #447 **deletes the predicate** for master — "that heuristic is unreliable (the FRE-825 class)"; always send | the guess is abandoned rather than fixed |
| 5 | 07-23 | FRE-939: consequence of #4 — a send into a busy master pane is booked as delivered and lost. **PR 602 sat ungated nine hours.** Busy-awareness is re-introduced, now as post-hoc confirmation | 24 unconfirmed sends since, 2 escalations |

Steps 2 and 3 are the *same predicate failing in opposite directions*. Step 4 removes it. Step 5 puts it
back. That is not progress toward a correct answer — it is a system with no ground truth alternating
between the two error directions of a guess.

**And the ground truth partly exists, unused.** There are now at least **four** mutually inconsistent
readiness oracles in `scripts/dispatch/`:

- `pane_state.session_is_idle` — regex over `tmux capture-pane` (its own docstring: "best-effort,
  fail-safe = busy")
- `context_probe` — transcript file mtime, which **documents its own unreliability in its module
  header**: "mtime only advances when a line is appended, so a session … [reads] IDLE while genuinely
  BUSY. Treat idle as a hint, not a hard [signal]"
- Remote Control's busy status
- ADR-0116 channel delivery, which has real delivery semantics (`channel_delivery_failed` fires 9× on
  07-23) and is only partially adopted

The wedge detector's own message is the giveaway. `orchestrator.py:1079` defines a wedge as:

> `remote-control reports busy while the pane is idle`

**The system's alarm for this class is a disagreement between two unreliable oracles.** It cannot
say which one is wrong, because there is no third authority. That is precisely why it can log
fourteen consecutive warnings and escalate nothing: escalating would require knowing which oracle
lied, and nothing in the system can determine that.

---

## 4. Attacking master's hypothesis

**What survives.** The GitHub↔Linear half is genuinely an ownership problem and master has it right.
FRE-1011 documents 11–12 occurrences across 7 sessions of a PR *title* dragging a ticket backwards,
including two in one session with the rule loaded in memory, and one pair timestamped to two seconds.
Its own diagnosis is the correct one and worth quoting because it generalises: *"the rule is fighting
the grain of the work rather than the memory of the rule."* Nobody owns the ticket-state transition —
GitHub's integration takes it on a substring match, and master takes it deliberately, and they
overwrite each other. That is an ownership contract and it is missing.

**What does not survive.** "Nothing verifies it" is empirically false, and it points at the wrong fix:

- Verification was added four times (FRE-922 detector, FRE-924 age clock, FRE-939 confirmation,
  FRE-976 Linear reconcile) and the pipeline still stalled for 11 hours and wedged for 56 minutes this
  morning.
- FRE-927 shows two verifiers composing into a *gap*, not a guarantee.
- The FRE-1015 specimen shows a verified fact being discarded by the decision that requested it.

The deeper reason point-fixing cannot converge is compounding: **the pipeline's diagnostics describe the
code path taken, not the state observed.** So each new ticket is authored against a symptom description
produced by the same unreliable layer being fixed. FRE-939's own ticket body records master misreading
watcher silence as a wedge and writes it down "so nobody repeats it." A fix authored from
`reason=no-pr-past-timeout` would go looking for a missing PR that is sitting there open.

A state contract in master's framing — *who owns each transition, what verifies it, what happens when
they disagree* — would add a **fifth** reconciliation layer over the same four proxies. On this
evidence that is the intervention that has already failed four times.

---

## 4b. The Linear-side automation — added after owner review of the first draft

**This was a gap in the first draft.** The study treated the GitHub↔Linear integration as a fixed
hazard to be guarded against in code, and never asked the obvious prior question: *what is the
automation, and can it be turned off?* The owner asked it. The answer changes the recommendation.

### What is actually moving the tickets

Linear's GitHub integration links a PR to an issue by matching the ticket token in **any of three
fields — branch name, PR title, PR body** — and then applies the team's configured status transitions
on PR *drafted / opened / review-requested / ready-for-merge / merged*. Two of these transitions are
live here: **on open → In Progress**, and **on merge → Awaiting Deploy** (the latter was deliberately
retargeted from Done on 2026-07-04 — proof the settings page has already been used once).

Causation, proven twice from state history against PR creation timestamps, by **different fields**:

| PR | branch | token location | issue | moved | latency |
|---|---|---|---|---|---|
| **#737** | `pwa/cache-bump-phase-state` — **no token, correctly named** | **title only** | FRE-986 | Awaiting Deploy → In Progress | PR 05:12:18Z → move 05:12:20.9Z = **2.9 s** |
| **#416** | `fre-825-idle-detection-fix` — token for a *different* ticket | **body only** (`- Relates to FRE-823`) | FRE-823 | **Done** → In Progress | PR 23:06:07Z → move 23:06:10.5Z = **3.4 s** |

#416 is the more damaging shape: a bare prose cross-reference in a PR body pulled a **Done** ticket
back into In Progress, three seconds after the PR opened, on 2026-07-06. That is occurrence #1 of the
class FRE-1011 filed three weeks later — and it means the "keep the token out of the branch and title"
discipline was never sufficient, because the body alone is enough.

**The magic-word escape hatch does not solve this.** Linear's non-closing magic words (`ref`,
`references`, `part of`, `related to`, `contributes to`, `towards`) suppress only the **on-merge**
transition. Per Linear's own docs, a non-closing reference *"will still move the issue through other
statuses per Workflow settings"* — so it does not prevent the on-open → In Progress drag at all.

### It can be switched off, and mostly without code

Three independent levers, none requiring a commit. **I could not read the current values** — the MCP
surface exposes no integration or workflow config (`get_team` returns only name/icon/timestamps), so
these must be inspected in the UI:

1. **Team → Issue statuses & automations** (the git-automation block; Linear's own docs also call this
   Settings → Team → Workflow). Per-team, and FrenchForest is the only team. **Set the "on PR opened"
   event to no status.** This alone ends the entire backwards-drag class — every occurrence in evidence
   is an *on-open* move. Keep "on merge → Awaiting Deploy", which is wanted and correct.
2. **Branch-specific rules**, same page: regex target-branch rules that can override a default with
   **"no action"** (e.g. `^docs/.*`, `^pwa/.*`). The precision instrument if the blunt lever in (1) is
   too broad.
3. **Two personal toggles**, invisible to anyone auditing team settings and firing under the owner's
   own account: Settings → Account → **Code & reviews** → *"On git branch copy, move issue to started
   status"* and *"On open in coding tool, move issue to started status"*. Both should be **off** —
   agents copy branch names constantly.

### Why this matters beyond the fix

**FRE-1011's proposed remedy is at the wrong layer, and this study should say so.** It proposes a
pre-commit guard that infers whether a branch is a docs branch from its prefix, in order to avoid
tripping an automation that can simply be switched off. That is *writing code to improve a guess*
where a config change *removes the operation* — the exact distinction §1's convergence law says
separates the fixes that held from the ones that didn't. It also would not have caught #416, whose
branch was a legitimate delivering branch and whose trigger was the body.

Recommend FRE-1011 be **rescoped**: lever (1) as the fix, the guard downgraded to at most a warning.

This also gives **D2 its concrete content.** The board's `In Progress` means, per lifecycle-rules, *"a
session is building it now"* — a fact only master and the dispatch daemon know. GitHub cannot know it
and should never assert it. So the ownership rule is not abstract: **the integration owns exactly one
transition (on-merge → Awaiting Deploy) and no others.** Every other transition is master's.

---

## 5. Recommendation

**Yes to an ADR — but not the one master framed, and it should be two decisions, not one.**

Nine point-fixes not converging *is* evidence a contract is missing. Master's prior is right about that.
It is wrong about which contract.

### D1 — Decision provenance (the one the evidence weights most heavily)

> Every control decision in the dispatch surface must be a function of **named facts that are logged
> with the decision**, and a fact that could not be determined is `UNKNOWN` — never `False`.

Three consequences, each of which kills a documented failure rather than observing it:

1. `reason=` stops being a branch label and starts being the facts. `stall{pr_open=true,
   linear_state=approved, age=11h}` is a decision master can act on; `no-pr-past-timeout` is one that
   actively misleads.
2. `UNKNOWN ≠ False` ends the `_open_pr_exists` class — "I could not ask GitHub" stops being silently
   identical to "there is no PR". This is the same defect the project already named in memory as
   fail-open-to-False and, at the telemetry layer, as ADR-0128's reason for existing.
3. It makes the oracle disagreement *legible*: a wedge stops being "two sensors disagree" and becomes
   "RC=busy, pane=idle, transcript_mtime=340s, channel=unreachable" — from which escalation is a
   decidable question.

### D2 — Transition ownership across GitHub ↔ Linear

The narrower, conventional contract master proposed. Exactly one actor owns each ticket-state
transition; a PR moves a ticket only via an explicit declaration, never a substring match anywhere in
branch/title/body. FRE-1011 is this ADR's first child, not a standalone ticket.

### What should NOT go in the ADR

Three things are ready now and should not wait behind it — the first is a settings change, not a build:

- **Switch off "on PR opened" in the FrenchForest git automations** (§4b lever 1), and turn off the two
  personal move-to-started toggles. This is the single highest-leverage action in this document: zero
  code, reversible in one click, and it ends a class with 12+ recorded occurrences across seven
  sessions. Do this before anything else here.

- **FRE-927** (approved this morning) — the seat-keyed failure counter. It is the only filed ticket that
  measures the *seat* rather than the ticket, and it closes the exact gap that ran unescalated for 56
  minutes today.
- **The FRE-1015 stall specimen** — a small, provable bug (discarded fact + false reason string) that
  should be filed on its own today. It is a worked example the ADR can cite, and it is currently
  costing a stream.

### The one thing to carry into the ADR above all

Apply the convergence law as an acceptance test on every decision the ADR makes: **does this remove an
inference, or does it observe one better?** On three weeks of evidence, only the first kind holds. The
strongest single move available — larger than anything in this study's scope — is finishing the ADR-0116
channel migration so "did this message land?" is answered by an acknowledgement instead of by four
disagreeing guesses about a terminal rendering.

---

## Limitations

Stated once more, compactly, because a conclusion is only as good as its sampling:

- **The transcripts were not read.** If the recurrence has a component that lives in session reasoning
  rather than in daemon behaviour, this study cannot see it.
- The convergence law rests on **six fixes**, of which two held. It is a strong pattern on a small n,
  and it is a *correlation with a mechanism*, not a controlled result.
- The FRE-1015 specimen is traced through source reading plus a live reproduction of the `gh` query. I
  did **not** attach a debugger to the running daemon; the inference that line 487 is the branch taken
  follows from FRE-1015's Linear state history (Approved since 06:26:17Z) and the code, not from
  observing the process.
- `dispatch_blocked` (366) and `manual-continuation` (6) were checked and excluded as non-failures.
  `seat-busy` (101) is uncharacterised.
- **§4b's remedies are unverified against the live configuration.** I established the mechanism from
  Linear's documentation and proved the *effect* twice from state history, but the MCP surface exposes
  no workflow or integration config, so I could not read what the FrenchForest team's git automations
  are currently set to. The settings paths in §4b are from Linear's docs, which describe the GitLab
  integration's page in more detail than the GitHub one; the GitHub page is the same integration model
  and the same per-team workflow settings, but **confirm the exact labels in the UI** rather than
  trusting my transcription. What is *not* in doubt is the effect: two PRs, two different trigger
  fields, two status changes inside three seconds.
