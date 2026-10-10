# FRE-1572 — The bash auto-approve allowlist cannot be bypassed

**Ticket:** FRE-1572 (High, Tier-3 bugfix approved by master). **Backing:** ADR-0063 Amendment A
(the approval gate), FRE-283 (`auto_approve_prefixes`), FRE-1535.

## Owner decisions (2026-10-10, this session)

1. `python3` stays auto-approved. It is recorded as accepted: the skills depend on it.
2. Allowlisted binaries' options that run a command or write a file need approval.
3. `curl`'s file-read, upload and write options need approval. `psql -c` and `redis-cli` leave
   the NORMAL allowlist.

## The defect

`_split_command_segments` splits on top-level `;`, `|`, `||`, `&&` only. A newline, a single `&`,
`$(…)` and backticks hide a second command behind an allowlisted first word, and
`_check_segment_allowlist` reads only each segment's first word.

## Design: refuse auto-approval, do not parse recursively

A segment that holds a construct the checker cannot see into is **not auto-approvable**. It goes to
the normal approval flow (a card in the PWA, a denial in a CLI turn). It is not blocked. A
recursive parser of bash would be a second, partial bash implementation, and each gap in it is a
bypass. A refusal costs one card, and these forms are rare in normal use.

### Step 1 — A scanner that records why a segment is unsafe

`src/personal_agent/tools/primitives/bash.py`: a private `_scan_segments(command) ->
list[_Segment]` (`_Segment(text, unsafe: str | None)`). `_split_command_segments` keeps its name
and returns the texts, so its callers and tests do not change. Outside quotes the scanner:

- splits on `;`, `|`, `||`, `&&`, `|&`, **a newline** and **a single `&`**. A `&` in a redirection
  (`2>&1`, `>&2`, `<&0`, `&>`, `&>>`) is not a separator.
- skips a comment (`#` at the start of a word) to the end of the line. Without this, `ls #'`
  newline `chmod …` newline `#'` hides the middle line inside a quote the shell never opens.
- reads a here-doc body (`<<WORD`, `<<-WORD`, quoted or not) as data, to its delimiter line. An
  unquoted delimiter whose body holds `$(` or a backtick marks the segment unsafe.

The segment is unsafe when, outside single quotes (bash expands these in double quotes too), it
holds `$(` (which includes `$((`) or a backtick; and when, outside any quotes, it holds `<(`, `>(`,
`$'` (ANSI-C quoting, which the quote tracking cannot follow), a `${…@…}` transformation, `<>`, or
an output redirection (`>`, `>>`, `>|`, `&>`, `&>>`, `N>`) whose target is not a scratch path.

**Scratch path:** `/dev/null`, `/dev/stdout`, `/dev/stderr`, or a normalized path under `/tmp/`
(no `..`). This matches the `write` policy, whose `unattended_paths` include `/tmp/**`. A fd
duplication (`2>&1`, `>&2`) is not a file.

### Step 2 — Option rules for allowlisted binaries

`_option_hazard(words) -> str | None`, run on each segment after the prefix match:

| Binary | Needs approval when |
|---|---|
| `env` | any word that is not `NAME=VALUE` or `-i`/`-0`/`--null`/`--ignore-environment` (a command, `-S`, `-u`, `-C`) |
| `find` | `-exec`, `-execdir`, `-ok`, `-okdir`, `-delete`, `-fprint`, `-fprint0`, `-fprintf`, `-fls` |
| `awk` | the program holds `system`, `getline` or `\|`, or `print`/`printf` followed by `>`; or `-f`, `-i`, `-l`, `-E` and their long forms |
| `sed` | `-i`/`--in-place`, `-f`/`--file`, an `e`/`w`/`W` command, or an `s` command with an `e` or `w` flag |
| `sort` | `-o`/`--output` (also in a short-option cluster) or `--compress-program` (long-option abbreviations too) |
| `uniq` | a second file operand (the output file) |
| `rg` | `--pre` |
| `git` | `--output`, `--ext-diff` |
| `curl` | a value starting `@` for `-d`/`--data*`/`--json`/`-H`/`-F` (also `=@`, `=<` in `-F`, `@` in `--data-urlencode`); `-T`/`--upload-file`; `-K`/`--config`; `-O`/`--remote-name*`/`-J`/`--output-dir`; `-o`/`-D`/`-c`/`--trace*`/`--libcurl`/`--stderr` to a non-scratch path; a `file:` URL |
| `mmdc` | `-o` to a non-scratch path; `-p`/`--puppeteerConfigFile` (it can name an executable) |

