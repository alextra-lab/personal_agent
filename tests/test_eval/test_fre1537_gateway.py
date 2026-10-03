"""FRE-1511 / ADR-0154 D7: the standalone launcher never touches production and never uses compose (AC-1)."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from scripts.eval.fre1537 import gateway

PROBE_DIR = Path(gateway.__file__).parent
HOST_ENV = {
    "POSTGRES_PASSWORD": "pg-secret-123",
    "NEO4J_PASSWORD": "neo-secret-456",
    "AGENT_OWNER_EMAIL": "owner@example.invalid",
    "AGENT_SKILL_ROUTING_MODE": "keyword",
}
NETWORK = "stack_net"


class FakeDocker:
    """A dict-driven stand-in for the docker CLI. It records every argv."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[Mapping[str, str] | None] = []
        self.containers: dict[str, dict[str, object]] = {
            name: {
                "labels": {},
                "networks": [NETWORK],
                "status": "running",
                "health": "healthy",
                "id": f"id-{name}",
                "started": "t0",
            }
            for name in (*gateway.EVAL_SUBSTRATES, gateway.PRODUCTION_GUARD)
        }
        self.images: set[str] = set()

    def __call__(
        self,
        args: Sequence[str],
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        check: bool = True,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        argv = list(args)
        self.calls.append(argv)
        self.envs.append(env)
        out = self._dispatch(argv)
        if out is None:
            if check:
                raise subprocess.CalledProcessError(1, ["docker", *argv])
            return subprocess.CompletedProcess(argv, 1, "", "no such object")
        return subprocess.CompletedProcess(argv, 0, out, "")

    def _dispatch(self, argv: list[str]) -> str | None:
        head = argv[0]
        if head == "inspect":
            fmt, name = argv[2], argv[3]
            c = self.containers.get(name)
            if c is None:
                return None
            if fmt == gateway.FMT_LABEL:
                return str(c["labels"].get(gateway.LABEL, ""))  # type: ignore[union-attr]
            if fmt == gateway.FMT_NETWORKS:
                return json.dumps(c["networks"])
            if fmt == gateway.FMT_STATE:
                return f"{c['status']} {c['health']}"
            if fmt == gateway.FMT_IDENTITY:
                return f"{c['id']} {c['started']}"
            raise AssertionError(f"unexpected format {fmt}")
        if head == "image" and argv[1] == "inspect":
            return "ok" if argv[-1] in self.images else None
        if head == "build":
            self.images.add(argv[argv.index("-t") + 1])
            return ""
        if head == "run":
            name = argv[argv.index("--name") + 1]
            self.containers[name] = {
                "labels": {gateway.LABEL: "1"},
                "networks": [argv[argv.index("--network") + 1]],
                "status": "running",
                "health": "",
                "id": f"id-{name}",
                "started": "t1",
            }
            return "container-id\n"
        if head == "rm":
            self.containers.pop(argv[-1], None)
            return ""
        raise AssertionError(f"unexpected docker call {argv}")

    def runs(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "run"]


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    fake = FakeDocker()
    monkeypatch.setattr(gateway, "run_docker", fake)
    monkeypatch.setattr(gateway, "wait_healthy", lambda port, fingerprint, timeout=180.0: None)
    monkeypatch.setattr(gateway, "build_fingerprint", lambda: "f" * 64)
    return fake


def test_gateway_environment_is_eval_only_and_production_shaped() -> None:
    env, secrets = gateway.gateway_env(HOST_ENV)
    assert env["AGENT_DEPLOYMENT_PROFILE"] == "eval" and env["APP_ENV"] == "eval"
    assert (
        env["AGENT_PRIMITIVE_TOOLS_ENABLED"] == "true" and env["AGENT_PREFER_PRIMITIVES"] == "true"
    )
    assert env["AGENT_DELEGATION_ENABLED"] == "false" and env["AGENT_EXPANSION_ENABLED"] == "false"
    assert (
        env["AGENT_MCP_GATEWAY_ENABLED"] == "false" and env["AGENT_GATEWAY_AUTH_ENABLED"] == "false"
    )
    assert env["AGENT_SLM_BASE_URL"] == "http://fre1537-stub:8700"
    assert env["AGENT_SKILL_ROUTING_MODE"] == "keyword"
    assert "AGENT_EVENT_BUS_ENABLED" not in env
    # The eval profile refuses to boot without these two. The model is a stub, so no real key is passed.
    for key in ("AGENT_ANTHROPIC_API_KEY", "AGENT_OPENAI_API_KEY"):
        assert env[key] == gateway.PLACEHOLDER_KEY
        assert key not in HOST_ENV
    assert {
        "AGENT_DATABASE_URL",
        "AGENT_DATABASE_ADMIN_URL",
        "AGENT_NEO4J_PASSWORD",
        "AGENT_OWNER_EMAIL",
    } <= secrets
    gateway.assert_eval_only(env)


@pytest.mark.parametrize("name", ["POSTGRES_PASSWORD", "NEO4J_PASSWORD", "AGENT_OWNER_EMAIL"])
def test_a_missing_credential_stops_the_launcher_and_names_it(name: str) -> None:
    host = {k: v for k, v in HOST_ENV.items() if k != name}
    with pytest.raises(SystemExit, match=name):
        gateway.gateway_env(host)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://u:p@postgres:5432/personal_agent",
        "bolt://neo4j:7687",
        "http://elasticsearch:9200",
        "http://caddy:8600",
    ],
)
def test_a_production_host_in_the_environment_is_refused(url: str) -> None:
    env, _ = gateway.gateway_env(HOST_ENV)
    env["AGENT_SOMETHING_URL"] = url
    with pytest.raises(SystemExit, match="production"):
        gateway.assert_eval_only(env)


