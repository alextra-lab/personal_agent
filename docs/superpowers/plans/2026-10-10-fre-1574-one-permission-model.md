# FRE-1574 — One permission model (ADR-0156 T2)

**Ticket:** FRE-1574 (umbrella FRE-1573). **Design:** ADR-0156 D8 and D4, ADR-0063 Amendment B.
**Owner answers at pickup (2026-10-10, on the ticket):** AC-5 uses all 372 stage-3 calls minus the
classes that the ADR makes ask · the eval stack denies those classes, per the ADR · the worker card
is per call in this ticket (T8 scope item 3 moves here).

## Scope summary

- One rule set, `config/governance/permissions.yaml`: `deny`, `ask`, `allow` lists of `tool(pattern)`
  rules, evaluated deny → ask → allow. Mode scoping per rule.
- Bash: a `tree-sitter-bash` parse, the D8.2 closed condition, and FRE-1572's scanner and option
  rules as a second, independent check. Either check refusing makes the subcommand ask.
- Fail closed: a rule-load error or an evaluation error denies. `_check_path_governance` denies on a
  load error and on a missing policy.
- The card: tool, exact arguments, worker; "allow once"; "allow for this session" only for a call
  that matched no rule. Worker calls ask per call through the same card.
- No approval surface denies (D8.4); event `approval_ui_disabled_denied`; the model gets the refused
  subcommand and the rule in a D6-shaped error, not `approval_connection_lost`.
- `known_bad_patterns` leave the skills and the global substring check goes (D4).
- Eval rules file, selected by a new setting that the eval compose sets.

## Design

### Rule grammar (`src/personal_agent/governance/permissions.py`)

| Form | Matches |
|---|---|
| `tool` | every call of `tool` |
| `bash(<words>)` | a subcommand whose literal argument list equals `<words>` (shlex-split) |
| `bash(<words> *)` | a subcommand whose argument list starts with `<words>` |
| `tool(k=v, …)` | a call where every named parameter matches. `v` is `true`/`false`/`null`/a number (typed equality), a bare string (exact), `"json string"` (exact), or either string form followed by `*` (prefix). Unnamed parameters are free |
| `tool(=<canonical JSON>)` | session rules only: the full argument set, as canonical JSON, is equal |

Loader rejections (each with a test):
- an allow rule `bash(<w> *)` whose first word is an indirect-execution word (`eval`, `source`, `.`,
  `exec`, `command`, `builtin`, `env`, `nohup`, `timeout`, `nice`, `stdbuf`, `time`, `sudo`, `xargs`,
  `bash`, `sh`, `zsh`, `dash`) or a program-running word (`awk`, `gawk`, `mawk`, `nawk`, `sed`,
  `python`, `python3`, `perl`, `ruby`, `node`). An exact allow rule may name these words (D8.2:
  `bash(timeout 30 git status)`).
- `*` anywhere but as the last bash word; `tool()`; a `=` session form in the file; a deny rule
  without a `reason`; an unknown mode; an unknown key.

File shape: each list item is `{rule: str, modes?: [MODE…], reason?: str}`. `modes` absent = every
mode.

Evaluation, `evaluate_permission(tool, args, mode, rules, session_allows) -> PermissionDecision`:
1. Any deny rule matches → `deny`.
2. Any ask rule matches → `ask`, `explicit=True` (no session option).
3. A session allow rule equals the call → `allow`.
4. Bash: every top-level subcommand matches an allow rule under the closed condition → `allow`.
   Other tools: an allow rule matches → `allow`.
5. Otherwise `ask`, `explicit=False`, with the first refused subcommand and why.

For bash, deny and ask rules are tested against every command node in the tree, nested ones
included (subshell, substitution, control-flow body). A command with non-literal words is tested on
its leading literal words, with prefix semantics only.

`PermissionDecision` (frozen): `decision: Literal["allow","ask","deny"]`, `rule: str | None`,
`refused: str | None` (the subcommand or argument that decided it), `reason: str`, `explicit: bool`.

Fail closed: `decide_permission(...)` loads the rules and evaluates. Any exception → `deny` with
`reason="permission rules could not be evaluated: <error>"` and `refused` set to the call's command
(bash) or arguments.

Rules are loaded per call (no cache): YAML of about 200 lines, so a broken file denies the next call
instead of a stale cache allowing it. `settings.extra_permission_rules_path` (env
`AGENT_EXTRA_PERMISSION_RULES_PATH`) appends a second file's lists.

