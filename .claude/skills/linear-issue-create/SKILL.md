---
name: linear-issue-create
description: Create a Linear issue for the Personal Agent project with the correct team, state, and label policy. Use when the user asks to create, file, open, or draft a Linear issue/ticket. Do not invoke for reading or updating existing issues.
disable-model-invocation: true
---

# linear-issue-create

Create a Linear issue that conforms to the **Linear Implement Gate** policy in `.claude/CLAUDE.md`:

> New == Needs Approval. Implement == Approved.

## Required Fields

| Field | Value |
|-------|-------|
| Team | `FrenchForest` |
| State | `Needs Approval` (state — **not** a label) |
| Labels | `PersonalAgent` + exactly one tier label |
| Tier label (pick one) | `Tier-1:Opus` · `Tier-2:Sonnet` · `Tier-3:Haiku` |
| Project | The Linear project this work belongs to — **required** |

### Project is required

`save_issue` accepts `project` and silently omits it when you don't pass one, so a ticket filed
without it lands in no project at all and disappears from every project-level view. On
2026-09-01 that had produced **29 unfiled Approved tickets carrying three of the portfolio's four
Urgents**, across three months — the default outcome leaking through, not a one-off.

Run `mcp__claude_ai_Linear__list_projects` with `team: FrenchForest` and pick the one whose scope
covers the work. If genuinely none fits, say so explicitly to the user and get their agreement
before filing without one — never leave it unset silently.

**A wrong project name is as silent as a missing one.** `save_issue` returns success with no
`project` on the issue rather than erroring, so a typo, a stale name, or an HTML-escaped
character produces exactly the unfiled ticket this rule exists to prevent. During the 2026-09-01
backfill, passing `ADR-0081 Extended — Context &amp; Memory Injection Quality` (ampersand escaped)
did this; the project id resolved it. Copy the name verbatim from `list_projects`, or pass the
project **id**, and confirm `project` is populated on the returned issue before reporting success.

## Tier Selection

Use the routing policy from `.claude/MODEL_ROUTING_POLICY.md`:

- **Tier-1:Opus** — specs, plans, ADRs, complex debugging, architecture work
- **Tier-2:Sonnet** — feature implementation from an approved plan, first-pass debugging
- **Tier-3:Haiku** — Linear hygiene, git ops, linting, boilerplate, mechanical edits

Ask the user which tier if it isn't obvious from the description.

## Workflow

1. **Gather** title and body from the user. Body should reference any specs/ADRs.
2. **Determine** tier (ask if ambiguous).
3. **Find** team/label/project IDs:
   - `mcp__claude_ai_Linear__list_teams` → find `FrenchForest`
   - `mcp__claude_ai_Linear__list_issue_labels` → find IDs for `PersonalAgent` + chosen tier label
   - `mcp__claude_ai_Linear__list_issue_statuses` → find state ID for `Needs Approval`
   - `mcp__claude_ai_Linear__list_projects` → find the project whose scope covers this work
4. **Create** via `mcp__claude_ai_Linear__save_issue` with team, state, label IDs, **project**,
   title, description.
5. **Report** the created issue URL and remind the user it's `Needs Approval` — implementation is blocked until they move it to `Approved`.

## Never Do

- Add a `Needs Approval` **label** (the policy uses *state*, not label).
- Skip the tier label — every issue must have exactly one.
- File without a project because none obviously fits — ask the user instead. An unfiled ticket is
  invisible to project-level planning, which is how the most urgent work goes unseen.
- Move the issue to `Approved` yourself. Only the user does that.
- Create the issue under any team other than `FrenchForest`.

## Output Format

```
✅ Created FRE-XXX — <title>
   State: Needs Approval
   Labels: PersonalAgent, Tier-N:Model
   URL: https://linear.app/...

   Implementation is gated until you move it to Approved.
```
