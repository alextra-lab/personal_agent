"""ADR-0156 D5.4 (FRE-1578 AC-2): each skill script has a test that runs it.

The contract that D5.4 asks for ("each with --help and plain error text"), stated so that it can
be asserted:

- ``--help`` exits 0 and prints a usage line;
- success exits 0, a failed check or query exits 1, bad usage exits 2;
- every failure is one ``error:`` line on stderr. No traceback, no secret.

The scripts are standalone files under ``docs/skills/<name>/scripts/``. A stub server or a fake
driver stands in for the service. No test touches a real substrate (tests/CLAUDE.md, FRE-375).
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import ModuleType

import pytest

_SKILLS = Path(__file__).resolve().parents[2] / "docs" / "skills"
_CLASSIFIER = _SKILLS / "query-elasticsearch" / "scripts" / "es_index_granularity.py"
_PROBE = _SKILLS / "infrastructure-health" / "scripts" / "probe_services.py"
_CYPHER = _SKILLS / "neo4j-direct" / "scripts" / "cypher.py"
_TIMELINE = _SKILLS / "turn-forensics" / "scripts" / "call_timeline.py"
# FRE-1579 (T7). Its verdict goes to stdout (VALID, INVALID, ERROR), so its own tests are in
# test_validate_mermaid_script.py. Here it is held to the shared --help and usage-error contract.
_VALIDATE = _SKILLS / "mermaid-diagrams" / "scripts" / "validate_mermaid.py"


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


def _run(
    script: Path, args: list[str], stdin: bytes | str = b"", env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``script`` as a command, with a clean environment so no AGENT_* value leaks in."""
    data = stdin.encode() if isinstance(stdin, str) else stdin
    clean = {"PATH": os.environ.get("PATH", ""), **(env or {})}
    done = subprocess.run(
        [sys.executable, str(script), *args],
        input=data,
        capture_output=True,
        timeout=30,
        env=clean,
    )
    return subprocess.CompletedProcess(
        done.args, done.returncode, done.stdout.decode(), done.stderr.decode()
    )


def _plain_error(done: subprocess.CompletedProcess[str], code: int) -> str:
    """Assert the failure contract and return the error line."""
    assert done.returncode == code, (done.returncode, done.stderr)
    lines = [ln for ln in done.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1, done.stderr
    assert lines[0].startswith("error: ")
    assert "Traceback" not in done.stderr
    return lines[0]


# --- every script: --help and one error line ------------------------------------------------

_ALL = (_CLASSIFIER, _PROBE, _CYPHER, _TIMELINE, _VALIDATE)


@pytest.mark.parametrize("script", _ALL, ids=[p.stem for p in _ALL])
@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_exits_0_and_prints_usage(script: Path, flag: str) -> None:
    done = _run(script, [flag])
    assert done.returncode == 0
    assert "usage:" in done.stdout
    assert done.stderr == ""


@pytest.mark.parametrize("script", _ALL, ids=[p.stem for p in _ALL])
def test_a_second_unknown_option_is_bad_usage(script: Path) -> None:
    _plain_error(_run(script, ["--no-such-option", "x", "y"]), 2)


def test_every_script_file_has_a_test_here() -> None:
    """D5.4: a script that is not named in this file has no test."""
    named = {p.name for p in _ALL}
    on_disk = {p.name for p in _SKILLS.glob("*/scripts/*.py")}
    assert on_disk == named


# --- es_index_granularity.py ----------------------------------------------------------------


def test_classifier_reports_a_mixed_family() -> None:
    done = _run(_CLASSIFIER, ["agent-logs"], "agent-logs-2026.07.31\nagent-logs-2026-07\n")
    assert done.returncode == 0
    assert "MIXED" in done.stdout


def test_classifier_exits_1_on_empty_input() -> None:
    """The case where curl failed upstream: empty input must not read as "no indices"."""
    _plain_error(_run(_CLASSIFIER, ["agent-logs"], ""), 1)


def test_classifier_exits_1_when_no_name_has_a_known_shape() -> None:
    done = _run(_CLASSIFIER, ["agent-logs"], "agent-logs-2026\nother-2026-07\n")
    assert "unrecognized (2)" in done.stdout
    _plain_error(done, 1)


def test_classifier_exits_1_on_input_that_is_not_text() -> None:
    _plain_error(_run(_CLASSIFIER, ["agent-logs"], b"\xff\xfe\x00"), 1)


def test_classifier_exits_2_without_a_family() -> None:
    _plain_error(_run(_CLASSIFIER, [], ""), 2)


# --- probe_services.py ----------------------------------------------------------------------


class _Health(BaseHTTPRequestHandler):
    body = b'{"status": "green"}'
    code = 200

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self.send_response(self.code)
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_: object) -> None:
        pass


@contextmanager
def _http(code: int = 200, body: bytes = b'{"status": "green"}') -> Iterator[str]:
    handler = type("H", (_Health,), {"code": code, "body": body})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def _tcp(reply: bytes) -> Iterator[int]:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                conn.recv(64)
                conn.sendall(reply)

    threading.Thread(target=serve, daemon=True).start()
    try:
        yield server.getsockname()[1]
    finally:
        server.close()


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_probe_elasticsearch_green_and_yellow_pass() -> None:
    for status in ("green", "yellow"):
        with _http(body=json.dumps({"status": status}).encode()) as url:
            done = _run(_PROBE, ["elasticsearch"], env={"AGENT_ELASTICSEARCH_URL": url})
        assert done.returncode == 0, done.stdout
        assert done.stdout.startswith("elasticsearch: ok")


