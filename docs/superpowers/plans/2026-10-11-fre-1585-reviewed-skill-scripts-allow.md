# FRE-1585 — Reviewed skill scripts run without a card

**Ticket:** FRE-1585 (Approved, owner decision 2026-10-11). **Design intent:** ADR-0156 D8.2.
**Tier:** Complex (security: it widens the allow list). Codex plan review and security review required.

## Goal

The four tested, read-only skill scripts run without an approval card. Nothing else gains an allow.

Scripts in scope (the mermaid validator of FRE-1579 is not on `main`; it gets its rule in the PR
that reviews it):

- `docs/skills/infrastructure-health/scripts/probe_services.py`
- `docs/skills/neo4j-direct/scripts/cypher.py`
- `docs/skills/query-elasticsearch/scripts/es_index_granularity.py`
- `docs/skills/turn-forensics/scripts/call_timeline.py`

## Facts checked before the plan

- The bash tool runs `/bin/bash -o pipefail -c <command>` with no `cwd=`, so every call (worker
  calls included) starts in the gateway's directory, `/app` (`Dockerfile.gateway` `WORKDIR /app`).
  A relative path `docs/skills/...` resolves to `/app/docs/skills/...`, which the image copies.
- `cd` has no allow rule, so a command cannot change directory without a card.
- The uvicorn worker has `PATH=/app/.venv/bin:...`, so `python3` is the venv interpreter, with the
  `neo4j` driver. A `PATH=` assignment or `export` asks.
- `write` and `edit` are allowed in NORMAL with `allowed_paths: /app/**`, but `_check_durability`
  refuses a path under `/app` outside a mounted volume. `/app/docs/skills` and `/app/.venv` are
  not mounted. The durability check fails open when `/proc/self/mountinfo` cannot be read.
- `python3 <script>` puts the script directory first on `sys.path`. The venv disables the user
  site directory.
- **A hole in `cypher.py` (confirmed on the test Neo4j, read access mode):**
  `RETURN apoc.cypher.runFirstColumnSingle("CALL dbms.listConfig() YIELD name RETURN count(name)", {})`
  returned 171. The procedure check reads tokens and skips strings, so a blocked procedure inside
  the string of an `apoc.cypher.*` function runs. `apoc.load.json` (a URL fetch) runs the same way.
  APOC core is installed (`NEO4J_PLUGINS: ["apoc"]`).

## Steps

1. **Loader: one narrow rule form** — `src/personal_agent/governance/permissions.py`.
   - In `_check_allow_rule`, before the `PROGRAM_RUNNING_WORDS` refusal: when the rule is a prefix
     rule, `bash_words == ("python3", <path>)` (exactly two words), accept it only if
     `_reviewed_skill_script(<path>)` holds. Any other `python3 … *` rule stays refused, with a
     message that names the allowed form.
   - `_reviewed_skill_script(path)`: the text fullmatches
     `docs/skills/[a-z0-9][a-z0-9-]*/scripts/[A-Za-z0-9_][A-Za-z0-9_-]*\.py` (relative, no `.`/`..`
     segment, no glob, no `./` prefix); `Path.cwd() / path` is a regular file and not a symlink,
     and its parent directories are not symlinks (resolved path equals the lexical path).
     `Path.cwd()` is the base because the bash process inherits that directory.
   - The matcher is unchanged: `_words_match` compares words exactly, so `./docs/…`,
     `/app/docs/…`, `docs/…/../…`, `python3 -I docs/…` and `python docs/…` do not match.
     A separator, `$(…)`, a glob, an assignment and `> file` already fail D8.2's closed
     condition.
   - Verify: `make test-file FILE=tests/personal_agent/governance/test_permission_rules.py`.

2. **Rules** — `config/governance/permissions.yaml`: one allow rule per script, each with a
   reason, `modes: [NORMAL, ALERT, DEGRADED]`. Header comment: the form and its condition.

