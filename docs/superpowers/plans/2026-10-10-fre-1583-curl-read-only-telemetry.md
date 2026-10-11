# FRE-1583 — curl runs without asking only for a read of internal telemetry

**Ticket:** FRE-1583 (Approved, Urgent). **Amends:** ADR-0156 D8.2. **Tier:** Standard/Complex
(security, `src/` logic) — Codex plan review and a security review are required.

## Owner decisions

- 2026-10-10 (ticket): "Read-only internal (Recommended)". `curl` runs without asking only for a
  read of Elasticsearch, Tempo, and the health endpoints of our services. Everything else asks.
- 2026-10-10 (this build seat, AskUserQuestion):
  - "Allow _query (Recommended)": `POST /_query` (ES|QL) with a literal body runs without asking.
    ES|QL has no write command.
  - "GET / is health (Recommended)": a body-less `GET http://neo4j:7474/` is a health endpoint.
    Every other Neo4j path and method asks.

## Design

### Where the rule lives

1. `config/governance/permissions.yaml` loses the allow rule `bash(curl *)`.
2. The file gets a new top-level list, `curl_read`. Each entry names `hosts`, `methods`, `paths`,
   optional `body` (default `false`), optional `modes` and optional `reason`.
3. `_evaluate_bash` (`governance/permissions.py`) allows a top-level subcommand whose first word is
   `curl` and that matches no allow rule only when its arguments reduce to one request that a
   `curl_read` entry lists. Otherwise it asks with the reason the parser gives.
4. FRE-1572's `_curl_hazard` in `option_hazard` stays. It is a second, independent check.

AC-4's seeded negative is then literal: add `bash(curl *)` back to the allow list, and the four
rows of the ticket come out allow again.

### The request parser — new module `governance/curl_request.py`

`parse_curl_request(args) -> CurlRequest | CurlRefusal`. `CurlRequest` holds `authority`
(`host:port`), `method`, `path`, `body` (bool). The parser is an allowlist. An option that is not
in the table below asks.

| Option | Argument | Condition |
|--------|----------|-----------|
| `-s` `--silent` `-S` `--show-error` `-f` `--fail` `--fail-with-body` `-i` `--include` `--compressed` | none | — |
| `-G` `--get` | none | the data goes into the query string; the method is `GET` |
| `-m` `--max-time` `--connect-timeout` | number | `^\d+(\.\d+)?$` |
| `-X` `--request` | method | exactly `GET` or `POST`; given once only |
| `-H` `--header` | header | `Content-Type` or `Accept` (any case), value `application/json` |
| `-d` `--data` `--data-raw` `--data-binary` | body | does not start with `@` |
| `--data-urlencode` | body | no `@` before the first `=` |
| `-o` `--output` | file | `/dev/null` or `/dev/stdout` only; given once only |
| `-w` `--write-out` | format | only literal text, `%%`, `%{name}` and `\n` `\r` `\t`. No `@`, no `%output{…}`, no `%header{…}` |

- A short-option cluster (`-fsS`, `-sm5`) is read letter by letter. A letter that takes an
  argument takes the rest of the cluster, or the next word.
- `--name=value` asks (curl does not support it). `--` and a bare `-` ask.
- Exactly one URL (a word that does not start with `-`). Zero or two URLs ask.
- The URL must match `http://<authority><path>[?<query>]` exactly:
  - scheme `http://`, lower case. `https`, upper case and a missing scheme ask;
  - authority is `host:port` with an explicit port. It is matched exactly against the entry's
    `hosts`. A userinfo `@`, an IP literal, a trailing dot and a wrong port ask;
  - path characters are `A-Z a-z 0-9 . _ - * , /` only. No `%` (Elasticsearch decodes it, so
    `_bul%6b` is `_bulk`), no `//`, no `.` or `..` segment (curl squashes dot segments);
  - no `#`, `\`, space, control character, `{`, `}`, `[`, `]` anywhere in the URL (curl expands
    `{a,b}` and `[1-3]` into many URLs);
  - an empty path is `/`. One trailing `/` is removed before matching, except on `/`.
- Method: `-X` when given. Else `GET` with `-G`. Else `POST` with data. Else `GET`.
- `body` is true when data is present and `-G` is absent.

### Path patterns (`curl_read` entries)

A pattern is split on `/`. A literal segment matches itself. `*` matches one segment that does not
start with `_` (an index or a trace id, never an API word). There is no open prefix pattern, so a
route that a server adds later asks.

### The table (`permissions.yaml`)

All entries: `modes: [NORMAL, ALERT, DEGRADED]` (the modes of the old `bash(curl *)` rule).

| Hosts | Methods | Body | Paths |
|-------|---------|------|-------|
| `elasticsearch:9200`, `localhost:9200` | GET | no | `/`, `/_cat`, `/_cat/*`, `/_cat/*/*`, `/_cluster/health`, `/_cluster/health/*`, `/_mapping`, `/*/_mapping`, `/_field_caps`, `/*/_field_caps` |
| `elasticsearch:9200`, `localhost:9200` | GET, POST | yes | `/_search`, `/*/_search`, `/_count`, `/*/_count`, `/_query` |
| `tempo:3200` | GET | no | `/ready`, `/status`, the seven documented `/status/<page>` routes, `/api/echo`, `/api/status/buildinfo`, `/api/traces/*`, `/api/v2/traces/*`, `/api/search`, `/api/search/tags`, `/api/search/tag/*/values`, the two `/api/v2/search/…` forms, `/api/metrics/query`, `/api/metrics/query_range` |
| `neo4j:7474` | GET | no | `/` |
| `searxng:8080` | GET | no | `/healthz` |
| `grafana:3000` | GET | no | `/api/health` |
| `localhost:9001` | GET | no | `/health` |

Tempo is a path list, not "GET on any path": the Tempo API documents `GET` on state-changing
endpoints (`GET,POST,DELETE /live-store/prepare-downscale`, `GET,POST,PATCH,DELETE
/api/overrides`), and `/api/mcp` is an MCP server.

### Loader validation (`parse_rules_document`)

`curl_read` is a list. Each entry: `hosts` a non-empty list of `host:port` strings in lower case,
`methods` a non-empty subset of `{GET, POST}`, `paths` a non-empty list of patterns that start with
`/`, `**` last only, `body` a bool, `modes` as for rules. Any other key is an error. An error makes
`decide_permission` deny (fail closed, D8.1).

## Steps

1. Tests first — `tests/personal_agent/governance/test_curl_read.py`:
   - AC-1: the four ticket rows, an outside host, `-X PUT`/`DELETE`/`PATCH`, Neo4j `tx/commit`,
     `_bulk`, `_doc`, `_update_by_query`, `_delete_by_query`, `_settings`, `--url …`, `-K file` ask.
   - AC-2: `_cat/indices` on `localhost:9200`, `_search` with a JSON body (POST and GET), `_count`,
     `_query`, Tempo `/api/search?…` and `/api/traces/<id>`, the Neo4j and other health GETs allow.
   - AC-3: two URLs, userinfo, IP literal, `-L`, `--resolve`, `--connect-to`, `-x`, `--proxy`,
     `--max-time=5`, `--`, no URL, `{}`/`[]` globbing, `/../_bulk`, `_bul%6b`, `HTTP://`,
     `https://`, no port, wrong port, trailing-dot host, `-H 'Host: x'`, `-w '%output{/x}'`,
     `-w @f`, `-o /etc/x`, `-X post`, `-X GET -X DELETE`, `--unix-socket`, `-u a:b`, Tempo
     `/flush`, `/shutdown`, `/api/overrides`, body on a GET-only endpoint, a body on `_cat`.
   - AC-4: the production rules plus `bash(curl *)` allow the four rows.
   - Loader: each bad `curl_read` entry raises `PermissionRuleError`.
   Run `make test-file FILE=tests/personal_agent/governance/test_curl_read.py` → fails.