### Bash matcher (`src/personal_agent/governance/bash_matcher.py`)

`parse_bash(command) -> ParsedBash` with `top_level: tuple[Subcommand, ...]`,
`all_commands: tuple[Subcommand, ...]`, `refusal: str | None`, `refused_text: str | None`.

- `tree_sitter_bash` parse. `root.has_error` → refusal `unparseable`.
- Top-level walk: `program` → statements; `list` (`&&`, `||`) and `pipeline` (`|`, `|&`) recurse;
  `;`, `&`, newline separate. `comment` is skipped. `redirected_statement` with a `command` body →
  that command with its redirects. Any other statement (subshell, compound, if/for/while/case,
  function definition, negation, test, declaration, bare assignment, heredoc or herestring redirect,
  a redirect on a non-command body) → whole-command refusal naming the construct.
- `Subcommand`: `text`, `words: tuple[str, ...] | None` (None if any word is not literal),
  `literal_prefix`, `refusal: str | None` (the closed condition):
  - a `variable_assignment` child → `assignment`;
  - a word that is not literal after quote removal → `expansion` / `glob` / `brace` / `tilde`.
    Literal = `word` (backslash escapes resolved; an unescaped `*?[{}~` or `$`/backtick refuses),
    `raw_string`, `string` with only `string_content` and no `$`/backtick, `number` of digits, or a
    `concatenation` of these. `ansi_c_string`, every expansion and substitution node → refusal;
  - first word's basename is an indirect-execution word → `indirect` (only an exact allow rule can
    then match);
  - an argument `-exec`, `-execdir`, `-ok`, `-okdir` → refusal;
  - a redirect other than `>`/`>>`/`&>`/`&>>`/`2>`/`1>` to `/dev/null`, or exactly `2>&1` → refusal;
  - FRE-1572 `option_hazard(words)` not `None` → refusal (curl local file, find -delete, sort, rg
    --pre, git --output, mmdc, uniq).
- Second check: FRE-1572 `scan_segments(command)`. Any unsafe segment, or a segment count that
  differs from the top-level subcommand count, → whole-command refusal `parser_disagreement`.

### Executor (`src/personal_agent/tools/executor.py`)

`_check_permissions` keeps the mode check, then calls `decide_permission`. Deny → refused with a
D6 error payload. Ask → surface check in order: `approval_ui_enabled` false (`approval_ui_disabled_denied`),
no session id, no transport; then the card. `approve` runs once; `approve_session` adds a session
rule when the call was unmatched (`explicit=False`), else runs once; anything else denies. Every
denial carries `{code, message, retryable, next_action}` with the refused subcommand and the rule.
`approved_upstream` is removed. New keyword `worker: str | None` names the worker on the card.

`ToolResult.metadata["permission_error"]` carries the D6 error. `dispatch_tool_call` sends it to the
model as `{"ok": false, "error": …}`, bounded at 2,000 characters, outside the 150-character clip
(T3 removes the clip everywhere).

### Session rules (`src/personal_agent/governance/session_rules.py`)

In-process map `session_id → set[canonical rule text]`. Lost on restart, which ends "this session"
as far as the gateway is concerned.

### Workers

- `sub_agent.py` drops the broker gate and passes `worker=<worker type>` to `dispatch_tool_call`.
- `get_shared_tool_execution_layer()` builds the layer with `AGUITransport()`, like the primary.
- Removed: `orchestrator/sub_agent_approval.py`, the carrier in `executor.py`, the
  `sub_agent_tool_approval` constraint (`constraint_options.py`, `transport/events.py`),
  `sub_agent_tool_requires_approval`.

### Card (PWA + event)

- `ToolApprovalRequestEvent` gains `worker: str | None` and `session_option: bool`.
- `ApprovalModal`: arguments shown open; worker line; buttons Deny / Allow once / Allow for this
  session (the last only with `session_option`). `handleApprovalDecision` sends
  `approve` / `approve_session` / `deny`. `ApprovalDecision.decision` gains `approve_session`.

### Config

- `tools.yaml`: `requires_approval`, `requires_approval_in_modes`, `auto_approve_prefixes` removed
  from every tool. `ToolPolicy` drops the three fields and rejects them by name (a validator points
  to `permissions.yaml`).