def test_probe_elasticsearch_red_fails() -> None:
    with _http(body=b'{"status": "red"}') as url:
        done = _run(_PROBE, ["elasticsearch"], env={"AGENT_ELASTICSEARCH_URL": url})
    assert done.returncode == 1
    assert "FAIL cluster status red" in done.stdout


def test_probe_elasticsearch_http_error_and_closed_port_fail() -> None:
    with _http(code=503) as url:
        done = _run(_PROBE, ["elasticsearch"], env={"AGENT_ELASTICSEARCH_URL": url})
    assert done.returncode == 1 and "HTTP 503" in done.stdout
    down = f"http://127.0.0.1:{_closed_port()}"
    done = _run(_PROBE, ["elasticsearch"], env={"AGENT_ELASTICSEARCH_URL": down})
    assert done.returncode == 1 and "FAIL cannot connect" in done.stdout


def test_probe_redis_pong_noauth_and_failures() -> None:
    cases = [(b"+PONG\r\n", 0, "ok"), (b"-NOAUTH Authentication required.\r\n", 0, "ok")]
    for reply, code, word in cases:
        with _tcp(reply) as port:
            env = {"AGENT_EVENT_BUS_REDIS_URL": f"redis://127.0.0.1:{port}/0"}
            done = _run(_PROBE, ["redis"], env=env)
        assert done.returncode == code and done.stdout.startswith(f"redis: {word}")
    with _tcp(b"HTTP/1.1 400 Bad Request\r\n") as port:
        done = _run(
            _PROBE, ["redis"], env={"AGENT_EVENT_BUS_REDIS_URL": f"redis://127.0.0.1:{port}"}
        )
    assert done.returncode == 1 and "unexpected reply" in done.stdout
    down = {"AGENT_EVENT_BUS_REDIS_URL": f"redis://127.0.0.1:{_closed_port()}"}
    assert _run(_PROBE, ["redis"], env=down).returncode == 1


def _fake_psql(tmp_path: Path, script: str) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    psql = bin_dir / "psql"
    psql.write_text(f"#!/bin/sh\n{script}\n")
    psql.chmod(psql.stat().st_mode | stat.S_IEXEC)
    return {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}


def test_probe_postgres_runs_select_1_through_psql(tmp_path: Path) -> None:
    env = {
        **_fake_psql(tmp_path, 'echo "$PGPASSWORD" > "$0.pw"; echo 1'),
        "AGENT_DATABASE_URL": "postgresql+asyncpg://u:s3cret@db:5432/app",
    }
    done = subprocess.run(
        [sys.executable, str(_PROBE), "postgres"], capture_output=True, text=True, env=env
    )
    assert done.returncode == 0 and "postgres: ok" in done.stdout
    # The password reaches psql through PGPASSWORD, never through the command line.
    assert (tmp_path / "bin" / "psql.pw").read_text().strip() == "s3cret"
    assert "s3cret" not in done.stdout + done.stderr


def test_probe_postgres_failure_prints_no_password(tmp_path: Path) -> None:
    env = {
        **_fake_psql(
            tmp_path, 'echo "FATAL: password authentication failed for s3cret" >&2; exit 2'
        ),
        "AGENT_DATABASE_URL": "postgresql+asyncpg://u:s3cret@db:5432/app",
    }
    done = subprocess.run(
        [sys.executable, str(_PROBE), "postgres"], capture_output=True, text=True, env=env
    )
    assert done.returncode == 1
    assert "FAIL" in done.stdout and "s3cret" not in done.stdout + done.stderr


def test_probe_postgres_without_a_url_or_without_psql(tmp_path: Path) -> None:
    done = _run(_PROBE, ["postgres"])
    assert done.returncode == 1 and "AGENT_DATABASE_URL is not set" in done.stdout
    empty = tmp_path / "empty"
    empty.mkdir()
    done = _run(
        _PROBE,
        ["postgres"],
        env={"PATH": str(empty), "AGENT_DATABASE_URL": "postgresql://u:p@db/app"},
    )
    assert done.returncode == 1 and "psql is not installed" in done.stdout


def test_probe_all_reports_one_line_for_each_service_and_fails_if_one_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load(_PROBE)
    probes = {
        "postgres": lambda env: (True, "up"),
        "elasticsearch": lambda env: (True, "up"),
        "neo4j": lambda env: (False, "down"),
        "redis": lambda env: (True, "up"),
    }
    monkeypatch.setattr(module, "_PROBES", probes)
    lines: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: lines.append(" ".join(map(str, a))))
    assert module.main(["all"], {}) == 1
    assert lines == ["postgres: ok up", "elasticsearch: ok up", "neo4j: FAIL down", "redis: ok up"]


