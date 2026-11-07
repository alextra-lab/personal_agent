"""The publish check of a dev-agent page, gateway side (ADR-0159 D7.3–D7.5; FRE-1686).

The check runs in the ``check`` container (``container_profiles.yaml``) with no network. The
gateway runs that container twice, on the same read-only inputs:

* ``page`` — Seshat's script (``docker/dev-agent/publish-check.mjs``) runs the **load** and the
  **generic interaction** steps. No model-written Node code runs in this container.
* ``tests`` — the repo's Playwright tests, the **repo tests** step.

The repo tests are code that the model wrote, and they run as the same user as the script. In one
container they could forge the result line of the other two steps, so they get a container of their
own.

This module holds the pure parts: the inputs tar, the parsing of a run's output, and the result
that the job stores (D7.5). The Docker calls are :class:`CheckLauncher`, which
:class:`~personal_agent.dev_agent.launcher.DockerLauncher` implements.
"""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID

if TYPE_CHECKING:
    # The launcher imports this module, so the type is imported for annotations only.
    from personal_agent.dev_agent.launcher import BytesResult

CheckMode = Literal["page", "tests"]

#: The steps that each run must report, in order (D7.4).
EXPECTED_STEPS: Mapping[CheckMode, tuple[str, ...]] = {
    "page": ("load", "interaction"),
    "tests": ("repo_tests",),
}
#: The time limit of each run. Their sum is the 5-minute limit of D7.3. Each one stays well under
#: the fence's action limit (``SupervisorPolicy.action_timeout_seconds``), so the fence never cuts
#: a run that is in its budget.
RUN_SECONDS: Mapping[CheckMode, float] = {"page": 90.0, "tests": 200.0}
RESULT_PREFIX = b"SESHAT_CHECK_RESULT "
MAX_MESSAGES = 50
MAX_MESSAGE_CHARS = 500
#: Caps of the exported ``tests/`` folder.
MAX_TEST_FILES = 500
MAX_TEST_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class CheckSetup:
    """What the launcher prepared for the runs of one check.

    Attributes:
        image_id: The id of the check image. Both runs start from it, not from the tag.
        toolkit_digest: The digest of the toolkit volume, checked against the pins.
    """

    image_id: str
    toolkit_digest: str


class CheckLauncher(Protocol):
    """The Docker side of the check. Every resource carries the job and scope labels."""

    async def prepare_check(self, job_id: UUID, inputs: bytes) -> CheckSetup:
        """Check the toolkit, resolve the image, and fill the input volumes from ``inputs``."""
        ...

    async def run_check(
        self, job_id: UUID, setup: CheckSetup, mode: CheckMode, timeout: float
    ) -> BytesResult:
        """Run one check container to its end, then remove it. Return its output."""
        ...

    async def remove_check(self, job_id: UUID) -> None:
        """Remove the check containers and the input volumes of the job."""
        ...


@dataclass(frozen=True)
class CheckStep:
    """One step of the check.

    Attributes:
        name: ``load``, ``interaction`` or ``repo_tests``.
        passed: Whether the step passed.
        messages: The console, page and CSP messages, or why the step failed.
    """

    name: str
    passed: bool
    messages: tuple[str, ...]


@dataclass(frozen=True)
class CheckResult:
    """The result that the job stores (D7.5).

    Attributes:
        steps: The three steps, in order.
        image_digest: The id of the check image that ran.
        toolkit_digest: The digest of the toolkit.
        csp_manifest_digest: ``sha256:`` of the CSP manifest that the page run used, or ``None``
            when the page run reported none.
    """

    steps: tuple[CheckStep, ...]
    image_digest: str
    toolkit_digest: str
    csp_manifest_digest: str | None

    @property
    def passed(self) -> bool:
        """All three steps passed, and every digest is known."""
        names = tuple(s.name for s in self.steps)
        expected = EXPECTED_STEPS["page"] + EXPECTED_STEPS["tests"]
        return (
            names == expected
            and all(s.passed for s in self.steps)
            and self.csp_manifest_digest is not None
        )

    def to_json(self) -> dict[str, object]:
        """Return the JSON object for ``dev_jobs.check_result``."""
        return {
            "passed": self.passed,
            "steps": [
                {"name": s.name, "passed": s.passed, "messages": list(s.messages)}
                for s in self.steps
            ],
            "image_digest": self.image_digest,
            "toolkit_digest": self.toolkit_digest,
            "csp_manifest_digest": self.csp_manifest_digest,
        }


def _clip(messages: Sequence[object]) -> tuple[str, ...]:
    return tuple(str(m)[:MAX_MESSAGE_CHARS] for m in list(messages)[:MAX_MESSAGES])


