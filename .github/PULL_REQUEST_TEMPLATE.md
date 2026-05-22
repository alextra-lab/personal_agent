## Summary

<!-- 1-3 bullets on what changed and why. Link the Linear issue and any ADR. -->

- Closes FRE-XXX
- ADR-00XX

## Pre-merge checklist

- [ ] Tests passing (`make test`)
- [ ] `make mypy` clean
- [ ] `make ruff-check` + `make ruff-format` clean
- [ ] `pre-commit run --all-files` clean
- [ ] ADR / spec linked above (if architectural)
- [ ] Screenshots attached (if UI change)
- [ ] Joinability probe green in CI (if change touches event emit, cost recording, memory writes, or schema — ADR-0074)
- [ ] Identity tuple threaded on new emit sites (`session_id`, `trace_id` — ADR-0074)

## Test plan

<!-- How a reviewer can verify the change locally. -->

---

<!--
DO NOT put post-merge items in this checklist:
  - prod verification / curl evidence
  - telemetry confirmation (ES indices, log fields, dashboards)
  - deploy + restart steps
  - "verify on prod after merge"
Those go in a Linear comment on the issue, or in MASTER_PLAN.md follow-up.
The PR checklist gates the merge; reviewers cannot tick post-merge items.
-->
