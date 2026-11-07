# ruff: noqa: D101, D102
"""The publish check verdict and idempotent delivery of a whole job (FRE-1686 AC-1, AC-4, AC-5).

The supervisor and the pipeline are real, on the test stack (:5433). GitHub is local bare repos,
R2 is in memory, and the check verdict is a fake (the real check is ``test_publish_check_live.py``).
Apply migrations 0039 and 0040 to the test stack first (see ``conftest.py``).
"""

from __future__ import annotations

import io
import json
import tarfile

import pytest

from personal_agent.dev_agent import repo_pipeline
from personal_agent.dev_agent.jobs import JobState
from personal_agent.exceptions import DevAgentLaunchError

from .conftest import MakeJob, expire_lease
from .delivery_fakes import (
    CrashPoint,
    Delivery,
    FakeChecker,
    Seed,
    sha256,
    wait_end,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PAGE = "<!doctype html><title>Counter &amp; more</title><button id=add>Add</button>"


class TestCheckVerdict:
    async def test_a_failed_check_ends_failed_check_and_publishes_nothing(
        self, delivery: Delivery, make_job: MakeJob
    ) -> None:
        job = await delivery.job(make_job, Seed(PAGE))
        sup = delivery.supervisor("sup-a", FakeChecker(passes=False))
        await sup.scan_once()
        await wait_end(sup)

        row = await delivery.row(job.id)
        assert (row["state"], row["failure_reason"]) == ("failed_check", "check_failed")
        assert await delivery.artifact_rows(job.user_id) == []
        assert delivery.r2.objects == {} and delivery.events == []
        stored = json.loads(row["check_result"])
        assert stored["passed"] is False
        assert [s["name"] for s in stored["steps"]] == ["load", "interaction", "repo_tests"]
        for digest in ("image_digest", "toolkit_digest", "csp_manifest_digest"):
            assert stored[digest].startswith("sha256:"), digest

    async def test_a_check_that_cannot_run_ends_failed_and_publishes_nothing(
        self, delivery: Delivery, make_job: MakeJob
    ) -> None:
        job = await delivery.job(make_job, Seed(PAGE))
        checker = FakeChecker(fail=DevAgentLaunchError("seeded: docker is down"))
        sup = delivery.supervisor("sup-a", checker)
        await sup.scan_once()
        await wait_end(sup)

        row = await delivery.row(job.id)
        assert (row["state"], row["failure_reason"]) == ("failed", "check_unavailable")
        assert await delivery.artifact_rows(job.user_id) == []
        assert checker.removed == [job.id], "the check resources are removed after a failure"

    async def test_the_check_inputs_are_the_committed_page_and_tests_only(
        self, delivery: Delivery, make_job: MakeJob
    ) -> None:
        seed = Seed(PAGE, tests={"page.spec.mjs": "test", "helper.mjs": "h"})
        job = await delivery.job(make_job, seed)
        checker = FakeChecker()
        sup = delivery.supervisor("sup-a", checker)
        await sup.scan_once()
        await wait_end(sup)

        with tarfile.open(fileobj=io.BytesIO(checker.inputs[job.id])) as tar:
            names = sorted(tar.getnames())
            page = tar.extractfile("dist/index.html")
            assert page is not None
            checked = page.read()
            helper = tar.extractfile("tests/helper.mjs")
            assert helper is not None and helper.read() == b"h"
        assert names == ["dist/index.html", "tests/helper.mjs", "tests/page.spec.mjs"]
        row = await delivery.row(job.id)
        assert checked == delivery.committed_page(row["repo"], job.id)

    async def test_a_tests_folder_over_its_cap_ends_failed_check_with_a_stored_result(
        self, delivery: Delivery, make_job: MakeJob, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """/code-review finding 7: the check judges it, and the result is stored."""
        monkeypatch.setattr(repo_pipeline, "MAX_TEST_FILES", 1)
        job = await delivery.job(make_job, Seed(PAGE, tests={"a.spec.mjs": "a", "b.spec.mjs": "b"}))
        sup = delivery.supervisor("sup-a", FakeChecker())
        await sup.scan_once()
        await wait_end(sup)

        row = await delivery.row(job.id)
        assert row["state"] == "failed_check"
        stored = json.loads(row["check_result"])
        assert stored["steps"][2]["passed"] is False
        assert "cap" in stored["steps"][2]["messages"][0]
        assert await delivery.artifact_rows(job.user_id) == []


class TestDelivery:
    async def test_a_passed_check_publishes_the_committed_page_once(
        self, delivery: Delivery, make_job: MakeJob
    ) -> None:
        """AC-5: the SHA-256 of the R2 bytes equals that of dist/index.html at the result commit."""
        job = await delivery.job(make_job, Seed(PAGE))
        sup = delivery.supervisor("sup-a", FakeChecker())
        await sup.scan_once()
        await wait_end(sup)

        row = await delivery.row(job.id)
        assert row["state"] == "succeeded"
        rows = await delivery.artifact_rows(job.user_id)
        assert len(rows) == 1 and len(delivery.r2.objects) == 1
        artifact = rows[0]
        assert artifact["id"] == row["result_artifact_id"]
        assert artifact["session_id"] == job.session_id
        assert artifact["content_type"] == "text/html; charset=utf-8"
        assert artifact["title"] == "Counter & more"
        published = delivery.r2.objects[artifact["r2_key"]]
        committed = delivery.host.blob(row["repo"], f"{row['result_commit']}:dist/index.html")
        assert sha256(published) == sha256(committed)
        assert [(job_id, w["artifact_id"]) for job_id, w in delivery.events] == [
            (job.id, str(artifact["id"]))
        ]

    async def test_a_failed_delivery_is_retried_once_and_publishes_once(
        self, delivery: Delivery, make_job: MakeJob
    ) -> None:
        """/code-review finding 4: an in-process error retries the idempotent delivery."""
        job = await delivery.job(make_job, Seed(PAGE))
        delivery.r2.fail_puts = 1
        sup = delivery.supervisor("sup-a", FakeChecker())
        await sup.scan_once()
        await wait_end(sup)

        assert (await delivery.row(job.id))["state"] == "succeeded"
        assert len(await delivery.artifact_rows(job.user_id)) == 1
        assert len(delivery.r2.objects) == 1

    async def test_two_failed_deliveries_end_the_job_failed(
        self, delivery: Delivery, make_job: MakeJob
    ) -> None:
        job = await delivery.job(make_job, Seed(PAGE))
        delivery.r2.fail_puts = 2
        sup = delivery.supervisor("sup-a", FakeChecker())
        await sup.scan_once()
        await wait_end(sup)

        row = await delivery.row(job.id)
        assert (row["state"], row["failure_reason"]) == (
            "failed",
            "delivery_failed: ConnectionError",
        )

    @pytest.mark.parametrize(
        "point", ["before_upload", "after_upload", "after_row_insert", "before_end_state"]
    )
    async def test_a_crash_during_delivery_and_a_recovery_publish_once(
        self, delivery: Delivery, make_job: MakeJob, point: CrashPoint
    ) -> None:
        """AC-4: one R2 object and one artifacts row for the job after a crash at each point."""
        job = await delivery.job(make_job, Seed(PAGE))
        delivery.hang.at = point
        first = delivery.supervisor("sup-a", FakeChecker())
        await first.scan_once()
        await delivery.hang.reached.wait()
        await first.stop()  # the crash: the work stops where it is, nothing is cleaned up
        assert (await delivery.row(job.id))["state"] == JobState.DELIVERING.value

        await expire_lease(delivery.pool, job.id)
        second = delivery.supervisor("sup-b", FakeChecker())
        await second.scan_once()  # recovery resumes the delivery (D9.6)
        await wait_end(second)

        row = await delivery.row(job.id)
        assert row["state"] == "succeeded"
        rows = await delivery.artifact_rows(job.user_id)
        assert [r["id"] for r in rows] == [row["result_artifact_id"]], "one row, no duplicate"
        assert list(delivery.r2.objects) == [rows[0]["r2_key"]], "one object, no orphan"
        committed = delivery.committed_page(row["repo"], job.id)
        assert sha256(delivery.r2.objects[rows[0]["r2_key"]]) == sha256(committed)
