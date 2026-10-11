# Skill template (Agent Skills format, ADR-0156 D1, D5)

A skill is a folder: `docs/skills/<name>/`. Copy the two files below into it. The CI validator
(`tests/docs/test_skill_validator.py`) checks every rule on this page.

```
docs/skills/<name>/
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
  filters the catalog per worker type (D9). It never routes and never blocks.
- The body: under 500 lines and under 5,000 tokens. Put more detail in `references/`.
- Every file that the body names (`scripts/…`, `references/…`, `assets/…`, or a relative link)
  exists, one level below the skill folder.
- Every command in the body or in `scripts/` comes out allow under
  `config/governance/permissions.yaml`, or carries a `# ask: <reason>` line (D8.7).

````markdown
---
name: <name>
description: <What the skill does, in the third person>. Use when <the tasks that need it>.
metadata:
  requires-tools: bash
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
    expect: "<what a correct answer holds>"
  - prompt: "<a second request, in different words>"
    expect: "<…>"
  - prompt: "<a third request>"
    expect: "<…>"
```
