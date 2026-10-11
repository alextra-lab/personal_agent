---
name: mermaid-diagrams
description: 'Creates software diagrams in Mermaid syntax and validates them with mmdc before they are
  shown: flowcharts, sequence diagrams, state machines, ER diagrams, C4 architecture diagrams, class diagrams,
  Git graphs, Gantt charts, pie charts, mindmaps and timelines. Use when the user asks for a diagram,
  a flowchart, a schema, an architecture sketch or a visualization, or when a picture explains a system,
  a data flow or a workflow better than prose.'
metadata:
  requires-tools: bash, write
---

# SKILL: Mermaid Diagrams

## Rules

Before emitting any non-trivial mermaid block to the user, validate it with the check script `scripts/validate_mermaid.py`, which runs `mmdc` (see Validation section).
The first content line MUST be the diagram-type keyword. Do not invent syntax — every arrow, keyword, and bracket must be from the v11 spec.

> **Use this skill to** (a) decide when a diagram beats prose, (b) pick the right type, (c) get v11 syntax exactly right, (d) **validate the fence with the check script before sending it to the user**.

---

## When to Use a Diagram vs Prose

**Diagram if:**
- Three or more entities with non-linear relationships
- Temporal ordering or message sequence is the point
- A state machine has three or more states
- A decision tree branches into multiple paths
- Architecture explanation benefits from spatial layout

**Prose if:**
- Linear narrative or a single entity
- The diagram adds chrome without adding information
- A bulleted list is shorter and equally clear

---

## Diagram Type Selection

| Intent | Type | First-line header |
|---|---|---|
| Flow / decision / process / pipeline | flowchart | `flowchart TD` or `flowchart LR` |
| Actors exchanging messages over time | sequence | `sequenceDiagram` |
| States and transitions | state | `stateDiagram-v2` |
| Entities + relationships (DB/domain schema) | ER | `erDiagram` |
| OO model / class hierarchy / domain model | class | `classDiagram` |
| Software architecture (system/container/component) | C4 | `C4Context` / `C4Container` / `C4Component` |
| Git branching strategy | git | `gitGraph` |
| Timeline / project plan | gantt | `gantt` |
| Proportions of a whole | pie | `pie` |
| Hierarchical brainstorm | mindmap | `mindmap` |

**Layout hints for `flowchart`:**
- `TD` (top-down): tree structures, ≤6 nodes
- `LR` (left-right): chains, pipelines, request flows
- `BT` / `RL` are valid but uncommon — prefer `TD`/`LR` unless you have a specific reason (e.g. bottom-up dependency tree)

**When to use C4 vs flowchart for architecture:**
- C4 for multi-level architectural views (system → containers → components)
- Flowchart for data/request flow within one level

---

## Core Syntax

All mermaid diagrams follow this pattern: a fenced code block with the language tag `mermaid`, whose first line is the diagram-type keyword, followed by the diagram body. Example structure (literal backticks not shown):

    fenced block with language `mermaid`
        first line: diagram type keyword
        following lines: diagram content

Key principles:
- The **first non-empty line** is the diagram-type keyword — mandatory, no exceptions
- Use `%%` for comments: `%% this is a comment`
- Indentation and line breaks improve readability but aren't required
- Unknown keywords and typos break diagrams silently or with a parse error

---

## Syntax Invariants

These are the rules most commonly broken. Violating any produces a parse error in mermaid v11.

### 1. Diagram-type header is mandatory on line 1

Without it: `No diagram type detected`.

```
flowchart TD
    A --> B    ✅

    A --> B    ❌  (missing header)
```

### 2. Node IDs vs labels in `flowchart`

A node ID is a bare token (no spaces, no punctuation). Put the display label in square brackets:

```
flowchart TD
    A[User clicks Buy] --> B{Authenticated?}
    B -->|Yes| C[Dashboard]
    B -->|No| D[Login page]
```

Never write `User clicks Buy --> B` — that tries to use a label as an ID.

### 3. Arrow syntax is type-specific — never mix

| Type | Valid arrows |
|---|---|
| `flowchart` | `-->` `-.->` `==>` `--text-->` `-->|label|` |
| `sequenceDiagram` | `->>` `-->>` `-x` `--x` `-)` `--)` |
| `stateDiagram-v2` | `-->` only |
| `erDiagram` | cardinality glyphs (see §8 below) |

### 4. Reserved words cannot be bare node IDs

In `flowchart`, these are reserved: `end`, `class`, `subgraph`, `direction`, `default`, `style`, `linkStyle`. Bracket them or rename:

```
A --> B[end state]   ✅
A --> end            ❌  (parse error)
```

### 5. `subgraph` must close with `end`

```
flowchart TD
    subgraph Pipeline
        A --> B
    end
    B --> C
```

Missing `end` leaves the parser in an open block.

