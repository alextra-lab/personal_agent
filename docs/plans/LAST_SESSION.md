# Last session — master, to 2026-11-08

## Doing / discussing  (≤5 sentences)
Master delivered the ADR-0159 development agent, one ticket at a time. The last two build tickets
are with the build seats. The gate on each one is the normal gate, with `/code-review high` on the
ticket. The owner also asked about plugin and skill versions. Master answered, and the owner did
not yet ask for an update.

## What was decided and why
- **The dev-agent supervisor stays off on purpose.** `AGENT_DEV_AGENT_SUPERVISOR_ENABLED` is unset.
  Each deploy check expects 0 `dev_job_supervisor` log lines. Do not enable it as part of a ticket
  deploy. The first real jobs come with the owner's approval card and the paid evaluation.
- **The GitHub organization uses the Free plan.** ADR-0159 D8.2 uses no branch rules and no GitHub
  Actions, so a paid plan adds nothing. The VPS token cannot read the plan, so master did not
  confirm the plan from the API.
- **The App has three permissions only:** Administration write, Contents write, Metadata read.
  The FRE-1685 plan step and a live probe confirmed that these three cover every call.
- **The embedder backup uses the same model on Scaleway.** Scaleway rejects the OVH casing of the
  model id with a 422, so the config guard compares ids without case. Do not "fix" the lower-case
  id in `.env`. The measured cross-provider agreement is on FRE-1750.
- **Three small FRE-1750 items are known and not ticketed**, because the owner does not want
  over-ticketing: the rate equals the OVH rate, the Scaleway URL is duplicated, and an http endpoint
  is not refused.
- **Calendar credential.** The private iCal address is read-only, so it serves only the read side
  of FRE-1747. For writes, master recommended a Google service account that the owner shares only
  the Seshat calendar with. The adrs seat and the owner decide at the design step.
- **A diff that is escalated always needs `/code-review high` before the merge.** Two PRs arrived
  at the gate before the result existed. Master held both and sent no bounce, because the seat had
  already started the review. The next trigger, at the fix commit, carried the result.

## Corrections from this session
- Master stated the plan of the organization before it checked the plan. Master then corrected the
  ticket comment.
- Master loaded `.env` into a shell to run a script. The shell broke some values. The settings
  loader reads `.env` itself, so no sourcing is needed.
- Master ran `scripts/verify_dev_agent_github.py` a second time only to read the top of its output.
  Each run creates one private probe repo, and v0 deletes none. Save the output of the first run.

## Worktrees — anything special
- `adrs`, `explore` and `agent-ad5db2001c2a5c5f9` are on old branches. Master did not touch them.

## Answers for the fresh start
- **`.env` backups.** `.env.bak-fre1685` and `.env.bak-fre1750` hold secrets and are ignored by git.
  They are safe to delete once the owner agrees.
- **Before the paid evaluation (FRE-1689):** add the Scaleway catalog entry for the chat model,
  check the DeepSeek catalog id, and get the owner's decision on the weekly `_total` cap. A deploy
  grant is never a budget grant. The run also needs a preflight, the owner's room confirmation and
  a recorded go.
- **Awaiting Deploy holds more than 10 old tickets.** Master offered to go through them, and the
  owner did not answer. They block no stream. FRE-1724 waits for the owner's AC-5 check in the PWA.
- **A startup warning for a settings override with no audit row** is an open owner decision. See
  the master comment of 2026-11-02 on FRE-1724.
- **Dependabot:** one medium alert is open, `minimatch` in `seshat-pwa/package-lock.json`. It
  appeared on 2026-11-07. It is not triaged.
- **Pending updates.** `security-guidance` has an update, 2.0.17 to 2.0.18. Master recommended that
  update and a pick-up at each session's next normal restart. Claude Code also shows an installed
  update that waits for a restart. Restart no seat in the middle of a ticket.
- **Owner-pending:** FRE-1742 acceptance, FRE-1693, FRE-1725, FRE-1726, and the design steps of
  FRE-1747 and FRE-1748. The FRE-1745 AC-5 reads go as comments on that Done ticket. The first
  weekly check of FRE-1741 is on 2026-11-12.
