# Last session — 2026-07-31 (the night the process was diagnosed, and it was not master who did it)

## Doing / discussing

Two process ADRs were authored, reviewed and shipped end to end — one severing criterion inheritance,
one retiring MASTER_PLAN for an owner console — and both chains ran to completion in the same session.
The session ends mid-delivery with both build seats working: build1 on the shard-ceiling ticket, build2
on ADR-0126's behavioural profile. The through-line worth inheriting is not what shipped but *who was
right*: the owner diagnosed the root cause after master had spent a night filing tickets against
symptoms.

## What was decided and why

**The acceptance-criteria diagnosis was the owner's, and master's four patches were the wrong shape.**
Master filed four tickets against symptoms — the queue's ambiguity, the watcher's re-trigger, the wedge
alarm, the integration's corruption — before the owner named the cause: criteria are written in a form
that cannot be evaluated at the gate. Per the convergence law, ADR-0130 removes the operation that
could fail; the four patches improve observers. Expect ADR-0130 to hold and at least two of those four
to recur.

**"Deployed IS deployed."** Master had widened `Awaiting Deploy` from a deploy queue into a verification
queue, let it reach twenty-one, then argued for *adding a state* to resolve ambiguity it had created.
The owner rejected that. Nineteen tickets closed in one sweep; not one had anything wrong with it.

**Two tiers, and only one belongs to a ticket that builds.** The owner's distinction: an ADR ticket is
done when the ADR's criteria are met; a build ticket when its own micro criteria are. One long-lived
seam ticket per ADR is correct by design. Nineteen long-lived build tickets is the pathology.

**Branch-specific rules cannot fix the docs-PR corruption — do not re-propose them.** Master
recommended them off the settings section heading, before seeing the field. They scope by the PR's
**target** branch; every PR here targets `main`, so they cannot distinguish a docs PR from an
implementation one. The remaining lever is detection: make `reconcile_board.py` treat a started-state
ticket with **no merged PR** as **FAIL** rather than UNVERIFIABLE. Never filed — the owner asked for
analysis only.

**The owner re-enabled the Linear PR transitions deliberately, to see whether the corruption returns.**
That is a live experiment, not a settled decision, and it is in direct tension with ADR-0131 D4 and
FRE-1086. Do not read D4 as agreed. Until FRE-1086 lands, the console directive against bare `FRE-XXXX`
in docs PR titles, bodies and branch names is load-bearing rather than precautionary.

**A build session's claim about production, derived from its test environment, is suspect.** FRE-1015's
handoff concluded its mechanism was inert in production. It reasoned from the one recall branch its
test environment could reach — the proactive path dies there for want of an embedder credential.
Production entities come from that very path. Master accepted the claim and told the owner the deploy
would be a no-op; it was not. Corrected before the deploy was authorised.

**The owner monitors the build seats; master must not.** FRE-867's premise — that master is the owner's
proxy for seat visibility — was false, and the whole notification path was removed rather than tuned.
Master keeps `send-keys` for *content*: a bounce, a finding, a decision. Not for watching.

**Master approved and dispatched two tickets the owner had not asked for**, on two separate misreadings
of "all 3" (FRE-1064, FRE-1086). Both reverted. When a terse instruction could mean two sets of
tickets, ask before mutating nine of them.

## Worktrees — anything special

- **build2** holds several merged-but-undeleted branches, which is why `--delete-branch` fails on every
  merge. Harmless and expected; do not chase it.
- Two pre-existing stashes remain (`fre-916` WIP, a sandbox-debug one). Neither is master's.

## Sequence position + drift

The console's eight standing directives are now the overlay prime-master reads at #8; they replace
what the plan file used to carry. ADR-0129's eight children sit at Needs Approval — master's hold is
*released*, but the owner has not approved them.

We deviated hard into process work for a full night. FRE-1036 — the only ticket with a hard external
deadline — went unscheduled until 11:03, having spent two days falsely showing as delivered. That was
the correct diagnosis but it crowded out the one item with a clock.

## Answers for the fresh start

- **Why is `Awaiting Deploy` nearly empty?** Nineteen closed in one sweep under the corrected standard.
  If it grows past a handful again, that is the pathology returning, not throughput.
- **Why does FRE-1036 outrank everything?** Only hard external deadline; measured ~50 days of shard
  headroom, not the 34 its text states. It is an **ES reindex — always-ask deploy, FRE-599 risk shape**,
  and its own text warns mappings were wrong on the first pass last time. Ask before it touches prod.
- **Three ADR-0129 obligations are master's**, not any ticket's — recorded on FRE-1043's mapping. The
  live one: run the identity-share query once after FRE-1064 deploys and decide whether the chain
  proceeds. It is the chain's only cheap kill-switch.
- **FRE-1073's due date (2026-10-01) is provisional** and must be re-dated to fourteen days after its
  last child deploys.
- **Untraced, flagged rather than implied:** whether stance items compete in the recall budget trim.
  If recall quality wobbles on stanced topics, look there first.
