# E-003 — Safety Gateway Effectiveness

Purpose:
Validate whether deterministic primitives meaningfully reduce risk without killing usability.

Hypothesis:
Linked to H-003

Method:
Simulate risky + normal operations:

- destructive command attempts
- outbound leaks
- runaway execution loops
- normal work scenarios

Metrics:

- number of blocked unsafe attempts

- number of false positives

- user frustration score
- stability impact

Outcome Criteria:
See H-003

Status: Planned
