# Captain's Log Human-Approval Invariant (ADR-0030 Addendum)

## Invariant Statement

**The Captain's Log enforces a mandatory human-approval gate on all self-modification proposals: no proposal can advance from `AWAITING_APPROVAL` to `APPROVED` status without explicit human intervention.**

## Mechanism

The promotion pipeline (`src/personal_agent/captains_log/promotion.py`) scans entries in `AWAITING_APPROVAL` status and creates Linear backlog issues when they meet promotion criteria (min frequency, min age, category filters). These Linear issues are created in "Needs Approval" state—not auto-approved—requiring human review and labeling (via Linear's feedback labels: Approved, Rejected, Deepen, etc.) before the proposal can be acted upon.

Only after successful Linear issue creation does the pipeline mark the corresponding `CaptainLogEntry` as `APPROVED` in the local store (`promotion.py:603-629`). This ensures:

1. **No self-action path**: an entry never transitions from AWAITING_APPROVAL to APPROVED without an external human-facing ticket in Linear
2. **Human review first**: every promoted proposal lands in Linear with full context (`promotion.py:106-181`), requiring human decision before any downstream automation
3. **Audit trail**: the mapping between Captain's Log entry and Linear issue ID is immutable once set (`promotion.py:614`)

## Future Automation Policy

Any future automation that *acts on* Captain's Log proposals—creating branches, implementing code changes, filing PRs, or triggering cost-gated operations—must itself be a separately-reviewed, separately-gated decision subject to the same human-approval gate. Do not extend the promotion pipeline to auto-implement; instead, create new workflows that treat the Linear issue as the approval point, and gate those workflows with their own sign-offs.

## References

- **ADR-0030**: Captain's Log Deduplication & Self-Improvement Pipeline (promotion pipeline design)
- **ADR-0105**: Self-Improvement: Proposal-to-Insight Lifecycle (proposal lifecycle, promotion funnel, outcome capture)
- **Code**: `src/personal_agent/captains_log/promotion.py` (l. 249, l. 578, l. 603-629)
