# Last session — 2026-07-16 → 17 (very long; config chain shipped + deployed, build2 crash+recovery, tmux 3.7a, cost-gov ADR, backlog reel-in)

## ▶ THIS RESET IS THE TMUX CUTOVER — read first
The reset that follows this file is **not a plain /clear** — the owner is running **`tmux kill-server`** to
cut the shared tmux server over to **3.7a** (built from source this session; see below). It kills **every**
seat (master, build, build2, adrs, explore). After the kill: the dispatcher (systemd, survives) spawns
build seats on 3.7a when work is dispatched; the owner re-creates cc-adrs/cc-explore + re-primes master via
`/prime-master`. **First thing on re-prime: confirm the seats came back and `tmux -V` in a fresh shell shows
3.7a** (server is 3.7a only after this kill; before it, it was 3.5a).

## Doing / discussing (≤5 sentences)
Shipped the **FRE-896 config-cleanup chain end to end**: 876 (D4 field-doc guard) · 896 (curated removal — only 3 confirmed-outgrown deleted, owner's origin-ADR caveat held) · 897 (managed_* secret redaction) · **906 (wired event_bus_ack_timeout_seconds → XAUTOCLAIM self-reclaim sweep — DEPLOYED + live-verified: reclaimed 2 real orphaned messages on gateway startup)** · 907 (audit-scanner hardening). **cc-build2 tmux CRASHED mid-906**; recovered it (root cause: launcher relaunches into the crashed deterministic session-id → dies on the stale lock; fix = clear the session state — see memory `reference_worker_seat_crash_recovery`). Routed + merged **cost-gov ADR-0120** (Proposed, supersedes ADR-0065). Did a **backlog reel-in** (27 orphans homed into projects; ~124 stale local branches pruned; cc-explore2 torn down). Built **tmux 3.7a** from source, cutover pending = this reset.

## Commits — the story behind the last 10
`cfffe3dc`/#562 FRE-907 scanner hardening (codex ran; 6 code-review fixes incl. quote-parity guard). `d9783ca1`/#561 **FRE-906** event-bus wiring — I reviewed the consumer logic myself (codex didn't run; workflow reviewer errored on infra) and it held; **`20ea7b25` is the WIP-checkpoint from the build2 CRASH** (committed to preserve the crashed first attempt before relaunching fresh). #560 FRE-897 secret redaction · #559 FRE-896 curated removal (I independently grep-verified the 3 deletions zero-consumer, then FRE-907 revealed my grep shared the tool's `.py`-only blind spot — re-verified clean across all file types). Not in `git log -10` but this session: PRs #554 (reel-in) #556 (ADR-0120) #557 (0065→Superseded) #558 (0119 cost-surface note).

## Worktrees — anything special
- **cc-build (build1)** — last built FRE-907 (Done). Its worktree also holds the **committed fre-879 WIP** (artifact_builder extraction, +466, pushed to `origin/fre-879-...`) — do-not-discard, resumes when the config-UI chain does.
- **cc-build2 (build2)** — last built FRE-906 (Done). CRASHED mid-build this session; recovered. Crashed session `4d5840d9` archived to scratchpad.
- **cc-adrs** — delivered ADR-0120, idle.
- **cc-explore** — owner deliberation seat.
- **cc-explore2** — **TORN DOWN this session** (cost-gov work merged; don't look for it).

## Plan position + drift
- **Configuration Management project advanced hard**: the whole FRE-893→896/897/906/907 audit+cleanup chain shipped; ADR-0099 D4 guard live. On the go-forward: **#1 (route cost-gov to adr) DONE — ADR-0120 authored+merged Proposed.**
- **Config-UI (ADR-0119) is still next**, waits on the cost-gov ADR *result* per owner's item-2; I amended ADR-0119 (PR #558) so its observe view renders ADR-0120's cost surface (T6), not the abolished hard caps — the two ADRs are now coherent. No structural config-UI change needed.
- **Deploy:** running gateway is now **cfffe3dc** (rebuilt 06:51 UTC to ship 906; embedder re-stopped). Awaiting-Deploy holds unchanged: FRE-884/739/866/717.
- No real drift — followed the go-forward, reeled in the backlog on the owner's direction, and absorbed a mid-session infra crash.

## Answers for the fresh start
- **Did the tmux cutover happen?** The reset was it. Verify seats are back on 3.7a (`tmux -V`); 3.5a stays in `/usr/bin` as fallback (revert = `rm /usr/local/bin/tmux`). Dispatcher PATH already resolves 3.7a.
- **Is 906 deployed?** Yes — live-verified (2 orphans reclaimed, zero errors). Done.
- **Was the build2 crash resolved?** Yes — both builds recovered + shipped. The recovery runbook is memory `reference_worker_seat_crash_recovery`.
- **Cost-gov next step?** ADR-0120 is Proposed; impl tickets (T0 = instrument OVH/Voyage/Perplexity) are NOT filed — they await the owner moving it Proposed→Accepted. Still ask-first on all cost.
- **fre-879 branch on origin?** Intentional — parked WIP backup, not stale. Keep.
- **Branch cleanup?** Done (155→9 local; remote clean). Fold `fetch --prune` + gone-branch delete into routine.