3. **Close the `cypher.py` hole** — `docs/skills/neo4j-direct/scripts/cypher.py`.
   - Add `find_blocked_function(query)`: read the dotted name chains (word and quoted-name tokens
     joined by `.`, a chain not preceded by `.`), unquote, join, lower-case. Return the first
     whose name starts with a blocked namespace: `apoc.cypher.` (runs Cypher text, so a blocked
     procedure or `LOAD CSV` hides in a string) and `apoc.xml.` (parses XML text on the server).
   - `run_query` calls it after the procedure check and before the write check. Error text names
     the function and says why.
   - Update the module docstring.

4. **Defence in depth for the file the rule trusts** — `config/governance/tools.yaml`:
   add `/app/docs/skills/**` and `/app/.venv/**` to `write_forbidden_paths` (an alias, so `edit`
   gets them too). A forbidden path is refused before the durability check, and does not depend
   on `mountinfo`.

5. **Skill bodies** — remove the 10 `# ask: python3 can run any program…` lines from
   `neo4j-direct`, `infrastructure-health`, `query-elasticsearch` and `turn-forensics` SKILL.md.
   Regenerate `docs/reference/PROMPT_CORPUS.md` (`make render-prompt-corpus`).

6. **ADR-0156** — a dated Status Update "2026-10-11 - Amendment to D8.2: reviewed skill scripts
   (FRE-1585)" with the owner's decision, the rule form, the conditions, and the accepted residual
   risks. A one-line pointer at the end of D8.2's indirect-execution sentence. Governance
   `AGENTS.md`: one line on the form.

## Tests (each criterion gets an outcome test)

- **AC-1** — `tests/personal_agent/governance/test_skill_commands_permissions.py`: every recipe
  that holds `python3 docs/skills/` evaluates to `allow` in NORMAL with the real rules; at least
  10 such recipes; each of the 4 scripts appears in one.
- **AC-2** — `tests/personal_agent/governance/test_permission_rules.py` (or a new
  `test_skill_script_rules.py`):
  - ask or deny under the real rules: `python3 -c 'print(1)'`, `python3 /tmp/x.py`,
    `python3 docs/skills/x/scripts/../../../evil.py`,
    `python3 docs/skills/neo4j-direct/scripts/../../infrastructure-health/scripts/probe_services.py all`,
    `python3 docs/skills/*/scripts/cypher.py x`, `python3 docs/skills/neo4j-direct/cypher.py x`
    (outside `scripts/`), `python3 docs/skills/neo4j-direct/scripts/other.py`,
    `python3 ./docs/…`, `python3 /app/docs/…`, `python3 -I docs/…`, `python docs/…`,
    and a listed script followed by `; rm x`, `&& id`, `| sh`, `$(id)`, `` `id` ``, `> /tmp/out`,
    `>> f`, a `PATH=/tmp` assignment.
  - the loader refuses `bash(python3 <p> *)` for: `/tmp/x.py`, `docs/skills/x/y.py`,
    `docs/skills/x/scripts/../../../evil.py`, `docs/skills/*/scripts/a.py`,
    `./docs/skills/...`, `/app/docs/skills/...`, a missing file, a symlink (tmp cwd),
    a script plus an extra word (`python3 <p> all *`), and `bash(python *)`.
  - the loader accepts the real file, and an exact `bash(python3 …)` stays as D8.2 had it.
