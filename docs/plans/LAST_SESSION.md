# Last session — master, 2026-10-01 09:39 UTC to 2026-10-03 07:30 UTC

## Doing / discussing  (≤5 sentences)
The session began with VPS maintenance (kernel, OpenSSL, Docker 29.8.2, reboot) and a re-prime.
It then cleared two dispatch stalls, shipped FRE-1529 and FRE-1530, merged ADR-0153, and replaced
master's `tmux send-keys` with `SendMessage`. The owner then disabled the `remember` plugin and
trimmed this file's template. Two threads are open: FRE-1535 (tool approval, the owner's four
policy answers relayed on 2026-10-03 07:18 UTC) and FRE-1537 (the planner ADR).

## What was decided and why

**Severity follows reachability, not the CVE list.** 13 PyJWT advisories looked High. The only
JWT call site, `artifacts_router.py:191`, runs behind `_verify_internal_token`, which fails closed.
No anonymous caller reaches it, so master folded the two drafts into one Medium ticket (FRE-1530).

**Dispatch stalled for two weeks and nobody saw it.** build2 logged `dispatch_seat_wedged` for
4,119 ticks (about 14 days) on an Urgent ticket. The reboot cleared it. adr stalled 17 days on
FRE-1502, parked by removing its stream label while it stayed In Progress. Master then repeated the
mistake: it moved FRE-1525 to Backlog without clearing its record, and caught it the next morning.
Each time, the orchestrator wrote an alert to the notify ledger, and the ledger reached nobody.

**Escalated diff, owner review waived.** PR #1185 (FRE-1529) merged without `/code-review ultra`
under the 2026-08-31 directive. The tests reproduced the defect on the old source.

**An ADR PR waits for the seat's handoff.** Master held PR #1187 (ADR-0153) until the adr seat
posted its handoff, because ADR-0152 merged early on 2026-09-14 and the owner reopened it.

**ADR-0152 is superseded, not amended.** The owner said "go" to one ADR that merges ADR-0147 and
ADR-0152, filed as FRE-1537. Worker round budgets stay out of it (FRE-1487 owns them).

**FRE-1535 had a wrong tool in its list.** It named `mcp_list_indices`, which needs no approval.
The eighth tool is `mcp_mcp-remove` (`tools.yaml:823`). Master corrected the ticket.

**FRE-1530's live check used an internal request, not a gateway turn.** Master sent two bad JWTs
to `/internal/artifacts/{id}` from inside the container: no model call, no write. That needed no
owner OK, unlike a `/chat` turn.

**FRE-1529 AC-4 waits for a natural owner turn** (the owner's choice, as for FRE-1527 AC-5). The
qualifying trace is listed on the ticket.

**`SendMessage` replaces `send-keys` for master** (PR #1189). Tested on cc-2build: reply in about
15 s, no approval hold across permission modes. The Python daemons keep `send-keys`.

**The `remember` plugin is disabled; the `.remember/` folder is kept.** It is third-party, not
ours. Its one shared handoff (a FRE-1122 note from 2026-09-08) reached every seat 96 times.

## Worktrees — anything special
- `adrs`: the git-ignored `telemetry/archive/fre1502-planner-probe/` holds the raw ADR-0152 probe
  rows. FRE-1537 can reuse them.
- Untracked `*.bak*` files at the repo root and under `telemetry/` are the owner's.

## Answers for the fresh start
- **Ticket, stream and seat state:** `uv run python -m scripts.dispatch.next_resolver`, Linear and
  `telemetry/dispatch_state.json`. Not copied here.
- **`python` is not on PATH on this host.** Every skill now says `uv run python -m ...`.
- **Session start shows no `LAST HANDOFF` or `MEMORY` block, and prompts carry no time stamp.**
  That is the plugin change, not a fault. Use `date -u`.
- **Restarts still due** (plugin off, Claude Code 2.1.288 installed): cc-master at this reset,
  cc-1build and cc-adrs after their current PRs, cc-explore when the owner finishes there. Start
  each with `cc-sessions restart <seat>`, then check the pane for a "Resume from summary" prompt.
- **cc-explore is the owner's own seat for now** (Laya and Jev research). Do not message it.
- **The explore skill's no-argument resolver call always fails:** the resolver accepts `adr`,
  `build1` and `build2` only. Explore runs on an explicit ticket id. Not fixed: registering an
  explore stream is a design choice.
- **Pending owner decision:** whether dispatch alerts must reach the owner. The notify ledger
  records stalls and wedges, and nothing reads it (see "two weeks" above).
- **Deliberate `.env` settings** carry dated owner comments in `.env` itself.
- **Local model:** a real completion through `:8600` answered on 2026-10-01. Recheck with a
  completion, never `/health`.