### 6. Curly braces have meaning — use them deliberately

In `flowchart`, `{...}` is the **decision/rhombus node shape**: `B{Authenticated?}` is a valid diamond node. `{}` is *not* generally broken — it's reserved syntax.

What can break:
- `{}` inside a `[ ]` rectangle label can confuse the parser depending on contents — e.g. `A[Result {data}]` may parse `{data}` as start of a new node. Either rephrase the label or escape: `A["Result {data}"]` (quoted label).
- `{}` in `%%` comments is usually fine but has been reported to break in some v11 builds — when in doubt, paraphrase the comment.

Quoted labels (`A["any string here"]`) are the safest form when the label contains punctuation, brackets, or special characters.

### 7. `sequenceDiagram` participants with spaces

Declare them explicitly when names contain spaces:

```
sequenceDiagram
    participant GitHub Actions
    participant Deploy Server
    GitHub Actions ->> Deploy Server: push artifacts
```

### 8. `erDiagram` cardinality glyphs

Use exactly these — don't invent variants:

| Relationship | Glyph |
|---|---|
| One to many | `||--o{` |
| One to one | `||--||` |
| Many to many | `}o--o{` |
| Zero or one | `|o--o|` |

### 9. `stateDiagram-v2` specifics

- `[*]` is the start/end state
- Transitions use `-->` with optional label after `:`
- Nested states: `state X { ... }`

---

## Pre-emit Checklist (mental)

Before closing the fence, verify:

1. First line has the diagram-type header
2. Every arrow uses the syntax for *that* diagram type
3. Every label with spaces or punctuation is inside `[ ]` (flowchart) or declared via `participant` (sequence)
4. All `subgraph` blocks have matching `end` lines
5. Decision-shape `{...}` is only used as a `flowchart` node shape, not inside `[...]` rectangles

---

## Validation (automated)

The gateway container has `mmdc` (`@mermaid-js/mermaid-cli`) installed, and the check script `scripts/validate_mermaid.py` of this skill runs it and gives one verdict. **Validate any non-trivial diagram before showing it to the user.** A trivial diagram is ≤5 nodes of a type you've successfully produced earlier in this turn.

### Pattern

1. Write the diagram body (no fence) to `/tmp/m.mmd` with the `write` tool.
2. Run the script on that file:

```bash
# ask: python3 can run any program, so this asks for approval (ADR-0156 D8.2).
python3 docs/skills/mermaid-diagrams/scripts/validate_mermaid.py /tmp/m.mmd
```

The first line of the output is the verdict:

| First line | Meaning | Next step |
|---|---|---|
| `VALID` | `mmdc` parsed the diagram and wrote an SVG | Emit the fence to the user. |
| `INVALID` | The diagram has a syntax error. The error text follows. | Read it, fix, write the file again, run the script again (Recovery). |
| `ERROR` | The check could not run: no `mmdc`, no browser, a time-out or a missing file | Do not repair the diagram (Recovery). |

**Why a script.** Do not rebuild the check with `mmdc` and a file test. Observed on `mmdc` 11.17.0:

- most parse errors exit 1 and write no SVG, but a broken `pie` diagram exits 0 and writes mermaid's error diagram as an SVG;
- a diagram of more than 50,000 characters exits 0, and mermaid reads none of it (it draws a placeholder);
- a browser failure also exits 1, and it looks like a diagram error;
- a fixed output path such as `/tmp/m.svg` keeps an old SVG, and a file test then calls a broken diagram valid.

The script writes to a new directory on every run, reads the exit code and the SVG, and tells a browser failure from a diagram error. `--help` lists the options.

### Cost-aware policy

Each `mmdc` call takes ~2 seconds (Chromium spin-up). Use the cost budget:

- ✅ **Validate** diagrams ≥6 nodes, any new diagram type in the turn, anything with unfamiliar syntax (cardinality glyphs, C4 directives, subgraphs)
- ❌ **Skip validation** for trivial fences you've already seen succeed earlier in the same turn — repeated calls just burn 2s for no information gain

### Repair loop

If the script prints `INVALID`:

1. Read the error text. Typical forms: `Parse error on line 3: ... Expecting 'X', got 'Y'`.
2. Match the error against the Syntax Invariants section above. The most common failures: missing diagram-type header (§1), label not bracketed (§2), wrong arrow for the type (§3), reserved word as bare ID (§4).
3. Rewrite the diagram and re-validate. Cap at **3 repair attempts** — if you can't get it to parse after three, fall back to the source-only form: emit the raw `mermaid` fence as a `text` code block (so it's at least readable) and explain in prose what the diagram was supposed to show.

If the script prints `ERROR`, the check did not run, so the diagram is not known to be wrong. Do not repair it.

---

## Seshat PWA Conventions

The PWA renders mermaid fences with a custom dark theme (slate palette, blue-500 accent). To keep diagrams native to the app:

- **Keep diagrams ≤30 nodes** — larger diagrams overflow the chat column.
- **Do not use `%%{init: ...}%%` directives** — the PWA injects its own theme. An `init` directive overrides it.
- **Do not use frontmatter config blocks** (the `---\nconfig:\n---` syntax) — same reason.
- **Do not embed click handlers** (`click NodeId href "..."`) — strict security mode strips them silently.

---

## Worked Examples

### Flowchart — PR merge flow

```mermaid
flowchart LR
    A[Open PR] --> B{CI passes?}
    B -->|Yes| C{Review approved?}
    B -->|No| D[Fix and push]
    D --> A
    C -->|Yes| E[Merge to main]
    C -->|No| F[Address feedback]
    F --> A
```

### Sequence diagram — SSE streaming

```mermaid
sequenceDiagram
    participant Browser
    participant Gateway
    participant LLM
    Browser ->> Gateway: POST /chat
    Gateway ->> LLM: stream request
    LLM -->> Gateway: token chunks
    Gateway -->> Browser: SSE events
    Browser ->> Browser: render tokens
```

### State diagram — consolidation lifecycle

```mermaid
stateDiagram-v2
    [*] --> Idle
    Idle --> Checking : interval elapsed
    Checking --> Skipped : active requests > 0
    Checking --> Running : all clear
    Skipped --> Idle
    Running --> Idle : complete
    Running --> Error : exception
    Error --> Idle : logged
```

### Class diagram — domain model

```mermaid
classDiagram
    User "1" --> "*" Session : owns
    Session "1" --> "*" Message : contains
    class User {
        +UUID id
        +string display_name
    }
    class Session {
        +UUID id
        +datetime created_at
        +list~Message~ messages
    }
    class Message {
        +UUID id
        +string role
        +text content
    }
```

### C4 — system context

```mermaid
C4Context
    title Seshat system context
    Person(user, "User", "Sends chat messages")
    System(seshat, "Seshat Gateway", "Orchestrates LLM calls and memory")
    System_Ext(llm, "LLM Server", "MLX inference on Apple Silicon")
    System_Ext(neo4j, "Neo4j", "Knowledge graph")
    Rel(user, seshat, "HTTPS/SSE")
    Rel(seshat, llm, "OpenAI-compat API")
    Rel(seshat, neo4j, "Bolt")
```

### ER diagram — session schema

```mermaid
erDiagram
    USER ||--o{ SESSION : "has"
    SESSION ||--o{ MESSAGE : "contains"
    MESSAGE {
        string id PK
        string role
        text content
        datetime created_at
    }
```

---

## Success

A diagram is ready to show when all of these hold:

- The script printed `VALID` for this exact text, or the diagram is trivial under the cost-aware policy.
- The first non-empty line is the diagram-type keyword.
- The Pre-emit Checklist holds.
- The diagram has ≤30 nodes, no `%%{init: ...}%%` directive, no frontmatter config block and no `click` handler (Seshat PWA Conventions).

## Recovery

What to do on each failure, with a stop rule:

| Failure | Cause | Next step |
|---|---|---|
| `INVALID`: `Parse error on line N: ... Expecting 'X', got 'Y'` | A syntax rule is broken | Match the error to the Syntax Invariants: missing header (§1), label not bracketed (§2), wrong arrow for the type (§3), reserved word as bare ID (§4). Rewrite, write the file again, run the script again. |
| `INVALID`: `No diagram type detected` | The first line is not a diagram-type keyword | Add the header (§1). Run the script again. |
| `INVALID`: `mmdc drew mermaid's error diagram` | The diagram has a syntax error that `mmdc` does not report as a failure | Compare the diagram with the syntax of its type. Rewrite and run the script again. |
| `ERROR` | `mmdc` is missing, its browser did not start, or it timed out | Do not repair the diagram. Emit the `mermaid` fence and say that it was not validated. |
| `INVALID`: `The diagram has N characters` | The text is over the limit of 50,000 characters of mermaid | Split it into smaller diagrams. Check each one. |
| `INVALID` 3 times in a row | The diagram is not fixed | Stop. Emit the raw source as a `text` code block and explain in prose what the diagram was supposed to show. |
| The user reports that a diagram did not render | The PWA failed to parse it | Ask them to paste the error shown in the source-view fallback in the PWA (the rose-tinted annotation above the raw code). Compare it with the Syntax Invariants: most failures are a missing header, mixed arrow types, a bare reserved word, or `{}` in a label. Re-emit the corrected fence in a fresh message — don't patch the previous one in place. |

---

## Further Reference

External resources (use for syntax lookup, not as import targets):
- [Mermaid Live Editor](https://mermaid.live) — paste a diagram and see if it parses before sending
- Mermaid v11 docs: [mermaid.js.org](https://mermaid.js.org/)
