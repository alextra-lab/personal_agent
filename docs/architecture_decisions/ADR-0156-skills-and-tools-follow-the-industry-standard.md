# ADR-0156: Skills and Tools Follow the Industry Standard — Agent Skills Folders Chosen by the Model, One Tool Contract, and One Permission Model That Asks the User

**Status:** Accepted — the owner decided every item on 2026-10-10. Implementation not started.
**Date:** 2026-10-10
**Deciders:** Owner (the directive, and an answer to each brief item, 2026-10-10), `master` (the commission, the production checks, the relay), `adr` seat at Opus 5.5 (author)
**Tags:** skills, tools, permissions, approvals, workers, governance, evaluation

**Umbrella:** FRE-1573. **Amends:** ADR-0063 Amendment A, rows A3 (D8.4), A4 (D9.3) and A5 (D8), recorded as ADR-0063 Amendment B. **Keeps:** ADR-0028's tier order.

---

## Context

### The directive

The owner, on FRE-1573:

- "Full stop on skills/tools. We move to the industry standard."
- "Skills/tools are broken. We must move to the industry standard. We stop testing until this is completed. All skills converted and the usage implemented."
- "We are chasing a problem that is broken at its root."
- On approvals: "We should apply the same functionality as the major harnesses. Let the user accept or deny."

Model testing is stopped until the gate in D10 passes.

### The evidence