def test_probe_neo4j_reads_the_http_page_of_the_bolt_host(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load(_PROBE)
    seen: list[str] = []
    monkeypatch.setattr(module, "_http_get", lambda url: (seen.append(url), (200, b""))[1])
    ok, _ = module.probe_neo4j({"AGENT_NEO4J_URI": "bolt://graph.internal:7687"})
    assert ok and seen == ["http://graph.internal:7474/"]


def test_probe_with_an_unknown_service_is_bad_usage() -> None:
    _plain_error(_run(_PROBE, ["mysql"]), 2)


# --- cypher.py ------------------------------------------------------------------------------

_WRITES = [
    "CREATE (n:Entity {name: 'x'})",
    "MERGE (n:Entity {name: 'x'})",
    "MATCH (n:Entity) SET n.seen = true",
    "MATCH (n) DETACH DELETE n",
    "MATCH (n) DELETE n",
    "MATCH (n) REMOVE n.name",
    "DROP INDEX entity_name",
    "MATCH (n) FOREACH (x IN [1] | SET n.v = x)",
    "LOAD CSV FROM 'file:///x.csv' AS row RETURN row",
    "match (n) set n.x = 1",
    "INSERT (n:Entity {name: 'x'})",
    # Administration commands (FRE-1585). SHOW SETTINGS and TERMINATE TRANSACTIONS ran in read
    # access mode on the test server.
    "SHOW SETTINGS YIELD name RETURN name",
    "show transactions",
    "TERMINATE TRANSACTIONS 'neo4j-transaction-1'",
    "USE system MATCH (n) RETURN n",
    "ALTER USER neo4j SET PASSWORD 'x'",
    "GRANT ROLE admin TO bob",
    "STOP DATABASE neo4j",
]


@pytest.mark.parametrize("query", _WRITES)
def test_cypher_refuses_a_write_clause_before_it_connects(query: str) -> None:
    done = _run(_CYPHER, [query], env={"AGENT_NEO4J_PASSWORD": "s3cret"})
    line = _plain_error(done, 1)
    assert "read-only" in line and "s3cret" not in done.stderr


_BLOCKED_PROCEDURES = [
    "CALL apoc.load.json('http://elasticsearch:9200/_cat/indices') YIELD value RETURN value",
    "CALL apoc.cypher.doIt('CREATE (n)', {}) YIELD value RETURN value",
    "CALL dbms.listConfig() YIELD name RETURN name",
    "call apoc.export.csv.all('x.csv', {})",
    "MATCH (n) CALL apoc.nodes.delete(n, 1) YIELD value RETURN value",
    # The quoted forms of a name are valid Cypher. Each one hid the name from a text search.
    "CALL `apoc.load.json`('http://elasticsearch:9200/_cat/indices') YIELD value RETURN value",
    "CALL `apoc`.`load`.`json`('http://elasticsearch:9200') YIELD value RETURN value",
    "CALL apoc . load . json('http://elasticsearch:9200') YIELD value RETURN value",
    "CALL /* x */ apoc.load.json('http://elasticsearch:9200') YIELD value RETURN value",
    "CALL `dbms`.`listConfig`() YIELD name RETURN name",
    # A CALL that cannot be read is blocked: the check fails closed.
    "CALL 'apoc.load.json'('http://x') YIELD value RETURN value",
    "CALL $name()",
]


@pytest.mark.parametrize("query", _BLOCKED_PROCEDURES)
def test_cypher_refuses_a_procedure_that_is_not_on_the_list(query: str) -> None:
    done = _run(_CYPHER, [query], env={"AGENT_NEO4J_PASSWORD": "s3cret"})
    line = _plain_error(done, 1)
    assert "is not allowed" in line and "db.labels" in line


@pytest.mark.parametrize(
    "query",
    [
        "CALL db.labels() YIELD label RETURN label",
        "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType",
        "CALL { MATCH (n) RETURN n LIMIT 1 } RETURN n",
        "MATCH (n) WHERE n.note = 'CALL apoc.load.json(x)' RETURN n",
    ],
)
def test_cypher_passes_a_listed_procedure_a_subquery_and_a_quoted_call(query: str) -> None:
    cypher = _load(_CYPHER)
    assert cypher.find_write_clause(query) is None
    assert cypher.find_blocked_procedure(query) is None


def test_a_quoted_name_of_a_listed_procedure_is_allowed() -> None:
    cypher = _load(_CYPHER)
    assert cypher.find_blocked_procedure("CALL `db`.`labels`() YIELD label RETURN label") is None
    assert cypher.find_blocked_procedure("CALL db . labels() YIELD label RETURN label") is None


def test_a_quoted_procedure_name_is_read_whole() -> None:
    """Seeded negative: the name that a reader of the text could not see."""
    cypher = _load(_CYPHER)
    query = "CALL `apoc`.`load`.`json`('http://x') YIELD value RETURN value"
    assert cypher.find_blocked_procedure(query) == "apoc.load.json"
    assert cypher.find_blocked_procedure("CALL 'x'()") == "<unreadable>"
    # Not a CALL of a procedure: a property after a dot, and a word inside a string.
    assert cypher.find_blocked_procedure("MATCH (n) RETURN n.call, 'CALL apoc.x()'") is None


def test_a_quote_inside_a_quoted_name_does_not_hide_a_write() -> None:
    """Seeded negative: the first form opened a string at the quote and hid the SET."""
    cypher = _load(_CYPHER)
    assert cypher.find_write_clause("MATCH (n:`a'b`) SET n.p = 1 RETURN 'c'") == "SET"
    assert cypher.find_blocked_procedure("MATCH (n:`a'b`) CALL apoc.x.y() RETURN 'c'") == "apoc.x.y"


def test_a_quoted_name_that_looks_like_a_clause_is_not_a_write() -> None:
    """The error text says to quote a property named like a clause, so this must work."""
    cypher = _load(_CYPHER)
    assert cypher.find_write_clause("MATCH (n) RETURN n.`set`, n.`create`") is None


def test_cypher_does_not_read_a_clause_word_inside_a_string_or_comment() -> None:
    cypher = _load(_CYPHER)
    assert cypher.find_write_clause("MATCH (n) WHERE n.note = 'please SET me' RETURN n") is None
    assert cypher.find_write_clause("MATCH (n) // CREATE later\nRETURN n") is None
    assert cypher.find_write_clause("MATCH (n) RETURN n.created_at") is None


def test_cypher_needs_a_password() -> None:
    line = _plain_error(_run(_CYPHER, ["MATCH (n) RETURN count(n)"]), 1)
    assert "AGENT_NEO4J_PASSWORD" in line


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--limit", "0", "RETURN 1"],
        ["--limit", "x", "RETURN 1"],
        ["--param", "bad", "RETURN 1"],
    ],
)
def test_cypher_bad_usage_exits_2(args: list[str]) -> None:
    _plain_error(_run(_CYPHER, args), 2)