### Step 3 — Governance

`config/governance/tools.yaml`: remove `psql -c` and `redis-cli` from `auto_approve_prefixes.NORMAL`,
with a comment naming FRE-1572. ALERT and DEGRADED do not list them.

### Step 4 — Tests

`tests/test_tools/primitives/test_bash_allowlist_bypass.py`:

- **AC-1:** one test per form, each asserting a non-`None` result with the real NORMAL allowlist
  from `tools.yaml`: newline, single `&`, `$(…)`, backticks, `$(` inside double quotes, `<(…)`,
  `>(…)`, the comment-quote hide, an unquoted here-doc with `$(`, `$'…'`, `${x@P}`, `> file`,
  `>> file`, `<>`, and every option rule above (one row per rule).
- **AC-2:** normal commands still return `None`: `ls -la | grep x`, `cat f && grep y f`,
  `grep x f 2>&1`, `cmd > /dev/null`, `cmd > /tmp/out.txt`, `echo "$HOME"`, `echo '$(x)'`,
  `curl -s -o /dev/stdout URL`, `curl -X POST -d '{"a":1}' URL`, `sed -n '1,10p' f`,
  `sed 's/a/b/g' f`, `awk '$3 > 5 {print $1}' f`, `find . -name '*.py'`, `sort -rn f`, `uniq -c f`,
  `python3 -c "print(1)"`, `python3 - <<'EOF'` with a body, `mmdc -i /tmp/m.mmd -o /tmp/m.svg`,
  `env`, and the skill-doc commands the tests can read.
- **AC-4 (seeded negative):** each AC-1 form run against the old splitter (a verbatim copy of the
  origin/main function in the test file, applied with `monkeypatch`) returns `None`, so the tests
  fail on the old code.
- The permission layer: `_check_permissions` with a bypass command does not auto-approve; it reaches
  the approval flow (`approval_no_transport` with no transport).
- The governance change: `psql -c` and `redis-cli` are not in the NORMAL list.

### Step 5 — Gates

`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files` ·
the code reviewer and `security-review` on `git diff origin/main...HEAD`. Diff class: escalated
(the approval gate).

## Review of scope item 3 (AC-3, also in the PR)

| Construct | Verdict |
|---|---|
| `eval`, `exec`, `source`, `.`, `sh -c`, `bash -c`, `xargs`, `nohup`, `timeout`, `nice`, `command`, `builtin`, `time`, `coproc` | Caught: not an allowlisted first word. Tests pin each. |
| `env CMD` | Caught (option rule). `env` alone stays auto-approved. |
| `find -exec` family, `-delete`, `-fprint*` | Caught. |
| here-docs | Body read as data. Unquoted delimiter with `$(` or a backtick in the body: caught. |
| `git -c` | Caught: `git -c x log` does not match the `git log` prefix. `--output` and `--ext-diff` caught. |
| `awk system()` / `getline` / pipes / `print >` | Caught. |
| `sed` `e`/`w`/`-i` | Caught. |
| `sort --compress-program` / `-o`, `uniq` output file, `rg --pre`, `mmdc -p` | Caught. |
| `curl` file read, upload, write, `file:` | Caught. Plain GET/POST with inline data: accepted (81 skill uses; it is the primary's route to Elasticsearch and the API). The data in such a request is model-written: the residual risk. |
| `psql -c`, `redis-cli` | Removed from the allowlist. |
| `python3` | **Accepted** (owner decision 1): any python3 call runs arbitrary code; the skills depend on it. |
| `cat`, `grep`, `head`, `tail`, `jq`, `rg` reads | Accepted: reads are not gated by approval (FRE-487 is the read-discipline ticket). |
| `git status` running a repository-configured command (`core.fsmonitor`) | Accepted: it needs a prior write to `.git/config`. |
| `date -s` | Accepted: the container has no `CAP_SYS_TIME`. |
| `tail -f`, `top` | Accepted: bounded by the bash timeout. |
| Subshells, groups, functions, `if`/`for`, `!`, `X=1 cmd` | Caught: the first word (`(cmd`, `{`, `if`, `!`, `X=1`) is not allowlisted. Tests pin each. |