def test_secrets_travel_by_name_and_never_appear_in_argv() -> None:
    env, secrets = gateway.gateway_env(HOST_ENV)
    argv, process_env = gateway.env_flags(env, secrets)
    text = " ".join(argv)
    for secret in ("pg-secret-123", "neo-secret-456", "owner@example.invalid"):
        assert secret not in text
        assert any(secret in v for v in process_env.values())
    assert "AGENT_DATABASE_URL" in argv and "AGENT_DATABASE_URL=" not in text
    assert "AGENT_DEPLOYMENT_PROFILE=eval" in argv


def test_up_starts_two_labelled_containers_on_the_eval_network(
    docker: FakeDocker, tmp_path: Path
) -> None:
    gateway.up(tmp_path, HOST_ENV, port=9012)
    stub_run, gw_run = docker.runs()
    for run in (stub_run, gw_run):
        assert run[run.index("--network") + 1] == NETWORK
        assert f"{gateway.LABEL}=1" in run
    assert gateway.STUB_NAME in stub_run and gateway.GATEWAY_NAME in gw_run
    assert "127.0.0.1:9012:9001" in gw_run
    assert "--user" in stub_run
    assert (
        "--no-healthcheck" in stub_run
    )  # the image's gateway healthcheck does not apply to the stub
    assert any(f"{tmp_path.resolve() / 'stub-out'}:/out" in a for a in stub_run)


def test_up_builds_under_its_own_image_tag_never_a_production_one(
    docker: FakeDocker, tmp_path: Path
) -> None:
    gateway.up(tmp_path, HOST_ENV, port=9012)
    (build,) = [c for c in docker.calls if c[0] == "build"]
    tag = build[build.index("-t") + 1]
    assert tag.startswith("fre1537-probe-gateway:") and "seshat-gateway" not in tag
    docker.calls.clear()
    gateway.up(tmp_path, HOST_ENV, port=9012)
    assert not [c for c in docker.calls if c[0] == "build"]  # the tag exists: no rebuild


def test_up_never_calls_compose_and_removes_only_labelled_containers(
    docker: FakeDocker, tmp_path: Path
) -> None:
    gateway.up(tmp_path, HOST_ENV, port=9012)
    gateway.up(tmp_path, HOST_ENV, port=9012)  # a second up replaces its own containers
    gateway.down()
    flat = [a for call in docker.calls for a in call]
    assert "compose" not in flat and "docker-compose" not in flat
    removed = {c[-1] for c in docker.calls if c[0] == "rm"}
    assert removed <= {gateway.STUB_NAME, gateway.GATEWAY_NAME}
    assert not set(docker.containers) & {gateway.STUB_NAME, gateway.GATEWAY_NAME}
    assert gateway.PRODUCTION_GUARD in docker.containers