class _Record:
    def __init__(self, data: dict[str, object]) -> None:
        self._data = data

    def data(self) -> dict[str, object]:
        return self._data


class _FakeTx:
    def __init__(self, rows: int) -> None:
        self.rows, self.seen = rows, []

    def run(self, query: str, params: dict[str, object]):
        self.seen.append((query, params))
        return iter([_Record({"n": i}) for i in range(self.rows)])


class _FakeSession:
    def __init__(self, owner: _FakeDriver) -> None:
        self.owner = owner

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute_read(self, work):  # no execute_write exists: a write path would fail loudly
        if self.owner.error is not None:
            raise self.owner.error
        return work(self.owner.tx)


class _FakeDriver:
    def __init__(self, rows: int = 2, error: Exception | None = None) -> None:
        self.tx, self.error, self.modes = _FakeTx(rows), error, []

    def session(self, **kwargs: object) -> _FakeSession:
        self.modes.append(kwargs.get("default_access_mode"))
        return _FakeSession(self)


def test_cypher_opens_a_read_session_and_runs_a_read_transaction() -> None:
    import neo4j

    cypher = _load(_CYPHER)
    driver = _FakeDriver(rows=2)
    out = cypher.run_query("MATCH (n) RETURN n", {"q": "a"}, 10, {}, driver=driver)
    assert driver.modes == [neo4j.READ_ACCESS]
    assert out == {"rows": [{"n": 0}, {"n": 1}], "row_count": 2, "truncated": False}
    assert driver.tx.seen == [("MATCH (n) RETURN n", {"q": "a"})]


def test_cypher_truncates_at_the_limit_and_says_so() -> None:
    cypher = _load(_CYPHER)
    out = cypher.run_query("MATCH (n) RETURN n", {}, 3, {}, driver=_FakeDriver(rows=50))
    assert out["row_count"] == 3 and out["truncated"] is True and len(out["rows"]) == 3


def test_cypher_turns_each_driver_failure_into_one_plain_line() -> None:
    from neo4j import exceptions as exc

    cypher = _load(_CYPHER)
    env = {"AGENT_NEO4J_PASSWORD": "s3cret", "AGENT_NEO4J_URI": "bolt://u:s3cret@graph:7687"}
    # A server error is built the way the driver builds it, with the text in .message. A plain
    # ClientError("...") has no server text, and would not test the masking.
    server = exc.Neo4jError._hydrate_neo4j(
        code="Neo.ClientError.Statement.SyntaxError", message="Invalid input for s3cret"
    )
    failures = [
        exc.AuthError("bad credentials s3cret"),
        exc.ServiceUnavailable("cannot reach bolt://u:s3cret@graph:7687"),
        server,
        exc.ConfigurationError("bad scheme"),
    ]
    for failure in failures:
        with pytest.raises(cypher.CypherError) as caught:
            cypher.run_query("MATCH (n) RETURN n", {}, 5, env, driver=_FakeDriver(error=failure))
        message = str(caught.value)
        assert "s3cret" not in message and "\n" not in message
        if failure is server:
            assert "Invalid input for ***" in message


def test_cypher_masks_a_password_that_the_server_echoes() -> None:
    """Seeded negative: without the masking, this message would hold the password."""
    from neo4j import exceptions as exc

    cypher = _load(_CYPHER)
    echo = exc.Neo4jError._hydrate_neo4j(code="Neo.ClientError.General.Unknown", message="x s3cret")
    with pytest.raises(cypher.CypherError, match=r"x \*\*\*"):
        cypher.run_query(
            "RETURN 1", {}, 5, {"AGENT_NEO4J_PASSWORD": "s3cret"}, driver=_FakeDriver(error=echo)
        )


@pytest.mark.parametrize(
    ("uri", "shown"),
    [
        ("bolt://u:pw@graph:7687", "bolt://graph:7687"),
        ("bolt://graph", "bolt://graph"),
        ("bolt://graph:notaport", "the URI"),
        ("", "the URI"),
    ],
)
def test_cypher_safe_uri_hides_user_info_and_survives_a_bad_port(uri: str, shown: str) -> None:
    assert _load(_CYPHER)._safe_uri(uri) == shown


# --- call_timeline.py -----------------------------------------------------------------------

_LOG = (
    # Real lines also hold budget_role= and cache_creation_input_tokens=, before the real keys.
    "\x1b[2m10:00:00.100Z\x1b[0m model_call_completed budget_role=captains_log "
    "cache_creation_input_tokens=3329 trace_id=abc123 input_tokens=1200 role=primary\n"
    "10:00:03.900Z tool_call_started trace_id=abc123 tool_name=read {'path': '/app/big.py'}\n"
    "10:00:04.100Z model_call_completed trace_id=abc123 input_tokens=34000 role=primary\n"
    "10:00:09.000Z model_call_completed trace_id=other99 input_tokens=5 role=primary\n"
    "10:00:12.000Z artifact_draft_sub_agent_complete trace_id=abc123\n"
    "10:00:13.000Z something_else trace_id=abc123\n"
)


