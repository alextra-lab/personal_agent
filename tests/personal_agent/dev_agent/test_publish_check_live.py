# ruff: noqa: D101, D102, D103
"""The publish check with real Docker, through the real supervisor (FRE-1686 AC-1, AC-2, AC-3, AC-6).

Each job runs the whole path of ``test_delivery_integration.py`` (test stack, local repos,
in-memory R2), but the checker is the real ``DockerLauncher`` with the ``check`` profile. The check
image is ``SESHAT_DEV_CHECK_TEST_IMAGE``, else ``seshat-dev-check:fre1686-test``. Build it with::

    docker build -t seshat-dev-check:fre1686-test --target check \
        --build-context csp=e2e/artifact-lib -f docker/dev-agent/Dockerfile.browser docker/dev-agent/

The toolkit is a test volume that holds one real pinned asset, Chart.js. Its bytes are read
read-only from the toolkit volume (``AGENT_DEV_AGENT_TOOLKIT_VOLUME``) and checked against the pin.
Every container and volume carries a test scope label, so the test never touches production ones.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TypedDict
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from personal_agent.config import settings
from personal_agent.dev_agent.container_spec import load_profiles
from personal_agent.dev_agent.jobs import DevJob
from personal_agent.dev_agent.launcher import (
    SCOPE_LABEL,
    BytesResult,
    DockerLauncher,
    check_container_name,
    check_volume_names,
    run_bytes,
    run_command,
)
from personal_agent.dev_agent.toolkit import (
    ToolkitAsset,
    fill_toolkit_volume,
    load_toolkit_assets,
    read_toolkit_volume,
)
from personal_agent.dev_agent.verification import inspect_violations
from personal_agent.storage.artifact_export import verify_sri

from .conftest import MakeJob
from .delivery_fakes import (
    GOOD_TEST,
    Delivery,
    Seed,
    sha256,
    wait_end,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CHART = "chartjs@4.4.7/chart.umd.js"

GOOD_PAGE = f"""<!doctype html>
<html><head><title>Clicks</title><script src="/lib/{CHART}"></script></head>
<body>
<canvas id="c" width="300" height="150"></canvas>
<button id="add">Add</button> <span id="out">0</span>
<label>Colour <select id="colour"><option>blue</option><option>red</option></select></label>
<input id="size" type="range" min="1" max="10" value="5">
<script>
const chart = new Chart(document.getElementById('c'), {{
  type: 'bar',
  data: {{ labels: ['clicks'], datasets: [{{ label: 'n', data: [0], backgroundColor: 'blue' }}] }},
  options: {{ animation: false }},
}});
const out = document.getElementById('out');
document.getElementById('add').addEventListener('click', () => {{
  chart.data.datasets[0].data[0] += 1;
  chart.update();
  out.textContent = String(chart.data.datasets[0].data[0]);
}});
document.getElementById('colour').addEventListener('change', (e) => {{
  chart.data.datasets[0].backgroundColor = e.target.value;
  chart.update();
}});
document.getElementById('size').addEventListener('input', (e) => {{
  chart.options.barPercentage = Number(e.target.value) / 10;
  chart.update();
}});
</script>
</body></html>
"""
# The mutation: the button still updates the chart, but no longer the count the test asserts.
MUTATED_PAGE = GOOD_PAGE.replace(
    "out.textContent = String(chart.data.datasets[0].data[0]);", "/* count not shown */"
)
THROWS_ON_CLICK = GOOD_PAGE.replace(
    "chart.data.datasets[0].data[0] += 1;", "throw new Error('seeded: broken button');"
)
CALLS_FETCH = GOOD_PAGE.replace(
    "const out = document.getElementById('out');",
    "const out = document.getElementById('out'); fetch('/data.json').catch(() => {});",
)

# Code review finding 3: page code that writes the benign Chromium text must not hide an error.
SPOOFS_BENIGN = GOOD_PAGE.replace(
    "const out = document.getElementById('out');",
    "const out = document.getElementById('out');"
    " console.error(\"Unrecognized Content-Security-Policy directive 'webrtc'.\");",
)

# /code-review findings 1, 2, 5, 6: controls that a correct page has, and that must not fail
# the generic interaction: a button that opens a modal dialog, a readonly field, time, month and
# week inputs, and tab buttons that set the hash.
EXTRAS_PAGE = GOOD_PAGE.replace(
    '<button id="add">Add</button>',
    '<button id="open">Settings</button>'
    '<dialog id="dlg"><p>Settings</p><button id="close">Close</button></dialog>'
    '<button id="tab">Tab 2</button>'
    '<input id="shown" readonly value="result">'
    '<input type="time"><input type="month"><input type="week">'
    '<input type="datetime-local">'
    '<button id="add">Add</button>',
).replace(
    "const out = document.getElementById('out');",
    "const out = document.getElementById('out');"
    " document.getElementById('open').onclick = () => document.getElementById('dlg').showModal();"
    " document.getElementById('close').onclick = () => document.getElementById('dlg').close();"
    " document.getElementById('tab').onclick = () => { location.hash = 'tab2'; };",
)


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(["docker", *args], capture_output=True, timeout=120, check=False)
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)}: {result.stderr.decode()}")
    return result


def _check_image() -> str | None:
    for candidate in (
        os.environ.get("SESHAT_DEV_CHECK_TEST_IMAGE"),
        "seshat-dev-check:fre1686-test",
    ):
        if candidate and _docker("image", "inspect", candidate, check=False).returncode == 0:
            return candidate
    return None


@dataclass
class Inspecting:
    """Runs ``docker`` with binary output, and inspects each check container just before its start."""

    inspects: dict[str, Mapping[str, object]] = field(default_factory=dict)

    async def __call__(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
        timeout: float = 60.0,
        max_stdout: int = 64 * 1024 * 1024,
    ) -> BytesResult:
        if list(args[:3]) == ["docker", "start", "-a"] and args[3].startswith("seshat-dev-check-"):
            out = _docker("inspect", args[3]).stdout
            self.inspects[args[3]] = json.loads(out)[0]
        return await run_bytes(args, env=env, stdin=stdin, timeout=timeout, max_stdout=max_stdout)


@dataclass
class Live:
    image: str
    volume: str
    scope: str
    runner: Inspecting
    checker: DockerLauncher

    def leftovers(self) -> list[str]:
        label = f"label={SCOPE_LABEL}={self.scope}"
        containers = _docker("ps", "-aq", "--filter", label).stdout.decode().split()
        volumes = _docker("volume", "ls", "-q", "--filter", label).stdout.decode().split()
        return containers + volumes


async def _chart_asset(image: str) -> tuple[ToolkitAsset, bytes]:
    pinned = {a.path: a for a in load_toolkit_assets()}[CHART]
    volume = settings.dev_agent_toolkit_volume
    if _docker("volume", "inspect", volume, check=False).returncode != 0:
        pytest.skip(f"no toolkit volume {volume}: run make dev-agent-toolkit")
    data = (await read_toolkit_volume(volume, image, run_command)).get(CHART)
    assert data is not None and verify_sri(data, pinned.sri), (
        "the toolkit volume differs from the pin"
    )
    return pinned, data


@pytest_asyncio.fixture
async def live() -> AsyncIterator[Live]:
    if shutil.which("docker") is None:
        pytest.skip("no docker")
    image = _check_image()
    if image is None:
        pytest.skip("no check image: build seshat-dev-check:fre1686-test")
    asset, data = await _chart_asset(image)
    tag = uuid4().hex[:8]
    volume, scope = f"fre1686-toolkit-{tag}", f"fre1686-test-{tag}"

    async def fetch(path: str) -> bytes:
        assert path == CHART
        return data

    await fill_toolkit_volume(volume, [asset], fetch, image=image, runner=run_command)
    profiles = load_profiles()
    runner = Inspecting()
    checker = DockerLauncher(
        profiles.agent.model_copy(update={"image": image}),
        network_name="none",
        toolkit_volume=volume,
        toolkit_assets=[asset],
        scope=scope,
        check_profile=profiles.check.model_copy(update={"image": image}),
        bytes_runner=runner,
    )
    state = Live(image=image, volume=volume, scope=scope, runner=runner, checker=checker)
    try:
        yield state
    finally:
        for name in state.leftovers():
            _docker("rm", "-f", name, check=False)
            _docker("volume", "rm", "-f", name, check=False)
        _docker("volume", "rm", "-f", volume, check=False)


async def _run(delivery: Delivery, live: Live, make_job: MakeJob, seed: Seed) -> DevJob:
    job = await delivery.job(make_job, seed)
    sup = delivery.supervisor("sup-live", live.checker)
    await sup.scan_once()
    await wait_end(sup, timeout=400)
    assert live.leftovers() == [], "the check leaves no container or volume"
    return job


class StoredStep(TypedDict):
    name: str
    passed: bool
    messages: list[str]


async def _steps(delivery: Delivery, job_id: UUID) -> dict[str, StoredStep]:
    row = await delivery.row(job_id)
    stored = json.loads(row["check_result"])
    for digest in ("image_digest", "toolkit_digest", "csp_manifest_digest"):
        assert str(stored[digest]).startswith("sha256:"), digest
    return {s["name"]: s for s in stored["steps"]}


async def _assert_not_published(delivery: Delivery, job: DevJob) -> None:
    row = await delivery.row(job.id)
    assert (row["state"], row["failure_reason"]) == ("failed_check", "check_failed")
    assert await delivery.artifact_rows(job.user_id) == []
    assert delivery.r2.objects == {}


class TestSeededPages:
    """AC-1: each broken page ends failed_check with no artifact row."""

    async def test_a_page_that_throws_only_on_a_click_fails_the_interaction(
        self, delivery: Delivery, live: Live, make_job: MakeJob
    ) -> None:
        job = await _run(delivery, live, make_job, Seed(THROWS_ON_CLICK))
        steps = await _steps(delivery, job.id)
        assert steps["load"]["passed"] is True
        assert steps["interaction"]["passed"] is False
        assert any("seeded: broken button" in m for m in steps["interaction"]["messages"])
        await _assert_not_published(delivery, job)

    async def test_a_page_that_calls_fetch_fails_on_a_csp_violation(
        self, delivery: Delivery, live: Live, make_job: MakeJob
    ) -> None:
        job = await _run(delivery, live, make_job, Seed(CALLS_FETCH))
        steps = await _steps(delivery, job.id)
        assert steps["load"]["passed"] is False
        assert any("CSP violation: connect-src" in m for m in steps["load"]["messages"])
        await _assert_not_published(delivery, job)

    async def test_a_repo_with_no_test_fails_the_repo_tests(
        self, delivery: Delivery, live: Live, make_job: MakeJob
    ) -> None:
        job = await _run(delivery, live, make_job, Seed(GOOD_PAGE, tests=None))
        steps = await _steps(delivery, job.id)
        assert steps["load"]["passed"] is True and steps["interaction"]["passed"] is True
        assert steps["repo_tests"]["passed"] is False
        assert "the repo has no test" in steps["repo_tests"]["messages"]
        await _assert_not_published(delivery, job)

    async def test_a_page_that_spoofs_the_benign_message_fails_the_load(
        self, delivery: Delivery, live: Live, make_job: MakeJob
    ) -> None:
        job = await _run(delivery, live, make_job, Seed(SPOOFS_BENIGN))
        steps = await _steps(delivery, job.id)
        assert steps["load"]["passed"] is False
        await _assert_not_published(delivery, job)


class TestGoodPage:
    async def test_a_chartjs_page_with_a_real_test_passes_and_is_published(
        self, delivery: Delivery, live: Live, make_job: MakeJob
    ) -> None:
        """AC-2, and AC-5 on the real path: the R2 bytes are the committed page."""
        job = await _run(delivery, live, make_job, Seed(GOOD_PAGE, {"page.spec.mjs": GOOD_TEST}))
        steps = await _steps(delivery, job.id)
        assert all(s["passed"] is True for s in steps.values()), steps
        row = await delivery.row(job.id)
        assert row["state"] == "succeeded"
        rows = await delivery.artifact_rows(job.user_id)
        assert [r["id"] for r in rows] == [row["result_artifact_id"]]
        published = delivery.r2.objects[rows[0]["r2_key"]]
        assert sha256(published) == sha256(delivery.committed_page(row["repo"], job.id))

    async def test_the_mutated_page_fails_its_repo_test(
        self, delivery: Delivery, live: Live, make_job: MakeJob
    ) -> None:
        """AC-3: the generic steps still pass, the repo test catches the broken interaction."""
        job = await _run(delivery, live, make_job, Seed(MUTATED_PAGE, {"page.spec.mjs": GOOD_TEST}))
        steps = await _steps(delivery, job.id)
        assert steps["load"]["passed"] is True and steps["interaction"]["passed"] is True
        assert steps["repo_tests"]["passed"] is False
        assert any("1 failed" in m for m in steps["repo_tests"]["messages"])
        await _assert_not_published(delivery, job)

    async def test_a_page_with_a_modal_readonly_typed_inputs_and_hash_tabs_passes(
        self, delivery: Delivery, live: Live, make_job: MakeJob
    ) -> None:
        job = await _run(delivery, live, make_job, Seed(EXTRAS_PAGE, {"page.spec.mjs": GOOD_TEST}))
        steps = await _steps(delivery, job.id)
        assert all(s["passed"] is True for s in steps.values()), steps
        assert (await delivery.row(job.id))["state"] == "succeeded"


class TestBoundary:
    async def test_docker_inspect_of_each_check_container_matches_the_profile(
        self, delivery: Delivery, live: Live, make_job: MakeJob
    ) -> None:
        """AC-6: every flag and limit of D7.3, on both runs."""
        job = await _run(delivery, live, make_job, Seed(GOOD_PAGE))
        profile = load_profiles().check
        volumes = {**check_volume_names(job.id), "/srv/artifact-lib": live.volume}
        for mode in ("page", "tests"):
            inspect = live.runner.inspects[check_container_name(job.id, mode)]
            assert inspect_violations(profile, inspect, network_name="none", volumes=volumes) == []
            image_id = _docker("image", "inspect", "--format", "{{.Id}}", live.image).stdout
            assert inspect["Image"] == image_id.decode().strip(), "created from the image id"
