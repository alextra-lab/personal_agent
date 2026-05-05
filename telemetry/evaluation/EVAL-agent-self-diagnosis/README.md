# EVAL-agent-self-diagnosis

Recovery harness output for the agent self-diagnosis recovery plan
(`docs/plans/2026-05-05-agent-self-diagnosis-recovery-execution-waves-0-2.md`).

## Layout

```
EVAL-agent-self-diagnosis/
  README.md                  ← this file
  prompts.yaml               ← canary prompt definitions
  survey-<YYYY-MM-DD>/       ← Wave 1.1 ES + Neo4j survey snapshots
    report.md
  <run-id>/                  ← Wave 1.2 / Wave 2 harness runs
    summary.md               ← roll-up across prompts
    <prompt-id>/
      report.md              ← per-prompt human-readable summary
      raw.json               ← per-prompt raw turn data + ES hits + Neo4j counts
```

## Commands

Survey existing telemetry (no /chat traffic):

```
make eval-recovery-survey
```

Run the full canary set (single PROFILE tag is metadata only in Wave 1):

```
make eval-recovery RUN=<id>
make eval-recovery RUN=<id> PROFILE=baseline
```

Run a single prompt:

```
make eval-recovery RUN=<id> PROMPT=primitive_tool_with_implied_skill
```

Wave 2 canaries (added when 2.x scripts land):

```
make canary-infra
make canary-memory UUID=$(uuidgen)
make canary-cleanup
```

## Hard rules

- Survey script must fail loudly on missing ES indices or unreachable Neo4j.
- Per-prompt reports always include `raw.json` so any rendered roll-up is
  reconstructible from primary sources.
- `:Canary` label is reserved for Wave 2 memory-canary entities so
  `canary-cleanup` can remove them without affecting real memories.

## Source

- Recovery plan (this scope): `docs/plans/2026-05-05-agent-self-diagnosis-recovery-execution-waves-0-2.md`
- Source doc (full Waves 0-5): `docs/plans/2026-05-05-agent-self-diagnosis-recovery-plan.md`