def test_timeline_prints_calls_tools_and_gaps_for_one_trace() -> None:
    done = _run(_TIMELINE, ["abc123"], _LOG)
    assert done.returncode == 0
    assert done.stdout.splitlines() == [
        "10:00:00  +?s  CALL in=1200 role=primary",
        "10:00:03  +3s  TOOL read /app/big.py",
        "10:00:04  +1s  CALL in=34000 role=primary",
        "10:00:12  +8s  SUBAGENT done",
    ]


def test_timeline_exits_1_on_empty_input_and_on_a_missing_trace() -> None:
    line = _plain_error(_run(_TIMELINE, ["abc123"], ""), 1)
    assert "abc123" in line
    _plain_error(_run(_TIMELINE, ["nosuch12"], _LOG), 1)


def test_timeline_exits_1_on_input_that_is_not_text() -> None:
    _plain_error(_run(_TIMELINE, ["abc123"], b"\xff\xfe\x00"), 1)


def test_timeline_rejects_a_trace_id_with_a_shell_character() -> None:
    _plain_error(_run(_TIMELINE, ["abc; rm x"], _LOG), 2)


def test_timeline_marks_a_failed_sub_agent_and_wraps_at_midnight() -> None:
    log = (
        "23:59:50.000Z model_call_completed trace_id=abc123 input_tokens=10 role=primary\n"
        "00:00:05.000Z artifact_draft_sub_agent_complete trace_id=abc123 success=False\n"
        "00:00:06.000Z artifact_draft_sub_agent_complete trace_id=abc123 success=True\n"
    )
    done = _run(_TIMELINE, ["abc123"], log)
    assert done.stdout.splitlines() == [
        "23:59:50  +?s  CALL in=10 role=primary",
        "00:00:05  +15s  SUBAGENT failed",
        "00:00:06  +1s  SUBAGENT done",
    ]


# --- probe_services.py: a URL with user-info, and a value that is not a port ----------------


def test_probe_prints_no_user_info_of_the_elasticsearch_url() -> None:
    down = f"http://admin:hunter2@127.0.0.1:{_closed_port()}"
    done = _run(_PROBE, ["elasticsearch"], env={"AGENT_ELASTICSEARCH_URL": down})
    assert done.returncode == 1 and "FAIL cannot connect" in done.stdout
    assert "hunter2" not in done.stdout + done.stderr


def test_probe_reports_a_bad_port_as_one_line_and_does_not_stop_the_run() -> None:
    env = {"AGENT_EVENT_BUS_REDIS_URL": "redis://redis:notaport/0"}
    done = _run(_PROBE, ["redis"], env=env)
    assert done.returncode == 1
    assert done.stdout.startswith("redis: FAIL unexpected ValueError")
    assert "Traceback" not in done.stderr


# --- cypher.py: a function that runs Cypher text or parses XML (FRE-1585) -------------------

# Seen live on the test Neo4j (read access mode): the first query returned 171 rows of
# configuration. A read transaction does not stop a read-mode procedure, and the procedure
# check skips strings, so apoc.cypher.* ran a blocked procedure from inside a string.
_BLOCKED_FUNCTIONS = [
    'RETURN apoc.cypher.runFirstColumnSingle("CALL dbms.listConfig() YIELD name '
    'RETURN count(name)", {}) AS n',
    "RETURN apoc.cypher.runFirstColumnMany('CALL apoc.load.json(\"http://x\") YIELD value "
    "RETURN value', {}) AS v",
    "RETURN apoc.cypher.runFirstColumnSingle('LOAD CSV FROM \"http://x\" AS r RETURN r', {})",
    "RETURN APOC.CYPHER.RUNFIRSTCOLUMNSINGLE('RETURN 1', {})",
    "RETURN `apoc`.`cypher`.`runFirstColumnSingle`('RETURN 1', {})",
    "RETURN `apoc.cypher`.runFirstColumnSingle('RETURN 1', {})",
    "RETURN `apoc.cypher.runFirstColumnSingle`('RETURN 1', {})",
    "RETURN apoc /* x */ . cypher . runFirstColumnSingle('RETURN 1', {})",
    "WITH 1 AS x RETURN apoc.xml.parse('<a/>') AS doc",
    # A range bound starts a name after two dots (code review, FRE-1585).
    "RETURN [1, 2, 3][0..apoc.cypher.runFirstColumnSingle('CALL dbms.listConfig() YIELD name "
    "RETURN count(name)', {})] AS x",
    "MATCH (n) RETURN (n).apoc.cypher.runFirstColumnSingle('RETURN 1', {})",
]

# Neo4j reads a \u escape in a name: apoc.\u0063ypher ran on the test server (FRE-1585).
_ESCAPES = [
    "RETURN apoc.\\u0063ypher.runFirstColumnSingle('RETURN 1', {}) AS x",
    "RETURN `apoc`.`\\u0063ypher`.runFirstColumnSingle('RETURN 1', {}) AS x",
    "CALL dbms.\\u006cistConfig() YIELD name RETURN name",
]

# A line comment ends at every line terminator, so the text after it is read (FRE-1585).
_COMMENT_ENDS = [
    f"// x{end}SHOW SETTINGS YIELD name RETURN name"
    for end in ("\r", "\x0b", "\x0c", "\x85", "\u2028", "\u2029")
] + ["// x\rCALL dbms.listConfig() YIELD name RETURN name"]