def test_up_refuses_a_name_held_by_a_container_it_did_not_create(
    docker: FakeDocker, tmp_path: Path
) -> None:
    docker.containers[gateway.GATEWAY_NAME] = {
        "labels": {},
        "networks": [NETWORK],
        "status": "running",
        "health": "",
        "id": "x",
        "started": "t",
    }
    with pytest.raises(SystemExit, match="not created by this probe"):
        gateway.up(tmp_path, HOST_ENV, port=9012)
    assert not docker.runs() and not [c for c in docker.calls if c[0] == "rm"]


def test_up_stops_when_an_eval_substrate_is_missing(docker: FakeDocker, tmp_path: Path) -> None:
    del docker.containers["cloud-sim-neo4j-eval"]
    with pytest.raises(SystemExit, match="cloud-sim-neo4j-eval"):
        gateway.up(tmp_path, HOST_ENV, port=9012)
    assert not docker.runs()


def test_up_stops_when_a_substrate_is_unhealthy(docker: FakeDocker, tmp_path: Path) -> None:
    docker.containers["cloud-sim-postgres-eval"]["health"] = "unhealthy"
    with pytest.raises(SystemExit, match="healthy"):
        gateway.up(tmp_path, HOST_ENV, port=9012)


def test_the_network_must_be_the_one_network_all_substrates_share(
    docker: FakeDocker, tmp_path: Path
) -> None:
    docker.containers["cloud-sim-redis-eval"]["networks"] = ["other_net"]
    with pytest.raises(SystemExit, match="network"):
        gateway.up(tmp_path, HOST_ENV, port=9012)
    docker.containers["cloud-sim-redis-eval"]["networks"] = [NETWORK, "second"]
    for name in gateway.EVAL_SUBSTRATES:
        docker.containers[name]["networks"] = [NETWORK, "second"]
    with pytest.raises(SystemExit, match="network"):
        gateway.up(tmp_path, HOST_ENV, port=9012)


def test_up_stops_if_the_production_searxng_container_changed(
    docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = docker._dispatch

    def recreate_after_run(argv: list[str]) -> str | None:
        out = original(argv)
        if argv[0] == "run":
            docker.containers[gateway.PRODUCTION_GUARD]["id"] = "recreated"
        return out

    monkeypatch.setattr(docker, "_dispatch", recreate_after_run)
    with pytest.raises(SystemExit, match="cloud-sim-searxng"):
        gateway.up(tmp_path, HOST_ENV, port=9012)


def test_render_runs_the_probe_file_inside_the_gateway_and_writes_the_prompts(
    docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"system": "S", "prompt_hash": "h", "fixtures": {}}
    seen: dict[str, object] = {}

    def fake(
        args: Sequence[str],
        env: object = None,
        cwd: object = None,
        check: bool = True,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        seen["args"], seen["stdin"] = list(args), stdin
        out = "2026 [info] noisy log line\n" + "FRE1537_PROMPTS=" + json.dumps(payload) + "\n"
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(gateway, "run_docker", fake)
    gateway.render(tmp_path)
    assert seen["args"][:3] == ["exec", "-i", gateway.GATEWAY_NAME]  # type: ignore[index]
    assert seen["args"][-1] == "/probe/render.py"  # type: ignore[index]
    assert {f["label"] for f in json.loads(str(seen["stdin"]))} >= {"greeting", "boiler_decline"}
    assert json.loads((tmp_path / "prompts.json").read_text()) == payload


def test_render_fails_clearly_when_the_gateway_printed_no_prompts(
    docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gateway,
        "run_docker",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, "only logs\n", ""),
    )
    with pytest.raises(SystemExit, match="FRE1537_PROMPTS"):
        gateway.render(tmp_path)


def test_no_module_of_the_probe_invokes_docker_compose() -> None:
    """AC-1: no step invokes `docker compose`."""
    pattern = re.compile(r"""["']compose["']|docker[- ]compose\s+(?:-|up|down|run|build|exec)""")
    offenders = [p.name for p in PROBE_DIR.glob("*.py") if pattern.search(p.read_text())]
    assert offenders == []


def test_no_module_of_the_probe_needs_a_path_outside_the_repo() -> None:
    """AC-1: no step needs a file outside the repo."""
    outside = "|".join(f"/{top}/" for top in ("opt", "tmp", "ho" + "me"))
    pattern = re.compile(rf"""["'](?:{outside})""")
    offenders = [p.name for p in PROBE_DIR.glob("*.py") if pattern.search(p.read_text())]
    assert offenders == []
