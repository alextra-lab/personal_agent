# Last session — 2026-07-21 (ADR-0122 amended + shipped; a bad day for master's certainty)

## Doing / discussing (≤5 sentences)

Dispatch was re-enabled and the day ran hot: **ADR-0122 was amended and its whole implementation chain
(T4/T5/T6) built, merged and deployed**, plus FRE-923 and FRE-925. Alongside it, **AC-7 failed live** on
its first real run and a family of silent-delivery defects kept surfacing — five new tickets came out of
one morning. **The through-line worth carrying: master was confidently wrong four separate times, and the
owner caught every one.** Two findings exist ONLY in that conversation and are written up below — the
**cache regression** and the **under-declared model caps**.

**Start here:** PR **#613** (FRE-928) is green and **ungated for hours** — the watcher sent `/master 613`
at 16:47 and it never arrived. Gate it first.

## Commits — the story behind the last ~15

- **ADR-0122 amendment (#606)** — owner-directed: the artifact-builder card moves from the build boundary
  to **turn start**. Master measured why: the card fired **117s** into a turn (55s perplexity + 43s silent
  planning), by which point the owner had put the phone down and the socket dropped. The card check itself
  takes **<1ms** — it was only ever *positioned* late.
- **T4/T5/T6 (#608, #611, #612)** — signal → turn-start ask → budget sizing. All merged and **deployed**
  (gateway rebuilt ~17:5x, verified inside the container, health green).
- **ADR-0123 (#609)** — new: turn progress surface. Its finding is sharp and verified: the transport has
  **zero** references to inference (0 vs 14 for tool events), so the system is silent precisely where it
  works longest. Sits **upstream** of FRE-928, not parallel.
- **#602 (FRE-925)** — the recall test, finally fixed and **Done**. Root cause was *displacement* (rows
  crowded out of a global top-50), not "stale data matching" — a misdiagnosis four handoffs repeated.
- **#600 (FRE-923)** — dispatch delivery atomicity, deployed and self-evidencing (`attempts=1` in the
  first live dispatch proved the merged code was the running code).

## Worktrees — anything special

- **build1** (cc-1build) — on FRE-928, PR **#613 open, green, UNGATED**. This is the first thing to do.
- **build2** — idle, queue empty. The ADR-0122 chain is complete.
- **Note:** at ~10:44 the **primary tree `/opt/seshat` was found checked out on a feature branch** with
  build2 in detached HEAD — a build took the branch in master's tree instead of its own worktree. Master
  restored `main`. Worth watching; not ticketed (one occurrence).

## Plan position + drift

MASTER_PLAN §0 rewritten twice as reality moved. ADR-0122's chain is now shipped but the ADR **does not
close** — AC-7 is live-only and still unproven. FRE-921 sits in **`Verify Failed`** with the full timeline.

**Two findings that live nowhere else — read these before touching the prompt path:**

1. **A cache regression master approved.** FRE-931 appends a per-turn planning note into the **system
   prompt** (`executor.py:123`), and the system message is exactly where the cache breakpoint goes
   (`litellm_client.py:189`, `:384`). At the gate master verified the note stays out of
   `static_prefix_hash` — *telemetry identity* — and reported that as "caching is preserved". **Those are
   different things.** Measured after deploy: the baseline is **fine** (prefix hash unchanged at
   `e6ddc4b50c52f2be`, reads of 8.9K–12.2K/turn, zero re-writes), but the owner's 6-turn test contained
   **no artifact-build turn**, so the note never fired and **the claim is still untested**. It can only
   manifest on a turn where `artifact_build_intent` fires.
2. **The catalog's `max_tokens` are policy values, far below real provider ceilings** — `claude_sonnet`
   declares 32768 against Sonnet 5's real **128K**; `claude_haiku` declares 4096 against Haiku 4.5's real
   **64K**. Retrospectively harmless (the old flat 32768 never exceeded a real limit, so nothing was
   broken in prod). **But FRE-931's clamp now enforces those numbers as hard caps** — picking Haiku yields
   a **4096-token** artifact, ~16× smaller than the model can produce, and the plan is sized to it too.
   **Not ticketed** — every fix touches prompt bytes, and the owner ruled that out pending their decision.

**Owner constraint, standing until lifted: do NOT make changes that affect the prompt cache.**

## Answers for the fresh start

- **Why is #613 ungated?** The watcher sent it at 16:47:37, logged `gating_send`, and the ledger shows
  **nothing unconsumed** — a swallowed send recorded as delivered. Second occurrence today (the first cost
  #602 nine hours). Tracked as **FRE-939**. **Do not trust the trigger — run `gh pr list`.**
- **Can I run AC-7?** Everything it needs is deployed. But **fix the caps first**, or the live run produces
  a needlessly small artifact and you'll misread the clamp as a sizing bug. One artifact-build turn would
  answer AC-7 *and* the cache question together.
- **Why is FRE-921 Verify Failed rather than unproven?** The owner's call and it was right: AC-7 says
  "fails if any leg breaks", the card leg broke, and a cause-based excuse ("the socket was down") does not
  exempt an outcome-based criterion.
- **Why did no card ever appear?** Three distinct mechanisms, all on FRE-928: (a) no socket at emit →
  instant default, bypassing the waiter's own 60s timeout; (b) a **reconnect** evicting the old
  registration → pending decision killed 11.6s early; (c) the server holding a **half-open** connection it
  believed live and pushing the card into a socket with no reader. (c) is the root — the client wiring is
  correct and should not be changed.
- **Is the model routing broken?** No. Every call ran cloud (`claude-sonnet-5`, `claude-haiku`,
  `gpt-5.4-mini`). The `131K` the owner saw was the **sessionless config endpoint** returning the role
  default; the in-turn path correctly reports 200000. FRE-926 owns it.
- **Why did the harness build an artifact unasked?** A 20-char conversational request in a session that
  already held an artifact from two days earlier. `prompts.py` has **zero** artifact guidance — the tool
  descriptions say *which* tool, never *whether*. FRE-932.

## The thing worth carrying forward

**Master was wrong four times today and the owner corrected every one:** the tool-gate cap (asserted the
`conversational` limit without checking the turn's actual classification), "your socket was down" (read a
`tail`-truncated log slice as complete), "no grace window exists" (the timeout was already there, being
bypassed), and "caching is preserved" (verified the adjacent claim, not the actual one).

They share one shape: **reaching for a mechanism that fits the symptom and reporting it as established.**
The corrections never came from self-review. The one diagnosis that held — the PWA session-continuity bug
— is the one where master stopped proposing, went looking for what it could observe, and then said plainly
*where the evidence ran out* instead of filling the gap.

Second, smaller, and repeated **three times**: master put actionable requests in **ticket comments**, which
are the durable record channel and explicitly not instructions. Builds correctly ignored all three. If
master wants something done, it goes in the ticket **description** or a direct message.