@pytest.mark.parametrize("query", _BLOCKED_FUNCTIONS)
def test_cypher_refuses_a_function_that_runs_cypher_text_or_parses_xml(query: str) -> None:
    done = _run(_CYPHER, [query], env={"AGENT_NEO4J_PASSWORD": "s3cret"})
    line = _plain_error(done, 1)
    assert "function" in line and "is not allowed" in line


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n) RETURN apoc.text.join([n.name], ',') AS names",
        "RETURN apoc.coll.sum([1, 2]) AS total",
        "MATCH (n) WHERE n.note = 'apoc.cypher.runFirstColumnSingle' RETURN n",
        "MATCH (cypher) RETURN cypher.name",
        "MATCH (n) RETURN n.apoc",
    ],
)
def test_cypher_passes_other_functions_and_the_name_in_a_string(query: str) -> None:
    assert _load(_CYPHER).find_blocked_function(query) is None


@pytest.mark.parametrize("query", _ESCAPES)
def test_cypher_refuses_a_backslash_escape_in_a_name(query: str) -> None:
    done = _run(_CYPHER, [query], env={"AGENT_NEO4J_PASSWORD": "s3cret"})
    assert "backslash" in _plain_error(done, 1)


def test_a_backslash_inside_a_string_is_data() -> None:
    assert not _load(_CYPHER).find_escape("MATCH (n) WHERE n.p = 'a\\\\b' RETURN n")


@pytest.mark.parametrize("query", _COMMENT_ENDS)
def test_cypher_reads_the_text_after_every_line_end_of_a_comment(query: str) -> None:
    cypher = _load(_CYPHER)
    assert cypher.find_write_clause(query) or cypher.find_blocked_procedure(query)


@pytest.mark.parametrize(
    "query",
    [
        "SHOW SETTINGS YIELD name RETURN name",
        "CYPHER 5 SHOW SETTINGS YIELD name RETURN name",
        "CYPHER runtime=slotted SHOW SETTINGS YIELD name RETURN name",
        "EXPLAIN SHOW SETTINGS YIELD name RETURN name",
        "CYPHER 5 PROFILE TERMINATE TRANSACTIONS 'x'",
        "MATCH (n) CALL { USE system MATCH (m) RETURN m } RETURN n",
    ],
)
def test_an_administration_command_is_refused_where_the_server_accepts_it(query: str) -> None:
    assert _load(_CYPHER).find_write_clause(query) is not None


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (start:Entity {name: 'x'})-[:RELATES_TO*1..2]->(stop) RETURN start, stop",
        "MATCH (n) WHERE n.ts > $start AND n.ts < $stop RETURN n",
        "MATCH (a) RETURN {start: a.x, stop: a.y, show: a.z} AS m",
        "MATCH (n:Show) RETURN n.grant, n.deny",
    ],
)
def test_an_administration_word_used_as_a_name_is_not_refused(query: str) -> None:
    assert _load(_CYPHER).find_write_clause(query) is None


# --- AC-3 (FRE-1585): no argument makes a reviewed script write, spawn or read elsewhere ----

# The child installs an audit hook (PEP 578) before the script runs, so it sees every file
# open, process spawn and socket connection that Python code makes, in every thread. It
# reports them on stderr after the script ends. -B keeps the interpreter from writing its own
# .pyc cache, which is not the script's doing.
_HARNESS = r"""
import json, os, runpy, sys, sysconfig
script, args = sys.argv[1], sys.argv[2:]
events = []
SPAWN = ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
         "os.fork", "os.forkpty", "pty.spawn")
CHANGE = ("os.remove", "os.rename", "os.replace", "os.rmdir", "os.mkdir", "os.chmod",
          "os.chown", "os.truncate", "os.link", "os.symlink", "os.utime", "shutil.")
def hook(event, a):
    if event == "open" and not isinstance(a[0], int):  # an int re-wraps an open descriptor
        events.append(["open", str(a[0]), None if a[1] is None else str(a[1]), a[2]])
    elif event.startswith(SPAWN):
        argv = a[1] if event == "subprocess.Popen" else a[1:2]
        events.append(["spawn", event, [str(x) for x in (argv or [])]])
    elif event.startswith(CHANGE):
        events.append(["change", event, repr(a)[:200]])
    elif event == "socket.connect":
        addr = a[1]
        shown = f"{addr[0]}:{addr[1]}" if isinstance(addr, tuple) else repr(addr)
        events.append(["connect", shown])
sys.addaudithook(hook)
sys.argv = [script, *args]
code = 0
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as exc:
    code = exc.code
roots = sorted({sys.prefix, sys.base_prefix, sys.exec_prefix, os.path.dirname(script),
                *sysconfig.get_paths().values()})
sys.__stderr__.write("AUDIT:" + json.dumps({"code": code, "events": events, "roots": roots}) + "\n")
"""

#: Device files that the interpreter reads for itself (random seeds) or that hold no data.
_DEVICES = frozenset({"/dev/null", "/dev/urandom", "/dev/random"})
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def _audit(
    script: Path, args: list[str], stdin: str = "", env: dict[str, str] | None = None
) -> dict[str, object]:
    clean = {"PATH": os.environ.get("PATH", ""), **(env or {})}
    done = subprocess.run(
        [sys.executable, "-B", "-c", _HARNESS, str(script), *args],
        input=stdin.encode(),
        capture_output=True,
        timeout=60,
        env=clean,
    )
    lines = [ln for ln in done.stderr.decode().splitlines() if ln.startswith("AUDIT:")]
    assert len(lines) == 1, done.stderr.decode()
    return json.loads(lines[0][len("AUDIT:") :])


