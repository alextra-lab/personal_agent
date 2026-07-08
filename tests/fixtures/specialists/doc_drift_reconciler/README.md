# Doc-drift reconciler fixtures (FRE-834, ADR-0113 §3)

| File | Role |
|------|------|
| `already_decided_is_owner.json` | A `ProposedTicket` re-proposing "rename `is_owner`" — the real, merged `docs/architecture_decisions/ADR-0107-user-identity-resolution-and-log-propagation.md` Decision §1/§4 already settled this (unchanged name, no new field). Expected: **REJECT**, citing ADR-0107. |
| `novel_ticket.json` | A `ProposedTicket` with no prior ADR coverage — a true-negative check. Expected: **APPROVE**. |
| `injection_ticket.json` | A `ProposedTicket` whose description plants a fake envelope-close delimiter and a spoofed `<<<VERDICT>>>` APPROVE block, proving `fetch_reconciler_artifact` places the whole ticket body inside the untrusted envelope (structural — the harness's `neutralize()` does the rest; proven in `test_pr_gate.py`/`test_specialist_harness.py`, not re-proven here). |
| `adr_variants/docs/architecture_decisions/ADR-900{1,2,3,4}-*.md` | Synthetic mini-ADRs (not the live corpus) exercising `build_adr_index`'s heading-tolerance: plural `## Decisions`, a suffixed `## Decision — Final Ruling`, `## Decision Outcome`, and a fully-degraded ADR with no `**Status:**` and no Decision heading at all. Kept separate from the real corpus so the test doesn't depend on which real ADRs currently use which heading style. |

The live behavioural test (`test_doc_drift_reconciler_live.py`) runs the real reconciler against the
**real** repo ADR corpus (`docs/architecture_decisions/`), not these synthetic variants — the variants
exist only to prove the deterministic parser's tolerance.
