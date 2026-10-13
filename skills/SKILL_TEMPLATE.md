# Skill template (Agent Skills format, ADR-0156 D1, D5)

A skill is a folder: `skills/<name>/`. Copy the two files below into it. The CI validator
(`tests/docs/test_skill_validator.py`) checks every rule on this page, with two rules of
ADR-0156 A3:

- **A name states an objective** (`name_words`). No part of the name, split on `-` and `_`, is a
  backend (`elasticsearch`, `es`, `tempo`, `neo4j`, `postgres`, `redis`) or the word `telemetry`.
- **A "Not for…" line names the neighbour** (`not_for`). The description ends with a sentence
  that starts with "Not for" and names the neighbouring skill or tool by its exact name, in
  backticks. A name that does not exist fails the check.

```
skills/<name>/
├── SKILL.md          # required: frontmatter + instructions
├── evals.yaml        # required for an agent skill: at least 3 scenarios
├── scripts/          # optional: tested scripts, each with --help and plain error text
├── references/       # optional: detail that the body links to, one level deep
└── assets/           # optional: templates and data files
```

The model sees only `name` and `description` in the catalog. It loads the body with
`read_skill(name="<name>")`, and one listed file with `read_skill(name="<name>", path="scripts/x.py")`.

## `SKILL.md`

The frontmatter holds `name`, `description` and `metadata` only. No other field is allowed.

- `name`: 1–64 characters, lowercase letters, digits and hyphens. It is the folder name.
- `description`: 1–1,024 characters, in the third person (no "I", "my", "you", "your"). It says
  what the skill does and when to use it. This text is all the model has to choose the skill.
- `metadata.requires-tools`: optional, a comma-separated list of the tools the skill uses. It
  filters the catalog per worker type (D9). A worker sees the skill only if it holds every
  listed tool, and every worker sees a skill that lists none. It never routes and never blocks.
- `metadata.provides-tools`: optional, a comma-separated list of skill-provided tools (ADR-0156
  A5). The primary sees such a tool only after `read_skill` loads a skill that provides it, and
  then for the rest of the session. Each name must be a registered tool marked
  `skill_provided`, and each skill-provided tool needs at least one providing skill
  (`provider_violations` in `orchestrator/skills.py`).
- The body: under 500 lines and under 5,000 tokens. Put more detail in `references/`.
- Every file that the body names (`scripts/…`, `references/…`, `assets/…`, or a relative link)
  exists, one level below the skill folder.
- Every command in the body or in `scripts/` comes out allow under
  `config/governance/permissions.yaml`, or carries a `# ask: <reason>` line (D8.7).

````markdown
---
name: <name>
description: <What the skill does, in the third person>. Use when <the tasks that need it>. Not for <the neighbouring task>. The `<neighbour>` skill gives that.
metadata:
  requires-tools: bash
  provides-tools: <a skill-provided tool that loading this skill reveals; leave the line out if none>
---

# <name> — <one line>

## Rules

- <The few rules that the model must never break, for example "answer only from a live query">.

## Steps

1. <The first call, with the exact command or script>.

```bash
<command>
```

2. <The next call>.

## Success

How to check the result before answering:

- <What a correct result looks like, for example "the count is a number, and the time range is
  the one asked for">.

## Recovery

What to do on each failure, with a stop rule:

| Failure | Cause | Next step |
|---|---|---|
| `<error text>` | <cause> | <the next call that can work> |
| The same error twice | — | Stop. Tell the user what failed and what was tried. |
````

## `evals.yaml`

Write the scenarios before the body (S3: "Create evaluations BEFORE writing extensive
documentation"). The D10 G2 gate run uses them: a scenario passes only if this skill loads and no
other skill loads.

```yaml
skill: <name>
scenarios:
  - prompt: "<a request that needs this skill>"
    expected_skill: <name>
    expect: "<what a correct answer holds>"
  - prompt: "<a second request, in different words>"
    expected_skill: <name>
    expect: "<…>"
  - prompt: "<a third request>"
    expected_skill: <name>
    expect: "<…>"
```

`expected_skill` is on each scenario, so a gate run can read a scenario without its file. A
boundary scenario, where a neighbouring skill is right, sits in the neighbour's file.
