# Requirements Traceability Matrix (RTM)

This connects functional goals → architectural decisions → hypotheses & validation.

| Requirement | Architecture Mapping | Hypothesis Link | Validation Source | Status |
| ------------ | ---------------------- | ----------------- | ------------------ | -------- |
| Local-first sovereign AI | Local Model Pool, Offline-first design | H-001 | Architecture Spec | Accepted |
| Safe & Controlled behavior | Safety Gateway, Supervisor, Graph Backbone | H-001 / H-003 | Tests + Logs | Proposed |
| Explainability & Trust | Explainable Plans, Captains Log, Telemetry | H-001 / H-004 | Evaluation & Logs | Proposed |
| Support experimentation | Experiment Runner, Metrics System | H-002 / H-004 | Evaluation Pipelines | Proposed |
| Ability to reason deeply | Reasoning + Planner/Critic cognition nodes | H-002 | Experiment E-002 | Proposed |
| System observability | Metrics store, audit logs, supervisor | H-001 / H-003 | Evaluation Framework | Proposed |
| Controlled autonomy | Human checkpoints, outbound gating, scope limits | H-003 | Safety Tests | Proposed |