def _writes(report: dict[str, object]) -> list[object]:
    found = []
    for event in report["events"]:  # type: ignore[union-attr]
        if event[0] == "change":
            found.append(event)
        elif event[0] == "open":
            _, path, mode, flags = event
            if (mode and set(mode) & set("wax+")) or (flags or 0) & _WRITE_FLAGS:
                if path != "/dev/null":
                    found.append(event)
    return found


def _reads_outside(report: dict[str, object]) -> list[str]:
    roots = [str(Path(r).resolve()) for r in report["roots"]]  # type: ignore[union-attr]
    paths = [e[1] for e in report["events"] if e[0] == "open"]  # type: ignore[union-attr]
    return [
        p
        for p in paths
        if p not in _DEVICES
        and not any(str(Path(p).resolve()).startswith(root + os.sep) for root in roots)
    ]


def _spawns(report: dict[str, object]) -> list[list[str]]:
    return [e[2] for e in report["events"] if e[0] == "spawn"]  # type: ignore[union-attr]


def _connects(report: dict[str, object]) -> list[str]:
    return [e[1] for e in report["events"] if e[0] == "connect"]  # type: ignore[union-attr]


def test_the_audit_harness_sees_a_write_a_spawn_a_read_and_a_connect(tmp_path: Path) -> None:
    """Seeded negative for the instrument: a script that does each thing is caught."""
    bad = tmp_path / "bad.py"
    port = _closed_port()
    bad.write_text(
        "import socket, subprocess\n"
        f"open({str(tmp_path / 'out')!r}, 'w').write('x')\n"
        "open('/etc/hostname').read()\n"
        "subprocess.run(['true'])\n"
        f"socket.socket().connect_ex(('127.0.0.1', {port}))\n"
    )
    report = _audit(bad, [])
    assert [e[1] for e in _writes(report)] == [str(tmp_path / "out")]
    assert "/etc/hostname" in _reads_outside(report)
    assert _spawns(report) == [["true"]]
    assert _connects(report) == [f"127.0.0.1:{port}"]


_HOSTILE = [
    ["/etc/passwd"],
    ["../../../../etc/passwd"],
    ["-c", "import os; os.system('id')"],
    ["--output=/tmp/fre1585-out"],
    ["-o", "/tmp/fre1585-out"],
    ["$(id)"],
    [";touch /tmp/fre1585-pwn"],
    ["all", "/etc/passwd"],
    ["--help", "/etc/passwd"],
    ["-m", "http.server"],
]


def _assert_contained(
    report: dict[str, object], allowed_connects: frozenset[str] = frozenset()
) -> None:
    assert _writes(report) == [], _writes(report)
    assert _reads_outside(report) == [], _reads_outside(report)
    assert _spawns(report) == [], _spawns(report)
    assert set(_connects(report)) <= allowed_connects, _connects(report)


@pytest.mark.parametrize("args", _HOSTILE, ids=[" ".join(a) for a in _HOSTILE])
@pytest.mark.parametrize(
    "script", [_CLASSIFIER, _TIMELINE, _PROBE, _CYPHER, _VALIDATE], ids=lambda p: p.stem
)
def test_ac3_a_hostile_argument_writes_spawns_reads_and_connects_nothing(
    script: Path, args: list[str]
) -> None:
    report = _audit(script, args, stdin="agent-logs-2026.10.01\n/etc/passwd\n")
    # The script ran and refused, or found nothing. argparse answers --help first, with 0.
    expected = (0,) if script == _VALIDATE and args[0] == "--help" else (1, 2)
    assert report["code"] in expected
    _assert_contained(report)


def test_ac3_the_classifier_and_the_timeline_read_only_stdin() -> None:
    names = "agent-logs-2026.10.01\nagent-logs-2026-10\n/etc/passwd\n"
    report = _audit(_CLASSIFIER, ["agent-logs"], stdin=names)
    assert report["code"] == 0
    _assert_contained(report)
    log = "10:00:00.000Z model_call_completed trace_id=abc123 input_tokens=1 role=primary\n"
    report = _audit(_TIMELINE, ["abc123"], stdin=log)
    assert report["code"] == 0
    _assert_contained(report)


_PSQL_ARGV = [
    "psql", "-w", "-h", "127.0.0.1", "-p", "1", "-U", "u", "-d", "app", "-tAc", "SELECT 1",
]


@pytest.mark.parametrize("service", ["postgres", "all"])
def test_ac3_the_probe_spawns_only_the_fixed_psql_and_connects_only_to_the_environment(
    service: str,
) -> None:
    es, neo, redis = _closed_port(), _closed_port(), _closed_port()
    env = {
        "AGENT_DATABASE_URL": "postgresql+asyncpg://u:s3cret@127.0.0.1:1/app",
        "AGENT_ELASTICSEARCH_URL": f"http://127.0.0.1:{es}",
        "AGENT_NEO4J_URI": "bolt://127.0.0.1:7687",
        "AGENT_EVENT_BUS_REDIS_URL": f"redis://127.0.0.1:{redis}/0",
    }
    del neo
    report = _audit(_PROBE, [service], env=env)
    assert report["code"] == 1  # nothing listens: the probe ran and failed
    assert _spawns(report) == [_PSQL_ARGV]  # the seeded positive: the spawn is seen
    assert _writes(report) == []
    assert _reads_outside(report) == []
    expected = (
        set()
        if service == "postgres"
        else {f"127.0.0.1:{es}", "127.0.0.1:7474", f"127.0.0.1:{redis}"}
    )
    assert set(_connects(report)) == expected


