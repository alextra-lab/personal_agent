# Governance (Runtime Policy & Authorization)

This directory will contain enforceable policies and mode definitions that
constrain the agent's behavior, such as:

- Tool and file access rules
- Outbound web and data-sharing rules
- Autonomy levels per mode (conservative / moderate / experimental)
- Human approval requirements

ADRs in `docs/architecture_decisions/` provide the design rationale that governance
processes refer to, but governance configs here are what actually constrain
the running system.