- `permissions.yaml`: translated from today's fields for every registered tool (native list + the
  three live MCP tools), with these ADR changes: bash prefixes minus `env`/`awk`/`sed`/`python3`;
  `create_linear_issue` and `create_linear_project` ask (migration table); `run_python(network=true)`
  ask in every mode. A tool with an approval mode list gets `ask` in those modes and `allow` in the
  rest.
- `permissions.eval.yaml` + `AGENT_EXTRA_PERMISSION_RULES_PATH` on the eval gateway anchor in
  `docker-compose.eval.yml`.
- `_governance.py`: a load error and a missing policy return a refusal dict.

### `known_bad_patterns` (D4)

All 10 apply to `bash`, and all are substring tests on the command. The grammar matches words, not
substrings. Each is dropped, with the reason in the PR: 1–7, 9, 10 are wrong-endpoint or wrong-field
hints, which the server's own error reports (T3 removes the clip that hid it), and T6 rewrites the
skill content. 8 (`from personal_agent`) blocks a `grep` for a reason about `run_python`. The field
leaves `SkillDoc` and the three skill files. The check leaves `tool_dispatch.py`.

### Eval turn-validity detector

`scripts/eval/approval_denial.py` reads the rules (not the removed fields) for the approval-capable
set, and counts `approval_ui_disabled_denied` and the new D6 error text.

## Steps (TDD: each test fails first)

1. Dependency: `uv add tree-sitter tree-sitter-bash`. Verify: `uv run python -c "import tree_sitter_bash"`.
2. `tests/personal_agent/governance/test_permission_rules.py` — grammar per form, loader
   rejections, order, modes, session form. Then `permissions.py`.
3. `tests/personal_agent/governance/test_bash_matcher.py` — AC-6 table under `bash(ls *)` (+
   `bash(find *)`), control rows, the seeded first-word negative (≥ 10 rows fail). Then
   `bash_matcher.py`.
4. `permissions.yaml` + translation; `tests/.../test_permissions_config.py` — AC-2 rows, every
   registered tool has a rule, retired keys rejected (seeded negative), `run_python(network=true)`
   asks in every mode (AC-6).
5. Executor + session rules + D6 payload: `tests/test_tools/test_permission_ask_flow.py` — AC-3/AC-4
   (ADR AC-5) for the primary and a worker, with a fake transport. Then the executor change.
6. Workers: remove the broker; worker name threaded; shared layer gets a transport. Update or
   delete the broker tests.
7. Card: event fields, PWA modal + hook, `ApprovalDecision` literal. PWA unit test for the modal
   buttons; `npm run lint`, `npm test`, Playwright suite.
8. `known_bad_patterns` removal + AC-7 test.
9. `_governance.py` fail closed + test.
10. Eval: rules file, setting, compose, `approval_denial.py`; AC-5 fixture test
    (`tests/fixtures/fre1574_stage3_calls.json`, tool + arguments only).
11. D8.7 skill-body test over `docs/skills/*.md` fenced `bash` blocks.
12. FRE-1572 tests move to the new entry point; `bash_allowlist.check_segment_allowlist` and the
    re-exports in `bash.py` go.
13. Docs: bash tool description, settings description, `TOOL_INTEGRATION_GUIDE.md` permission
    section.

Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`,
`cd seshat-pwa && npm run lint && npm test` and the Playwright suite.

## Acceptance criteria → proof

| AC | Proof |
|---|---|
| AC-1 bash matcher | `test_bash_matcher.py` table + loader rejections + seeded first-word matcher |
| AC-2 prefix review | `test_permissions_config.py::test_ac2_*` on the production file |
| AC-3 card | `test_permission_ask_flow.py`, primary and worker |
| AC-4 fail closed | same file: flag false, broken rules file; message holds the subcommand, not `approval_connection_lost` |
| AC-5 eval stack | fixture test over 372 calls with base + eval rules |
| AC-6 run_python network | `test_permissions_config.py` + flow test in NORMAL |
| AC-7 no blocklist | `grep "from personal_agent" x.py` allowed; no `known_bad_patterns` left |
| AC-8 live | post-deploy owner PWA turn (runbook in the handoff) |

## Risks

- A parser differential between tree-sitter and bash. Mitigation: the FRE-1572 scanner must agree,
  and either refusal asks. Nothing becomes allow that either check refuses.
- A worker deadline can cancel a card wait. The call then does not run.
- More cards in production for commands that FRE-1572 allowed with `/tmp` redirects (the closed
  condition allows only `/dev/null`). This is the ADR's design.
