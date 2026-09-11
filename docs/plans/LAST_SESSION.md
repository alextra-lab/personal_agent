# Last session — 2026-09-11 evening

## Doing / discussing  (≤5 sentences)
A strong day. The ADR-0150 chain started and its first ticket is merged, the study is settled
rather than drifting, and four gates ran clean. **The study does not run again until ADR-0150's
chain lands** — the owner decided this twice and it is now recorded on FRE-1487. The deploy hold
is lifted and 31 held commits are live. Pick up at the gate: build1 is on FRE-1489, build2 on
FRE-1493, and both are the work the owner called the most important this week.

## What was decided and why

**The study is settled, not paused.** The owner said it plainly: *"It's pointless to run the
study until the prompt adr has been completed."* They had already said *"run the study after the
prompt adr's delivery"* hours earlier. Master kept re-opening it as a live decision and once
offered the owner their own conclusion back as a recommendation. Do not re-raise it. Query 1
stands as the baseline and already answered its question.

**A gateway rebuild does NOT revert the study limits.** Part of the morning's hold rested on a
false premise. Verified in the mechanism: `Dockerfile.gateway:70` is `COPY config/ config/` and
the build context is `/opt/seshat` itself, so the dirty working tree ships into the image;
`docker-compose.cloud.yml:307` is `env_file: /opt/seshat/.env`. Two rebuilds today, all five
limits intact both times, checked inside the container. There is no restore dance to fear.

**An ADR's implementation table outranks a ticket's re-derived acceptance criteria.** FRE-1492's
AC-2 demanded that `finish_reason = None` and `content_filter` both force a `ledger`. The build
seat did not implement it and argued ADR-0150's AC-3 did not require it — that argument is wrong,
AC-3 says exactly that. The conclusion survived on better evidence: the ADR's own table gives T1
*"the `length` row"* and T3 *"every row of the validity table except the `length` row"*. Merged on
that basis. **A hole stays open until FRE-1494: a `content_filter` finish with valid content still
reports `synthesized`.**

**The tautological-test pattern cost three bounces on FRE-1485.** A test that builds its expected
string as a literal and asserts the literal against itself, importing production code it never
calls. It passes against any implementation. The fix that finally worked: extract the production
string-builder into a pure helper, assert exact equality against it, and add a second test that
drives the real `Orchestrator`. Watch for this shape at every gate.

**Codex plan-review earned its cost twice today.** On FRE-1485 it caught wrong tool-iteration
arithmetic and an invented pause action before any code was written. On FRE-1492 it caught that
the `length` check had to run before FRE-1399's `round_texts` bookkeeping, or a cut reply gets
repeated inside its own ledger.

## Worktrees — anything special
build1 on `fre-1481-...` (merged, worktree-held) and build2 on `fre-1492-...` (merged,
worktree-held) — harmless, the branches cannot be deleted while checked out. Four untracked
`*.bak-*` files at root: two are the owner's/study's, two are master's `dispatch_state` backups
from unwedging build2.

## Sequence position + drift
**The working tree is intentionally dirty and this is not drift.** `config/model_roles.yaml`
carries the study's `max_tokens: 8192` / `default_timeout: 600`, by owner direction. It must stay
until the owner says otherwise, and — see above — it ships into every rebuild.

`Awaiting Deploy` holds 16 tickets, most from previous days. That is this board's normal parking
state for work whose live check needs a turn nobody has fired, not a queue master created.

## Answers for the fresh start

- **First task?** Gate whatever is at the gate. Do not ask the owner about the study.
- **What is live?** 31 commits, deployed 19:41 and again 19:50. FRE-1480, FRE-1481, FRE-1485 and
  the `build_fingerprint` fix are all running. **FRE-1492 is merged and NOT deployed.**
- **How do I check what is deployed?** `curl -s localhost:9001/health` now returns a real
  `build_fingerprint`; compare it with
  `uv run python -m scripts.eval.gateway_freshness --print-fingerprint`. It read `unknown` on
  every cloud gateway until today — `docker-compose.cloud.yml` never passed the build arg that
  `docker-compose.eval.yml` did.
- **Should FRE-1492 deploy alone?** Master's recommendation, given to the owner and not yet
  answered: wait for FRE-1493. T2 rewrites the same file, and a fan-out is only worth testing
  against a worker that has a type, a thoroughness and a configured prompt.
- **Why were FRE-1489 and FRE-1488 blocked, and are they still?** They need live local-model
  turns, which would have contaminated the study. That reason is gone. FRE-1489 is now build1's
  head.
- **Two tickets are deployed but parked in `Awaiting Deploy` on purpose.** FRE-1481 needs a turn
  with a genuinely failing recall path; FRE-1485 needs `cache_read_tokens` measured on a turn
  driven past the cap. Both are recorded on the tickets with their runbooks.
- **Is `main` green?** Yes. Gateway rebuilt twice, healthy both times, zero tracebacks.
