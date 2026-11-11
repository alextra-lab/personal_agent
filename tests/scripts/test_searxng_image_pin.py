# ruff: noqa: D103
"""FRE-1762 — every compose file that runs SearXNG pins the same dated tag.

FRE-1331 pinned production after ``latest`` left a four-month-old image running. The dev
compose file kept ``latest``, so dev and production could run different SearXNG versions.
This test reads the image line from each root compose file, so a moved pin in one file and
not the other fails here instead of in a search result.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from personal_agent.config.config_guard import repo_root

_IMAGE_PREFIX = "searxng/searxng:"
# Upstream publishes one tag per commit: <year>.<month>.<day>-<short commit hash>.
_DATED_TAG = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}-[0-9a-f]{7,12}$")


def _searxng_images() -> dict[str, str]:
    """Map each root compose file that runs SearXNG to its image reference."""
    found: dict[str, str] = {}
    for path in sorted(Path(repo_root()).glob("docker-compose*.yml")):
        doc = yaml.safe_load(path.read_text())
        for service in (doc.get("services") or {}).values():
            image = service.get("image", "") if isinstance(service, dict) else ""
            if isinstance(image, str) and image.startswith("searxng/searxng"):
                found[path.name] = image
    return found


class TestSearxngImagePin:
    def test_dev_and_cloud_compose_files_both_run_searxng(self) -> None:
        assert {"docker-compose.yml", "docker-compose.cloud.yml"} <= set(_searxng_images())

    def test_no_compose_file_uses_latest_or_an_untagged_image(self) -> None:
        for name, image in _searxng_images().items():
            assert image.startswith(_IMAGE_PREFIX), f"{name}: untagged image {image!r}"
            assert _DATED_TAG.match(image.removeprefix(_IMAGE_PREFIX)), (
                f"{name}: {image!r} is not a dated tag"
            )

    def test_every_compose_file_pins_the_same_tag(self) -> None:
        tags = _searxng_images()
        assert len(set(tags.values())) == 1, f"compose files disagree: {tags}"