_HOSTILE_QUERIES = [*_WRITES, *_BLOCKED_PROCEDURES, *_BLOCKED_FUNCTIONS, *_ESCAPES, *_COMMENT_ENDS]


@pytest.mark.parametrize("query", _HOSTILE_QUERIES)
def test_ac3_cypher_refuses_a_hostile_query_before_any_connection(query: str) -> None:
    port = _closed_port()
    env = {"AGENT_NEO4J_PASSWORD": "s3cret", "AGENT_NEO4J_URI": f"bolt://127.0.0.1:{port}"}
    report = _audit(_CYPHER, [query], env=env)
    assert report["code"] == 1
    _assert_contained(report)


def test_ac3_cypher_connects_only_to_the_configured_server() -> None:
    """Seeded positive: a query that passes the checks does reach the socket, and only there."""
    port = _closed_port()
    env = {"AGENT_NEO4J_PASSWORD": "s3cret", "AGENT_NEO4J_URI": f"bolt://127.0.0.1:{port}"}
    query = "MATCH (n) RETURN count(n)"
    report = _audit(_CYPHER, ["--param", "x=/etc/passwd", query], env=env)
    assert report["code"] == 1  # nothing listens
    assert _writes(report) == [] and _spawns(report) == []
    assert _connects(report) and set(_connects(report)) == {f"127.0.0.1:{port}"}


def test_ac3_every_script_rule_names_a_script_that_this_file_tests() -> None:
    """A python3 allow rule cannot land without the argument tests above (FRE-1585)."""
    from personal_agent.governance.permissions import load_permission_rules

    repo = _SKILLS.parents[1]
    named = {
        rule.bash_words[1]
        for rule in load_permission_rules().allow
        if rule.bash_words and rule.bash_prefix and rule.bash_words[0] == "python3"
    }
    tested = {
        str(p.relative_to(repo)) for p in (_CLASSIFIER, _TIMELINE, _PROBE, _CYPHER, _VALIDATE)
    }
    assert named and named <= tested


# --- AC-3 for validate_mermaid.py (FRE-1585): only a .mmd file, mmdc with a fixed argv -------


def _fake_mmdc_dir(tmp_path: Path) -> dict[str, str]:
    """A PATH whose mmdc writes a small SVG to the -o path, as the real one does."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mmdc = bin_dir / "mmdc"
    mmdc.write_text('#!/bin/sh\necho "<svg></svg>" > "$4"\n')
    mmdc.chmod(mmdc.stat().st_mode | stat.S_IEXEC)
    return {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}", "TMPDIR": str(tmp_path / "t")}


@pytest.mark.parametrize("name", ["passwd", "m.txt", "m.mmd.py", ".env"])
def test_ac3_the_mermaid_check_reads_no_file_that_is_not_a_diagram(
    tmp_path: Path, name: str
) -> None:
    target = tmp_path / name
    target.write_text("AGENT_SECRET=s3cret\n")
    report = _audit(_VALIDATE, [str(target)], env=_fake_mmdc_dir(tmp_path))
    assert report["code"] == 2
    _assert_contained(report)


def test_ac3_the_mermaid_check_refuses_a_link_named_like_a_diagram(tmp_path: Path) -> None:
    secret = tmp_path / "secret.env"
    secret.write_text("AGENT_SECRET=s3cret\n")
    (tmp_path / "m.mmd").symlink_to(secret)
    report = _audit(_VALIDATE, [str(tmp_path / "m.mmd")], env=_fake_mmdc_dir(tmp_path))
    assert report["code"] == 2
    _assert_contained(report)


def test_ac3_the_mermaid_check_runs_mmdc_with_an_absolute_path_and_its_own_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _fake_mmdc_dir(tmp_path)
    (tmp_path / "t").mkdir()
    (tmp_path / "-p.mmd").write_text("flowchart TD\n    A --> B\n")
    done = subprocess.run(
        [sys.executable, "-B", "-c", _HARNESS, str(_VALIDATE), "--", "-p.mmd"],
        capture_output=True,
        timeout=60,
        env=env,
        cwd=tmp_path,
    )
    lines = [ln for ln in done.stderr.decode().splitlines() if ln.startswith("AUDIT:")]
    report = json.loads(lines[0][len("AUDIT:") :])
    assert report["code"] == 0, done.stdout
    [argv] = _spawns(report)  # the seeded positive: the one spawn is seen
    assert argv[:3] == ["mmdc", "-i", str(tmp_path / "-p.mmd")]
    assert argv[3] == "-o"
    scratch = Path(argv[4]).parent
    assert scratch.parent == tmp_path / "t" and scratch.name.startswith("mermaid-check-")
    assert not scratch.exists()  # removed after the run
    # Every change is inside TMPDIR: the scratch directory, made then removed, and the probe
    # file that tempfile writes and removes to check that the directory is writable.
    changes = [e for e in report["events"] if e[0] == "change"]
    assert ["change", "shutil.rmtree", repr((str(scratch), None))] in changes, changes
    # shutil.rmtree removes the SVG through a descriptor of the scratch directory, so that
    # event names the file without its directory.
    by_path = [
        e
        for e in changes
        if not (e[1] == "os.remove" and e[2].startswith("('diagram.svg', ") and "-1" not in e[2])
    ]
    assert all(f"{tmp_path / 't'}/" in e[2] for e in by_path), changes
    opened = [e[1] for e in _writes(report) if e[0] == "open"]
    assert all(path.startswith(f"{tmp_path / 't'}/") for path in opened), opened
    assert _connects(report) == []