def _failed(mode: CheckMode, why: str) -> list[CheckStep]:
    return [CheckStep(name, False, (why,)) for name in EXPECTED_STEPS[mode]]


def parse_check_output(mode: CheckMode, run: BytesResult) -> tuple[list[CheckStep], str | None]:
    """Read the steps of one run from its output.

    The script prints one result line at the end. A run with no valid line, another set of steps,
    a nonzero exit or a timeout fails every step of the run, with the reason as a message. This
    function never raises: a check that crashes must not read as a pass.

    Args:
        mode: The run.
        run: The output of the run.

    Returns:
        The steps of the run, and the CSP manifest digest (``sha256:<hex>``) of a page run, or
        ``None``.
    """
    if run.returncode == 124:
        return _failed(mode, f"the {mode} run passed its time limit"), None
    if run.returncode != 0:
        tail = run.stderr.strip()[-300:]
        return _failed(mode, f"the {mode} run exited with {run.returncode}: {tail}"), None
    lines = [line for line in run.stdout.splitlines() if line.startswith(RESULT_PREFIX)]
    if not lines:
        return _failed(mode, f"the {mode} run printed no result"), None
    try:
        body = json.loads(lines[-1][len(RESULT_PREFIX) :])
        raw_steps = body["steps"]
        steps = [
            CheckStep(
                name=str(s["name"]),
                passed=s["passed"] is True,
                messages=_clip(s.get("messages") or []),
            )
            for s in raw_steps
        ]
    except ValueError, KeyError, TypeError, AttributeError:
        return _failed(mode, f"the {mode} run printed a bad result"), None
    if body.get("mode") != mode or tuple(s.name for s in steps) != EXPECTED_STEPS[mode]:
        return _failed(mode, f"the {mode} run reported other steps"), None
    manifest = body.get("csp_manifest_sha256") if mode == "page" else None
    digest = f"sha256:{manifest}" if isinstance(manifest, str) and len(manifest) == 64 else None
    return steps, digest


def check_inputs_tar(page: bytes, tests: Mapping[str, bytes]) -> bytes:
    """Return the tar that fills the input volumes: ``dist/index.html`` and ``tests/``.

    The page is the bytes that delivery publishes (``GitMirror.read_page``). Every entry is a
    regular file with mode 0644 and a fixed time, so the tar depends on the bytes only.

    Args:
        page: The page bytes.
        tests: The files under ``tests/``, by path relative to the repo root.

    Returns:
        The tar bytes, for ``docker cp``.

    Raises:
        ValueError: If a test path is not under ``tests/`` or has a ``..`` part.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path, data in [("dist/index.html", page), *sorted(tests.items())]:
            if path != "dist/index.html" and (
                not path.startswith("tests/") or ".." in path.split("/")
            ):
                raise ValueError(f"{path!r} is not a file under tests/")
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def page_problem_of(page: bytes) -> str | None:
    """Return why the page cannot be published as it is, or ``None``.

    The artifact path stores non-empty text, so a page that is not UTF-8, or is empty or blank,
    fails the load step instead of failing later at the publish.

    Args:
        page: The page bytes.

    Returns:
        The reason, or ``None``.
    """
    try:
        text = page.decode("utf-8")
    except UnicodeDecodeError:
        return "dist/index.html is not valid UTF-8"
    if not text.strip():
        return "dist/index.html is empty"
    return None


def check_result(
    setup: CheckSetup,
    page_steps: Sequence[CheckStep],
    tests_steps: Sequence[CheckStep],
    csp_manifest_digest: str | None,
    *,
    page_problem: str | None,
    tests_problem: str | None = None,
) -> CheckResult:
    """Combine the two runs into the stored result.

    Args:
        setup: The image and toolkit digests.
        page_steps: The steps of the page run.
        tests_steps: The steps of the tests run.
        csp_manifest_digest: The digest that the page run reported.
        page_problem: What :func:`page_problem_of` found. It fails the load step.
        tests_problem: Why ``tests/`` could not be exported (over a cap). It fails the repo tests
            step.

    Returns:
        The result.
    """
    steps = list(page_steps)
    if page_problem is not None and steps and steps[0].name == "load":
        load = steps[0]
        steps[0] = CheckStep("load", False, _clip([page_problem, *load.messages]))
    tests = list(tests_steps)
    if tests_problem is not None and tests and tests[0].name == "repo_tests":
        tests[0] = CheckStep("repo_tests", False, _clip([tests_problem, *tests[0].messages]))
    return CheckResult(
        steps=(*steps, *tests),
        image_digest=setup.image_id,
        toolkit_digest=setup.toolkit_digest,
        csp_manifest_digest=csp_manifest_digest,
    )