2. `governance/curl_request.py`: the parser and `CurlReadRule` with `allows(request)`.
3. `governance/permissions.py`: `PermissionRules.curl_read`, loader, `merged`, `_evaluate_bash`.
4. `permissions.yaml`: remove `bash(curl *)`, add the table. Step-1 tests pass.
5. Skills (`docs/skills/`): keep recipes in the allowed form, or add `# ask:`:
   `query-tempo.md` (`{traceID}` → `<trace_id>`: curl globs braces), `fetch-url.md` (outside
   hosts ask), `neo4j-direct.md` (tx/commit asks), `bash.md`, `infrastructure-health.md`,
   `query-elasticsearch.md`, `self-telemetry.md` (approval lines). Add a test in
   `test_skill_commands_permissions.py`: every `curl` recipe of `query-elasticsearch`,
   `query-tempo`, `self-telemetry` and `infrastructure-health` comes out allow (AC-2).
6. AC-5: update `test_eval_permissions.py` — add the class "curl is not a listed telemetry read"
   and the new counts. List each changed outcome in the PR.
7. Fix other tests that expected an allowed curl (`test_bash_matcher.py`,
   `test_permissions_config.py`, `test_bash_auto_approve_wire.py`, `test_bash_allowlist_bypass.py`,
   `test_bash_shell_contract.py`, `test_latency_skill_paths.py`) — each change keeps the test's
   intent, with an internal URL where the test is about another feature.
8. ADR-0156: a dated amendment to D8.2, with both owner decisions.
9. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`. Self-review: `feature-dev:code-reviewer` + `security-review`.

## Codex plan review — dispositions

| # | Finding | Disposition |
|---|---------|-------------|
| 1 | `~/.curlrc` can add options unless `-q` is first | Not adopted. Requiring `-q` makes AC-2's named forms ask. Residual risk, stated in the ADR amendment. |
| 2 | `http_proxy` and friends redirect curl | Not adopted. No compose file sets one. The model cannot set one without an approved call. Stated in the ADR amendment. |
| 3 | An ordinary `bash(curl *)` allow rule bypasses the parser | Adopted. The loader refuses a `bash(curl … *)` prefix allow rule. AC-4 builds the old rule in memory. |
| 4 | `-o /tmp/…` writes a file | Adopted. `-o` accepts only `/dev/null` and `/dev/stdout`. |
| 5 | `_search?scroll` keeps a search context | Not adopted. The context expires and changes no data. Stated in the ADR amendment. |
| 6 | `**` patterns approve future routes | Adopted. `**` is removed. Tempo and `_cat` routes are listed one by one. |
| 7 | More parser equivalence tests | Adopted for the argument cases. A capture-server test against real curl is not added. |

## Residual risks (stated, not fixed here)

- curl reads `~/.curlrc` unless `-q` is first. In the gateway container the process runs as root
  (`HOME=/root`), and `/root` is outside the `write` tool's `allowed_paths`. The worker runs as
  `nobody`.
- A proxy environment variable redirects curl. No compose file sets one.
- `_search?scroll=…` keeps a search context open until it expires.
- A `_search` body can hold a Painless script. A search script cannot write.
