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