- **AC-3** — `tests/docs/test_skill_scripts.py`: run each script in a child `python` with a
  `sys.addaudithook` harness (`runpy.run_path`, then the script's `__main__`), with hostile
  arguments: paths (`/etc/passwd`, `../../x`), options (`-c`, `--output=/tmp/x`, `--param`
  forms), shell text (`$(id)`, `;rm`), and for `cypher.py` write clauses, blocked procedures,
  `apoc.cypher.*`/`apoc.xml.*` calls in every quoting form. Assert from the audit log:
  - no `open` with a write mode and no `os.remove`/`os.rename`/`shutil.*` events;
  - no spawn (`subprocess.Popen`, `os.exec`, `os.posix_spawn`, `os.system`, `os.fork`), except
    `probe_services.py postgres|all`, whose single spawn argv is exactly the fixed `psql … SELECT 1`;
  - no `open` for reading outside the interpreter's prefixes and the script's directory;
  - `socket.connect` only to the hosts that the environment names (env is not an argument).
  - `cypher.py`: a blocked query exits 1 with no `socket.connect`; the existing test keeps
    `READ_ACCESS` + `execute_read`.
  - A test that the `python3 docs/skills/…` rules in `permissions.yaml` name exactly the scripts
    that this AC-3 test covers, so a new rule cannot land without its argument test.
- **write guard** — a test that `write` and `edit` refuse `/app/docs/skills/x/scripts/y.py` and
  `/app/.venv/lib/x.py` through `forbidden_paths`, with the durability check patched to pass.
- **AC-4** — live, after deploy (owner turn in the PWA). Not testable here.

## Gates

`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
Self-review: `feature-dev:code-reviewer` and `security-review`. Diff class: escalated (governance,
widens the allow list).

## Accepted residual risks (to record in the amendment)

- The allowed file is trusted as code. It is in the image, not mounted, and refused to `write`
  and `edit`. A bash write to a file asks. A change reaches it only through a reviewed PR and a
  rebuild.
- `probe_services.py` spawns `psql` with a fixed argv, and connects to the hosts in the
  environment. No argument changes either.
- `cypher.py` sends model-written Cypher to Neo4j in a read transaction. It can read any node of
  the graph. That is its purpose.
- Piped stdin: a command before the script in a pipeline is judged by its own rule. The scripts
  only filter and print their input.

## Codex plan review (2026-10-11) and disposition

| Finding | Severity | Checked | Disposition |
|---|---|---|---|
| `READ_ACCESS` is only a routing hint, so `cypher.py` is not read-only; `INSERT` is missing | High | Test Neo4j, read access mode: `CREATE` and `INSERT` refused ("Writing in read access mode not allowed") | Data writes: the claim is wrong, the server enforces it. `INSERT` added to the early check anyway |
| `SHOW SETTINGS`, `TERMINATE TRANSACTIONS` pass both checks | High | Test Neo4j: `SHOW SETTINGS` returned 171 rows, `TERMINATE TRANSACTIONS` ran | Fixed: the script refuses administration commands (`SHOW`, `TERMINATE`, `USE`, `ALTER`, …) |
| The audit harness can pass without running the script | Medium | The harness already used `run_name="__main__"` | Fixed: each hostile-argument test asserts exit code 1 or 2; seeded positives for the spawn and the connect; a control test proves the hook sees a write, a read, a spawn and a connect |
| The hook cannot see what `psql` does | Medium | True | Recorded as a residual risk: `psql` argv is fixed, its target comes from the environment |
| Time-of-check/time-of-use between the loader and bash | Low | True | Recorded as a residual risk in the ADR amendment |

## Self-review and rebase (2026-10-11)

- Code review: a range bound (`[0..apoc.cypher.x()]`) passed the function check, and the admin
  words refused `start`/`stop` as names. Fixed. Admin commands are refused at the statement start
  only, where the server accepts them (probed: `CYPHER 5 …`, `CYPHER runtime=slotted …`,
  `EXPLAIN …` ran; `CALL { SHOW … }`, `UNION SHOW …`, `WITH … SHOW …` were refused by the server).
- Parser probes on the test Neo4j: `apoc.\u0063ypher.runFirstColumnSingle` ran. The script now
  refuses a backslash outside a string. Line comments end at every line terminator.
- Rebased on `ef6aac78` (FRE-1579). `validate_mermaid.py` gets a rule (NORMAL only, as
  `bash(mmdc *)`), reads only a `.mmd` file that is not a link, and passes the absolute path.