The FRE-1517 audit (`docs/research/2026-10-10-fre-1517-tool-call-efficiency.md`, PR #1242) measured our tools and skills, not the models.

- **Fixable calls.** On Flash-Next, 55 of 151 calls (36%) in s2_tool and 65 of 144 calls (45%) in s3_logs_learning had a cause in a tool, skill, instruction, policy or the environment (G2).
- **Skills (G4).** Of 23 skills, 5 state how to check a result and 9 state what to do on a failure.
- **Keyword routing is a substring test** (`orchestrator/skills.py:389`). "logic" loads `query-elasticsearch`, and "Spanish" loads `query-tempo`.
- **No real progressive disclosure (G6).** Whole bodies are inlined, up to 8,192 tokens. The `bash` body is in every turn.
- **The hint loop (G6).** 366 of 438 tool results carried "[hint: skill X is available — call read_skill…]". The model called `read_skill` 2 times.
- **Tools (G5).** No edit tool exists. No tool shares an error shape. An error from the tool execution layer is cut to 150 characters before the model sees it (`orchestrator/tool_dispatch.py:304`).
- **Wrong telemetry.** A tool that returns an error dict is logged with `success=True` (`tools/executor.py:620-637`).
- **Permissions.** The bash allowlist checks only the first word (FRE-1572). An allowlist refusal tells the model only `approval_connection_lost` (G5). `run_python` with `network: true` needs no approval in NORMAL mode (G7).

### The approval setting in production

- Production runs with `AGENT_APPROVAL_UI_ENABLED=true` (`/opt/seshat/.env` line 627, and `printenv` in the running gateway, checked by master on 2026-10-10).
- The code default is `False` (`config/settings.py:3035`).
- With `False`, a call that needs approval runs without a prompt and logs `approval_ui_disabled_proceeding` (`tools/executor.py:312-320`). ADR-0063 Amendment A row A3 made this an explicit opt-out.

### The standards

Each was read on 2026-10-10. Where a page has no version, the access date is the version.

- **S1 — Agent Skills specification:** https://agentskills.io/specification (no version on the page; accessed 2026-10-10).
- **S2 — Agent Skills client guide:** https://agentskills.io/client-implementation/adding-skills-support (accessed 2026-10-10).
- **S3 — Anthropic Agent Skills docs:** https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview and `/best-practices` (accessed 2026-10-10). The `docs.claude.com` link on the ticket redirects here.
- **S4 — Anthropic, "Writing effective tools for agents":** https://www.anthropic.com/engineering/writing-tools-for-agents (published 2025-09-11).
- **S5 — Anthropic, "Define tools":** https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools (accessed 2026-10-10).
- **S6 — Claude Code permissions:** https://code.claude.com/docs/en/permissions (accessed 2026-10-10). The `/iam` link on the ticket redirects to code.claude.com.
- **S7 — Anthropic text editor tool, `text_editor_20250728`:** https://platform.claude.com/docs/en/agents-and-tools/tool-use/text-editor-tool
- **S8 — MCP specification 2026-07-28, tools:** https://modelcontextprotocol.io/specification/2026-07-28/server/tools

The reference `skills-ref` is not a gate: its README says it is "for demonstration purposes only".

### How the decisions were made

The adr seat posted a decision brief on FRE-1573 (2026-10-10 16:41 UTC, addendum 8b at 16:42). The owner answered every item (16:43 and 17:37 UTC):

- items 1–8: "Accept all (Recommended)", including deleting `seshat-delegate`;
- 7b: the primary gets `query_telemetry`: yes;
- item 9: a read-only file grant for the `general` worker;
- item 10: the proposed bounds, and the G2 and G3 runs are allowed as tool measurements while model testing is stopped;
- 8b: "Refuse it (Recommended)": fail closed, and amend ADR-0063 Amendment A row A3.

---

## Decision

### D1 — Skill format and location (S1)

1. Each skill is a folder: `docs/skills/<name>/SKILL.md`, with optional `scripts/`, `references/` and `assets/` (S1).
2. The frontmatter holds `name` and `description`, plus S1's optional `metadata` (a map of string to string). No other top-level field stays (D4).
3. A CI test enforces the S1 rules on every skill:
   - `name`: 1–64 characters, lowercase letters, digits and hyphens, the same as the folder name;
   - `description`: 1–1,024 characters, in the third person (S3);
   - the body: under 500 lines and under 5,000 tokens (S1, S3);
   - every file that the body names exists, one level below the skill folder (S1).
4. **Why the folder layout fixes a real defect.** `Dockerfile.gateway:66-72` copies `docs/skills/` but not `scripts/`. A script under `docs/skills/<name>/scripts/` ships in the gateway image with no Dockerfile change.

### D2 — The model chooses the skill (S2)

1. The catalog is in the cached system prompt: one line per skill, with `name` and `description` only (S2, about 50–100 tokens per skill).
2. The model loads a body with `read_skill` (D3). The `name` parameter is an enum of the valid skill names (S2).
3. These are deleted:
   - the keyword routing (`orchestrator/skills.py:389`) and the `keywords` field;
   - the hint loop (`orchestrator/tool_dispatch.py:280-298`);
   - the Haiku skill router (`skill_routing_mode`, `skill_routing_model_key`, `orchestrator/executor.py:6674-6714`);
   - the `bash` body that every turn carries today.
4. A loaded skill body is never pruned from the context of its turn (S2, step 5).
5. **Standard, quoted:** "Most implementations rely on the model's own judgment as the activation mechanism, rather than implementing harness-side trigger matching or keyword detection" (S2).
6. **Risk:** a small model can fail to load a skill that it needs. D10 G2 measures this. A fallback is added only if G2 fails.

### D3 — `read_skill(name, path?)` gives detail on demand (S2)

1. With no `path`, `read_skill` returns the body and a list of the skill's files. It does not read those files (S2: the tool lists the resources but "should not eagerly read them").
2. With a `path`, it returns that one file of that skill. It refuses any path outside the skill folder.
3. It works for the primary and for every worker type, with no filesystem access. S2: "Register a tool (e.g., activate_skill) … This is required when the model can't read files directly".

### D4 — The custom frontmatter fields

| Field today | Target |
|---|---|
| `when_to_use` | Merged into `description`. S1 defines it as "what the skill does and when to use it" |
| `keywords` | Deleted (D2) |
| `nudge` | Moved into the body |
| `canonical_patterns` | Moved into the body. No code reads it |
| `tools` | Becomes `metadata.requires-tools`, a comma-separated list. It filters the catalog per agent (D9). It never routes and never blocks |
| `known_bad_patterns` | Leaves the skills. Each of the 10 patterns becomes a deny rule on its tool with its reason (D8), or is dropped with a stated reason |

**Why `known_bad_patterns` goes.** Today the patterns act as a global bash blocklist, also when the skill is not loaded (`orchestrator/tool_dispatch.py:168-224`). The test is a substring test (`:195`). `self-telemetry`'s pattern `"from personal_agent"` blocks a `grep`, but its reason is about `run_python`.

### D5 — How every skill is converted

1. **Every agent skill gets a success section and a recovery section.** The success section states how to check the result. The recovery section states what to do on each failure, with a stop rule. `mermaid-diagrams` is the model to copy (G4).
2. **Every agent skill gets at least 3 evaluation scenarios** in `docs/skills/<name>/evals.yaml`. S3: "Create evaluations BEFORE writing extensive documentation." D10 G2 uses these scenarios.
3. **A body over the D1 limits is split into `references/`.** Today: `query-elasticsearch` (532 lines) and `artifact-design` (about 5,600 tokens).
4. **Fragile command sequences move into tested `scripts/`,** each with `--help` and plain error text (S1, S3: "Solve, don't defer").
5. **Every command in a body or in `scripts/` passes the permission rules** (D8.7). It comes out allow, or a documented ask.
6. **A skill that only explains one tool becomes that tool's description, and the skill is deleted.** S5: descriptions are "by far the most important factor in tool performance".
7. **The 4 `seshat-*` skills are for external agents.** They leave the agent's catalog. `seshat-delegate` is deleted (it says "Works Now: Nothing"). The other 3 move to `docs/external-skills/<name>/SKILL.md` in the S1 format.
8. **Two files are not skills.** `EMPIRICAL_TEST_RESULTS.md` moves to `docs/research/`. `SKILL_TEMPLATE.md` is replaced by an S1 template with the D5.1 sections and an `evals.yaml` example.

The migration table gives the verdict for each file.

### D6 — One tool contract (S4, S5, S8)

1. **One result shape.** On success: `{ok: true, ...}`. On failure: `{ok: false, error: {code, message, retryable, next_action}}`.
   - `code` is a stable string from a closed set per tool.
   - `next_action` tells the model the next call that can work, or says that none exists.
   - The shape maps onto S8: a tool execution error is a result with `isError: true` that carries "actionable feedback that language models can use to self-correct".
2. **The 150-character clip is removed** (`orchestrator/tool_dispatch.py:304`). An error message is bounded at 2,000 characters. Then a refusal keeps its fix, for example the allowed fields of `query_telemetry`.
3. **A failure is recorded as a failure.** A result with `ok: false` logs `success: false` on `tool_call_completed`.
4. **Every result has a size bound.** A result over its bound is truncated with text that names the next call, as `read` does today. No result exceeds 25,000 tokens (S4: "For Claude Code, we restrict tool responses to 25,000 tokens by default").
5. **Every description follows S5:** "at least 3–4 sentences", what the tool does, when to use it and when not, what each parameter means, and its limits. It holds 1–3 example calls in the text.
   - `input_examples` (S5) is an Anthropic API field. The default primary runs on llama.cpp, so the examples go in the description text.
6. **MCP tools** keep their transport. The MCP client maps `isError: true` and the error text onto the D6.1 shape.

### D7 — The tool set (S4, S7)

1. **An `edit` tool is added.** It replaces one exact string in one file. Zero matches or more than one match is an error that gives the count (S7: "ensure that there is exactly one match … or provide appropriate error messages"). It has the path governance of `write`. Evidence: 13 calls (G3 #6).
2. **The primary gets `query_telemetry`.** The tool loses `worker_only=True` (`tools/telemetry_query.py:470`). Evidence: 42 primary calls of `bash` + `curl` against a stale skill (G3 #1a).
3. **No new MCP server.** ADR-0028's tier order stays: native, then CLI with a skill, then MCP. We copy S8's tool shape, not its transport.
4. **A narrow tool replaces a shell recipe** where the audit shows repeated shell work. `query_telemetry` (D7.2) is the first case. No other case is decided here.

### D8 — One permission model (S6)

1. **Rules.** Governance holds `deny`, `ask` and `allow` lists of `Tool(pattern)` rules for every tool. The order is "deny, then ask, then allow. The first match in that order determines the outcome" (S6). Mode-specific rules from `config/governance/tools.yaml` stay, as rules scoped to a mode.
   - **The pattern grammar.** For `Bash`, `Bash(<words>)` matches a subcommand whose argument list equals `<words>`, and `Bash(<words> *)` matches one whose argument list starts with `<words>`. For every other tool, `Tool(p1=v1, p2=v2 …)` matches a call only if every named parameter matches, each with the same exact or `*` prefix forms. Parameters that the pattern does not name are free. A session rule (D8.3) uses the complete form `Tool(=<arguments>)`: it matches only a call whose full argument set, as canonical JSON, is equal. T2 writes the grammar into `config/governance/` with a test per form.
   - **The rules fail closed.** If the rules cannot be loaded, or the evaluation of a call raises an error, the call is denied with a D6 error. A tool with no rule at all, and a call that matches no rule, ask (D8.3). Nothing runs by default.
   - This replaces the fail-open branches of the path check: `_check_path_governance` returns "permitted" on a load error and on a missing policy (`tools/primitives/_governance.py:168-176`). After T2, a load error denies.
2. **Bash.** A command is split on "`&&`, `||`, `;`, `|`, `|&`, `&`, and newlines. A rule must match each subcommand independently" (S6). Deny and ask rules also apply to a command inside a subshell, a command substitution or a control-flow body (S6). FRE-1572's splitter becomes this matcher. The `auto_approve_prefixes` lists become allow rules. Where S6 leaves the parsing open, these rules apply:
   - The command is parsed with a shell parser, not split on text. Quotes and escapes are resolved before a subcommand is matched.
   - **A subcommand can match an allow rule only if all of these hold.** Every other subcommand asks. This is a closed condition, not a list of known tricks:
     - the parser classifies it as a simple command;
     - every word of it is a literal after quote removal: no parameter, arithmetic or command expansion, and no glob;
     - its first word is not an indirect-execution word: `eval`, `source`, `.`, `exec`, `command`, `builtin`, `env`, `nohup`, `timeout`, `nice`, `stdbuf`, `time`, `sudo`, `xargs`, or a shell (`bash`, `sh`, `zsh`, `dash`) with `-c`;
     - no argument is `-exec`, `-execdir`, `-ok` or `-okdir`;
     - no variable assignment stands in front of it (for example `PATH=/x ls`);
     - it has no redirection, except output or errors to `/dev/null` and `2>&1`. An input redirection from a file asks, because it feeds a local file to the command.
   - A command that the parser cannot parse fully asks.
   - A function definition, a heredoc, and process substitution (`<(…)`, `>(…)`) are not simple commands, so they ask.
   - An indirect-execution word can be allowed only by an allow rule whose pattern names the word and the full inner command, for example `Bash(timeout 30 git status)`. A rule of the form `Bash(<indirect word> *)` is rejected when the rules load.
   - **Migrating `auto_approve_prefixes` is not a copy.** A prefix that can run another program does not become an allow rule: today's list holds `env`, `awk`, `sed` and `python3` (`config/governance/tools.yaml:648-691`). They ask. `curl` stays allowed only when no argument sends a local file (`-d @…`, `--data*` with `@`, `-F …=@…`, `-T`, `--upload-file`, `-K`, `--config`). Otherwise it asks. T2 lists every prefix of today with its outcome in the PR.
3. **No match asks the user.** The approval card (FRE-1461, ADR-0063 Amendment A) shows the tool, the exact arguments, and the worker when a worker asks. The card offers "allow once" and "allow for this session".
   - "Allow for this session" adds a session allow rule for the exact tool and arguments of the call.
   - It is offered only when the call asks because no rule matched. A call that matches an explicit `ask` rule (for example `run_python` with `network: true`) offers "allow once" only. The order of D8.1 holds: an explicit ask rule is never overridden by an allow rule.
4. **No approval surface: deny (amends ADR-0063 Amendment A row A3).** An "ask" call is denied, never run, in each of these cases:
   - the turn has no PWA client (as row A2);
   - the layer has no transport or no session id (as row A2);
   - **`approval_ui_enabled` is `False`.** This replaces row A3. The log event becomes `approval_ui_disabled_denied`.

   The model gets a D6 error with the refused subcommand and the rule that refused it, not `approval_connection_lost`. Standard: S6's `dontAsk` mode "Auto-denies every call that would otherwise prompt". In S6 only a mode chosen by name removes prompts. A default value never does. The owner approved this amendment on FRE-1573 (2026-10-10 17:37 UTC).
5. **The eval stack needs explicit rules.** Row A3 said the eval stack sets the flag false on purpose (FRE-1505). After D8.4, the eval stack lists the calls that it runs as allow rules in its own rules file. No default turns prompts off.
6. **`run_python` with `network: true` is an ask rule in every mode.** Evidence: G7.
7. **CI test.** Every command in a skill body or a skill script is evaluated against the rules. It must come out allow, or an ask that the skill documents.
8. **Order.** FRE-1572 ships first, as the live fix.

### D9 — Workers

1. **Each worker type gets a catalog** of the skills whose `metadata.requires-tools` that type holds. A filtered skill is hidden (S2: "Hide filtered skills entirely").
2. **Every worker type gets `read_skill`,** the researcher too. `read_skill` reads only the bundled skill files, not private data, so FRE-1564's outbound/private split holds.
3. **Worker calls pass through the D8 rules.** An "ask" goes to the same card, per call, with the exact arguments and the worker named (brief item 9). This replaces row A4's "once per tool per turn" for ask rules, and it answers FRE-1565's scope item 2. The `operator` type and worker bash as `nobody` in its own workspace (FRE-1565) are unchanged.
4. **Read-only file grant (owner, 2026-10-10).** The `general` worker may `read` the files that its task names. It may not write them. The `general` worker holds private reads and no outbound tool, so the FRE-1564 split holds. The grant works by these rules:
   - The task spec carries a structured field `read_paths`: a list of absolute file paths. Paths in the task text grant nothing.
   - A directory grants nothing, and a glob grants nothing. Each entry names one regular file.
   - At grant time, each path is resolved to its real path, and the file's device and inode numbers are recorded. A path that does not resolve to a regular file is dropped from the grant, with a log event.
   - At read time, two checks must both pass. First, the real path of the requested path must equal a recorded real path. Second, `read` opens the file and the opened file's device and inode numbers must equal the recorded ones. A mismatch in either is refused with a D6 error. The first check refuses a hard link at another path. The second check refuses a file that was replaced after the grant, including by a symlink.
   - The path must also pass the read tool's path governance. The grant never widens it.
   - The grant ends with the task.

### D10 — The gate that resumes model testing

Model studies (FRE-1517 and its successors) resume only when every implementation ticket of this ADR is Done and all four checks pass. The owner set the bounds on 2026-10-10.

| Check | Measure | Bound |
|---|---|---|
| **G1 Conformance** | CI: every skill passes the D1 validator, and every registered tool returns the D6 shape on each of its error paths | 100% |
| **G2 Skill choice** | The production primary model on every D5.2 scenario, plus 20 prompts that need no skill | Right skill loaded ≥ 90% of scenarios. A skill loaded on ≤ 10% of the no-skill prompts |
| **G3 Fixable calls** | The PR #1242 audit re-run on the same s2_tool and s3_logs_learning scripts, with the same model (Flash-Next), the same `DEFINITIONS.md`, and a hand check of the same size (39 rows) | Fixable share ≤ 15% in each session (baseline 36% and 45%) |
| **G4 Prompt cost** | First-call prompt tokens per turn, median over the G3 runs | Not above the median of the baseline sessions `860d7435` and `514a039b` |

- The G2 and G3 runs are tool measurements, allowed by the owner while model testing is stopped. They run on the eval stack, never on production substrate (FRE-375).
- The G4 baseline is computed from stored data and recorded on the gate ticket before the run.
- A change of model, script or bound needs the owner's approval before the run.

---

## Migration table

Each row has a target state and the ticket that delivers it. The tickets T1–T9 are listed in the Implementation Notes.

### Every file in `docs/skills/` (25 files)

| File | Lines | Target | Ticket |
|---|---|---|---|
| `artifact-design.md` | 453 | **Convert.** Split the body (about 5,600 tokens) into `SKILL.md` and `references/` per library | T5 (layout), T7 (content) |
| `bash.md` | 120 | **Fold into the `bash` description, then delete.** Keep: where commands start, absolute paths, how ask and deny work | T3 (fold), T5 (delete) |
| `fetch-url.md` | 110 | **Fold into the `fetch_url` description, then delete.** It teaches `curl` as a fallback for a native tool | T3, T5 |
| `get_location.md` | 124 | **Fold into the `get_location` description, then delete** | T3, T5 |
| `infrastructure-health.md` | 98 | **Convert.** The `psql "$(…)" -c` probe moves into a tested script that passes the rules | T5, T6 |
| `list-directory.md` | 137 | **Fold one line (`find … \| wc -l` to count) into the `bash` description, then delete** | T3, T5 |
| `mermaid-diagrams.md` | 391 | **Convert.** The `mmdc` validation moves into `scripts/` | T5, T7 |
| `neo4j-direct.md` | 138 | **Convert.** The Cypher call moves into `scripts/` | T5, T6 |
| `personal-history-recall.md` | 135 | **Fold the "we/I/my → this tool, else `search_memory`" rule into the `recall_personal_history` and `search_memory` descriptions, then delete** | T3, T5 |
| `query-elasticsearch.md` | 532 | **Convert.** Leads with `query_telemetry`. Split into `SKILL.md` and `references/`. Builds on FRE-1568's content fix | T5, T6 |
| `query-tempo.md` | 314 | **Convert.** Builds on FRE-1568's model-call recipe | T5, T6 |
| `read-write.md` | 134 | **Fold into the `read`, `write` and `edit` descriptions, then delete** | T3, T5 |
| `run-python.md` | 82 | **Fold into the `run_python` description, then delete.** Keep: no state between calls, network is an ask | T3, T5 |
| `self-telemetry.md` | 298 | **Convert.** Leads with `query_telemetry`. ES recipes shared with `query-elasticsearch` are linked, not copied | T5, T6 |
| `sequential-thinking.md` | 103 | **Convert** (an instruction skill with no tool) | T5, T7 |
| `seshat-delegate.md` | 64 | **Delete** (owner, 2026-10-10) | T5 |
| `seshat-knowledge.md` | 108 | **Move** to `docs/external-skills/seshat-knowledge/SKILL.md`, S1 format, out of the agent catalog | T5 |
| `seshat-observations.md` | 186 | **Move** to `docs/external-skills/seshat-observations/SKILL.md` | T5 |
| `seshat-sessions.md` | 100 | **Move** to `docs/external-skills/seshat-sessions/SKILL.md` | T5 |
| `system-diagnostics.md` | 85 | **Convert** | T5, T6 |
| `system-metrics.md` | 121 | **Convert** | T5, T6 |
| `turn-forensics.md` | 150 | **Convert** | T5, T6 |
| `web-search.md` | 94 | **Fold into the `web_search` description, then delete.** Keep: real-world facts go here, not to `search_memory`, and what `categories` returns | T3, T5 |
| `EMPIRICAL_TEST_RESULTS.md` | 193 | **Move** to `docs/research/` (not a skill) | T5 |
| `SKILL_TEMPLATE.md` | 88 | **Replace** with an S1 template (D5.8) | T5 |

Result: 11 agent skills, 3 external skills, 8 skills folded into tool descriptions, 1 deleted.

### Every tool that the gateway can register (28 rows: 26 registered in production, 1 conditional, 1 new)

Source: `tools/__init__.py:86-183`, and the startup log of the running gateway on 2026-10-10 (`mcp_tools_discovered count=3`, `primitive_tools_registered count=5`, `location_tool_registered`, `notes_tools_registered`, `artifact_tools_registered`). Every tool also gets its D8 rules (T2) and the D6 contract (T3). The table names the changes beyond that.

| Tool | Target beyond D6 and D8 | Ticket |
|---|---|---|
| `search_memory` | Description takes the "shared graph, not your own history" rule | T2, T3 |
| `recall_personal_history` | Description takes the `personal-history-recall` skill | T2, T3 |
| `web_search` | Description takes the `web-search` skill and states what `categories` returns | T2, T3 |
| `perplexity_query` | Contract only | T2, T3 |
| `get_library_docs` | Contract only | T2, T3 |
| `fetch_url` | Description takes the `fetch-url` skill | T2, T3 |
| `query_telemetry` | Registered for the primary (D7.2). Refusals keep the allowed fields (D6.2) | T2, T3, T4 |
| `create_linear_issue` | Ask rule (a write) | T2, T3 |
| `find_linear_issues` | Contract only | T2, T3 |
| `list_linear_projects` | Contract only | T2, T3 |
| `create_linear_project` | Ask rule (a write) | T2, T3 |
| `notes_write` | Contract only | T2, T3 |
| `notes_search` | Contract only | T2, T3 |
| `artifact_write` | Contract only. `TerminalToolError` maps onto D6.1 | T2, T3 |
| `artifact_list` | Contract only | T2, T3 |
| `artifact_read` | Contract only | T2, T3 |
| `artifact_draft` | Contract only | T2, T3 |
| `get_location` | Description takes the `get_location` skill | T2, T3 |
| `read` | `general` worker read-only grant on task-named paths (D9.4). Description takes `read-write` | T2, T3, T8 |
| `write` | Description takes `read-write` | T2, T3 |
| `bash` | Per-subcommand rules (D8.2), on FRE-1572. Description takes `bash` and `list-directory` | T2, T3 |
| `run_python` | `network: true` is an ask rule (D8.6). Description takes `run-python` | T2, T3 |
| `read_skill` | Rewritten as `read_skill(name, path?)` with an enum (D3) | T5 |
| `mcp_query-docs` | D6.6 mapping. Overlaps `get_library_docs` (Context7): open question O1 | T2, T3 |
| `mcp_resolve-library-id` | D6.6 mapping. Overlaps `get_library_docs`: O1 | T2, T3 |
| `mcp_sequentialthinking` | D6.6 mapping. Overlaps the `sequential-thinking` skill: O1 | T2, T3 |
| `expand_tool_result` | **Not registered in production** (see below). Contract only, and the AC-1 test covers it | T2, T3 |
| `edit` | **New** (D7.1). Built on the contract and the rules from the start | T4 |

**Not registered in production:** `expand_tool_result` registers only with `tool_result_compression_enabled`, which is off (ADR-0085 is parked). It stays in the table and in the AC-1 test, so that it conforms if it returns. The `mcp_browser_*` entries in `tools.yaml` have governance rows, but the gateway did not discover those tools.

**Open question O1 (for the owner, not decided here).** Three MCP tools overlap native ones. S4 warns against overlapping tools. Removing them changes the MCP server set, which the owner did not decide in this brief. Until the owner decides, they stay under the contract.

---

## Alternatives Considered

### Option 1: Keep the flat files and add fields (brief item 1B)

**Description:** keep `docs/skills/<name>.md` and add the missing fields.

**Pros:** no file moves, and fewer reader changes.

**Cons:** scripts still do not ship in the gateway image. The files do not match S1, so no S1 client or validator can read them.

**Why Rejected:** the owner asked for the industry standard. The folder layout is the standard and fixes the image defect.

### Option 2: Keep harness routing, on whole words (brief items 2B, 2C)

**Description:** keep `hybrid` mode, or keep keyword routing with whole-word matches as a fallback.

**Pros:** a small model gets a skill even when it does not choose one.

**Cons:** routing inlines whole bodies (G6) and fires on unrelated words (G4). Whole-word matches still fire on "model" and "draw". S2 says most implementations rely on the model's own judgment.

**Why Rejected:** G2 measures whether the model chooses well. A fallback is added only if G2 fails.

### Option 3: Fix only the clip and the messages (brief item 6B)

**Description:** remove the 150-character clip and rewrite the worst error texts.

**Pros:** small and fast.

**Cons:** the telemetry stays wrong (`success: True` on an error), and no tool tells the model whether to retry.

**Why Rejected:** S4 and S8 define errors as actionable feedback. One shape is the only way to test every error path (G1).

### Option 4: FRE-1572 only, with the per-tool rules unchanged (brief item 8B)

**Description:** close the bash bypass and keep each tool's own rules.

**Pros:** the live hole closes with the least change.

**Cons:** `run_python` network stays unasked, refusals stay opaque, and each tool keeps its own partial model.

**Why Rejected:** the owner asked for the major harnesses' model: "Let the user accept or deny".

### Option 5: Keep row A3 and record the risk (brief item 8B, option B)

**Description:** `approval_ui_enabled = False` keeps running "ask" calls without a prompt.

**Pros:** the eval stack needs no rules file.

**Cons:** a deployment that does not set the flag runs every gated call unasked. The code default is `False`.

**Why Rejected:** the owner chose to fail closed. In S6 only a named mode removes prompts.

### Option 6: Workers get no skills, and file tasks stay on the primary (brief item 9B)

**Description:** workers keep no catalog, and a worker never reads a task file.

**Pros:** the smallest worker surface.

**Cons:** 21 worker calls searched for a file that they could not open (G3 #2). A worker without a skill repeats the primary's errors.

**Why Rejected:** the owner chose the read-only file grant.

---

## Consequences

### Positive Consequences

- A skill loads when the model asks for it. Unrelated words load nothing.
- Every refusal and every error tells the model what to do next.
- Telemetry counts a failed call as a failure.
- One rule set decides every call of the primary and every worker, and the user decides every "ask".
- A deployment that forgets the approval flag is closed, not open.
- Skill scripts ship in the gateway image.

### Negative Consequences

- The skill loader, the prompt assembly, and the 18 files that read `docs/skills/` today change in one ticket (T5).
- The primary's prompt gets the `query_telemetry` and `edit` descriptions on every turn. G4 bounds the total.
- The eval stack needs its own allow rules before its next run (D8.5).
- A harness turn denies more calls than today. It still denied most of them under row A2.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| A small primary model does not load a skill that it needs | High | G2 measures it. A fallback is added only if G2 fails |
| The eval stack breaks after D8.4 | Medium | T2 ships the eval rules file in the same change |
| A rule set that is too strict floods the owner with cards | Medium | The allow rules start from today's `auto_approve_prefixes`. "Allow for this session" adds a rule |
| G3 cannot re-run with Flash-Next | Medium | The gate ticket stops and asks the owner. No silent model change |
| FRE-1568 and T6 edit the same two skills | Low | T6 is blocked by FRE-1568 |

---

## Implementation Notes

### The tickets, in order

| # | Ticket | Delivers | Blocked by |
|---|---|---|---|
| T1 | FRE-1572 (exists) | The live bash bypass fix. Its splitter becomes D8.2's matcher | — |
| T2 | Permission model | D8 (including the fail-closed load and the bash parser rules), D4 (`known_bad_patterns` to deny rules), the eval rules file | T1 |
| T3 | Tool contract | D6, and the 8 skills folded into descriptions (D5.6) | T2 |
| T4 | `edit` tool, and `query_telemetry` for the primary | D7.1, D7.2 | T3 |
| T5 | Skill loader and layout | D1, D2, D3, D4, D5.7, D5.8: every file moved, deleted or replaced | T3 |
| T6 | Conversion: telemetry and host skills | D5.1–D5.5 for 8 skills | T4, T5, FRE-1568 |
| T7 | Conversion: authoring skills | D5.1–D5.5 for `artifact-design`, `mermaid-diagrams`, `sequential-thinking` | T5 |
| T8 | Workers | D9 | T5 |
| T9 | Gate run | D10 | T6, T7, T8 |

T4 comes before T6 so that the telemetry skills can lead with `query_telemetry`. The owner's brief order put the tools after the conversion. This order avoids writing those skills twice.

### Code that changes (indicative, each ticket names its own)

- `orchestrator/skills.py`, `orchestrator/tool_dispatch.py`, `orchestrator/prompts.py`, `orchestrator/executor.py` (skill routing), `orchestrator/sub_agent.py` (worker catalogs).
- `tools/read_skill.py`, `tools/executor.py`, `tools/telemetry_query.py`, every tool definition.
- `config/governance/tools.yaml`, `config/settings.py` (the skill routing settings go).
- `docs/reference/TOOL_INTEGRATION_GUIDE.md` (the Tier 2 skill format).

### Not in scope

- FRE-226 (self-updating skills). The S1 format allows it later.
- The search backend (G3 #3) and overlapping research (G3 #4). They are not tool or skill defects.
- An egress-only sandbox network (G7). Master holds it for the owner.

---

## Verification / Acceptance Criteria

These are the ADR's own criteria. They are adjudicated on FRE-1573 after T1–T9 are Done and deployed. AC-1 to AC-4 are the D10 gate.

- **AC-1 — every skill and every tool conforms (G1).** **Check:** three CI tests.
  - **Skills.** The D1 validator runs over `docs/skills/*/SKILL.md` and `docs/external-skills/*/SKILL.md`. It asserts:
    - exactly the 11 agent skills and the 3 external skills of the migration table;
    - a success section and a recovery section in each agent skill (D5.1);
    - an `evals.yaml` with at least 3 scenarios in each agent skill (D5.2);
    - that every command in a body or a script comes out allow, or an ask that the skill documents, under the D8 rules (D8.7).
  - **Tools.** A contract test builds the registry with every registration flag on, so that conditional tools are included. It fails if the registry and the 28 rows of the migration table differ. For each tool it asserts:
    - the success shape and the failure shape of D6.1;
    - a failure can only be built from the tool's error-code type, so an undeclared code cannot be returned (a type check, not a declaration that the test trusts);
    - every line that builds a failure is executed by a test, shown by a coverage report over those lines. One test per code is not enough when several paths share a code;
    - a result over the tool's bound is truncated with next-call text, and no result exceeds 25,000 tokens (D6.4);
    - the description has at least 3 sentences and at least 1 example call (D6.5).
  - **Dispatch.** The failure paths outside the tools give the D6.1 shape: an unknown tool, a missing parameter, a governance refusal, a permission deny, a timeout and an exception in the executor.

  *Fails if* any assertion fails. Seeded negatives: a skill with a 1,025-character `description` fails the validator, and a tool that returns a plain error dict fails the contract test.
- **AC-2 — the model chooses the right skill (G2).** **Check:** the T9 run over every `evals.yaml` scenario (at least 33) and 20 no-skill prompts, scored from `read_skill_invoked` events. At least 10 of the 20 no-skill prompts contain a word that today's `keywords` match.
  - A scenario counts as right only if the expected skill loads and no other skill loads.
  - A no-skill prompt counts as wrong if any skill loads.

  *Fails if* fewer than 33 scenarios run, fewer than 90% of scenarios are right, or more than 2 of the 20 no-skill prompts load a skill.
- **AC-3 — fixable calls fall (G3).** **Check:** the PR #1242 audit method re-run per D10. *Fails if* the fixable share is above 15% in either session.
- **AC-4 — the prompt does not grow (G4).** **Check:** the median first-call `prompt_tokens` per turn over the G3 runs, against the baseline recorded before the run. *Fails if* the median is above the baseline.
- **AC-5 — every "ask" reaches the user, and runs only on the user's yes.** **Check:** tests for the primary and for a worker.
  - With a PWA client, a call that matches no rule shows the card with the exact arguments. It runs on "allow once" and is refused on deny. *Fails if* the card is not shown, or the call runs before the answer.
  - In each case of D8.4 (`approval_ui_enabled=False`, no session id, no transport, a transport whose PWA client is gone), the same call is denied with a D6 error. *Fails if* it runs.
  - If the rules fail to load, or the evaluation raises, the call is denied. A tool with no rule asks. *Fails if* either runs without an ask.
  - After "allow for this session" on an unmatched call, the same call runs without a card.
  - A call that matches an explicit ask rule offers no "allow for this session". The test then adds a session rule that equals that call, and runs the call again: the card is still shown.

  *Fails if* any of these differs.

  Seeded negative: restoring row A3's branch makes the second test fail.
- **AC-6 — the bash matcher allows only plain commands.** **Check:** a table test under the allow rule `Bash(ls *)` only. The table has one row for each of:
  - each separator: `&&`, `||`, `;`, `|`, `|&`, `&`, newline, each followed by `rm x`;
  - each FRE-1572 form, and `rm x` inside a subshell, a command substitution, backticks, and an `if`, `for` and `while` body;
  - each failed condition of D8.2: an expansion in a word, a glob, a variable assignment (`PATH=/x ls`), an output redirection to a file, an input redirection from a file (`ls < /etc/passwd`), a function definition, a heredoc, both process substitutions, and `find . -exec`, `-execdir`, `-ok` and `-okdir` under an added allow rule `Bash(find *)`;
  - every listed indirect-execution word in front of `ls` (for example `env ls`, `timeout 5 ls`, `command ls`). These rows test the guard: a matcher that strips the wrapper and matches `ls` would allow them;
  - the rules loader, given `Bash(env *)`, `Bash(timeout *)` and `Bash(awk *)`, rejects each one;
  - one input that the parser cannot parse.

  Where the form permits it, a row starts with `ls`.

  *Fails if* any row is allowed. Control rows: `ls -la /tmp` is allowed, and `ls -la /tmp > /dev/null` is allowed. Seeded negative: a matcher that checks only the first word fails at least 10 rows.
- **AC-7 — the user's message selects no skill.** **Check:** a test assembles the primary's first-call messages twice with the same session state and two different user messages: one with no skill word, and one with every `keywords` entry of every skill today. T5 saves that list as test data before it deletes the field. *Fails if* the system part of the two prompts differs, or either holds a skill body. This covers every term, not a sample.
- **AC-8 — the model sees the fix, and telemetry sees the failure.** **Check:** the AC-1 tool and dispatch tests also assert, for each driven failure, that the tool message to the model holds the full `message` (up to 2,000 characters) and that `tool_call_completed` logs `success: false`. Named instance: a `query_telemetry` call with a field that is not allowed returns the list of allowed fields. *Fails if* any failure is clipped below its message length, or logs `success: true`.
- **AC-9 — the worker file grant is read-only and bounded.** **Check:** a `general` worker test with a task whose `read_paths` names one file. *Fails if* the worker cannot read that file, or if any of these succeeds:
  - a read of a file that the task does not name;
  - a write or an edit of the named file;
  - a read through `../` or a symlink that resolves to another file;
  - a read after the named file is replaced by a symlink to another file;
  - a read of a directory named in `read_paths`;
  - a read through a hard link to the named file at another path;
  - a read of the named file after the task ends;
  - a read after the named file is replaced by another regular file at the same path (a rename over it).
- **AC-10 — each worker type sees its own catalog.** **Check:** a test builds the first-call prompt and the tool list of each worker type. *Fails if* any of these holds:
  - a type's catalog lists a skill whose `metadata.requires-tools` that type lacks;
  - a type's catalog omits a skill whose tools it holds;
  - any type, the researcher included, lacks `read_skill`;
  - the researcher's `read_skill` returns a file outside the skill folders.

---

## References

- FRE-1573 — the umbrella, the brief, and the owner's answers (2026-10-10).
- `docs/research/2026-10-10-fre-1517-tool-call-efficiency.md` — the evidence (G1–G7), PR #1242.
- ADR-0028 — tool integration tiers (kept).
- ADR-0063 — primitive tools, Amendment A (row A3 amended by D8.4).
- ADR-0085 — tool-result digestion (parked; `expand_tool_result`).
- ADR-0150 — worker types (D2); FRE-1564 and FRE-1565 amend it.
- FRE-1461 — the worker approval broker.
- FRE-1564 — the outbound/private worker split.
- FRE-1565 — worker side-effecting tools.
- FRE-1567 — `query_telemetry` with Tempo.
- FRE-1568 — the telemetry skill content fix.
- FRE-1572 — the bash allowlist bypass.
- FRE-226 — self-updating skills (out of scope).
- Standards S1–S8, listed in the Context.

---

## Status Updates

### 2026-10-10 - Accepted
**Changed By:** Owner (decisions), `adr` seat (author)
**Reason:** the owner answered every item of the FRE-1573 decision brief, including the A3 amendment.
