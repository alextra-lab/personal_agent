# ruff: noqa: D101, D102
"""The dev-agent toolkit: asset list, digest, check and volume fill (ADR-0159 D3.3; FRE-1751).

A fake ``docker`` CLI keeps volumes in memory. The real Docker behaviour is in
``test_toolkit_live.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from personal_agent.dev_agent.toolkit import (
    MANIFEST_FILE,
    ToolkitAsset,
    _read_tree,
    check_toolkit_contents,
    fill_toolkit_volume,
    load_toolkit_assets,
    manifest_file_bytes,
    read_toolkit_volume,
    toolkit_digest,
)
from personal_agent.exceptions import DevAgentToolkitError

from .toolkit_fakes import (
    SYNTHETIC_FILES,
    FakeDocker,
    complete_volume,
    synthetic_assets,
)

_IMAGE = "seshat-dev-agent:0.1"
_VOLUME = "toolkit-under-test"


def _write_config(
    tmp_path: Path, manifest_paths: list[str], map_paths: list[str]
) -> tuple[Path, Path]:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "origin": "https://x.invalid",
                "assets": [{"name": "n", "path": p, "kind": "script"} for p in manifest_paths],
            }
        )
    )
    sub_map = tmp_path / "map.json"
    sub_map.write_text(
        json.dumps(
            {
                "origin": "https://x.invalid",
                "entries": [
                    {"lib_path": f"lib/{p}", "name": "n", "kind": "script", "sri": "sha384-AAAA"}
                    for p in map_paths
                ],
            }
        )
    )
    return manifest, sub_map


class TestLoad:
    def test_the_committed_config_gives_every_manifest_asset_with_a_sha384_pin(self) -> None:
        manifest = json.loads(Path("config/artifact_lib_manifest.json").read_text())
        expected = {a["path"] for a in manifest["assets"]}
        assets = load_toolkit_assets()
        assert {a.path for a in assets} == expected
        assert len(assets) == 10
        assert all(a.sri.startswith("sha384-") for a in assets)

    def test_an_asset_without_a_pinned_sri_is_refused(self, tmp_path: Path) -> None:
        manifest, sub_map = _write_config(tmp_path, ["a@1/a.js", "b@1/b.js"], ["a@1/a.js"])
        with pytest.raises(DevAgentToolkitError, match=r"b@1/b\.js"):
            load_toolkit_assets(manifest, sub_map)

    @pytest.mark.parametrize("bad", ["../escape.js", "/abs/escape.js", "a/../../escape.js"])
    def test_a_path_that_leaves_the_volume_is_refused(self, tmp_path: Path, bad: str) -> None:
        manifest, sub_map = _write_config(tmp_path, [bad], [bad])
        with pytest.raises(DevAgentToolkitError, match="unsafe"):
            load_toolkit_assets(manifest, sub_map)


class TestReadTree:
    def test_a_symlink_is_refused_and_never_followed(self, tmp_path: Path) -> None:
        secret = tmp_path / "outside.txt"
        secret.write_text("host file")
        volume = tmp_path / "volume"
        volume.mkdir()
        (volume / "link.js").symlink_to(secret)
        with pytest.raises(DevAgentToolkitError, match="special entry: link.js"):
            _read_tree(volume)

    def test_a_pipe_is_refused_and_never_read(self, tmp_path: Path) -> None:
        os.mkfifo(tmp_path / "pipe.js")
        with pytest.raises(DevAgentToolkitError, match="special entry: pipe.js"):
            _read_tree(tmp_path)

    def test_regular_files_in_folders_are_read(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "b.js").write_bytes(b"1")
        assert _read_tree(tmp_path) == {"a/b.js": b"1"}


class TestDigest:
    def test_it_does_not_depend_on_the_order(self) -> None:
        assets = synthetic_assets()
        assert toolkit_digest(assets) == toolkit_digest(tuple(reversed(assets)))
        assert toolkit_digest(assets).startswith("sha256:")

    def test_it_changes_when_one_pin_changes(self) -> None:
        assets = synthetic_assets()
        changed = (ToolkitAsset(assets[0].path, "sha384-other"), *assets[1:])
        assert toolkit_digest(changed) != toolkit_digest(assets)


class TestCheck:
    def test_a_correct_volume_passes(self) -> None:
        check_toolkit_contents(synthetic_assets(), complete_volume())

    def test_a_missing_asset_and_a_changed_asset_are_both_named(self) -> None:
        contents = complete_volume()
        missing, changed = sorted(SYNTHETIC_FILES)
        del contents[missing]
        contents[changed] += b"!"
        with pytest.raises(DevAgentToolkitError) as err:
            check_toolkit_contents(synthetic_assets(), contents)
        assert f"missing: {missing}" in str(err.value)
        assert f"differs from the pin: {changed}" in str(err.value)

    def test_an_extra_file_is_refused(self) -> None:
        contents = {**complete_volume(), "planted.js": b"evil()"}
        with pytest.raises(DevAgentToolkitError, match="unexpected file: planted.js"):
            check_toolkit_contents(synthetic_assets(), contents)

    def test_a_missing_manifest_file_is_refused(self) -> None:
        contents = complete_volume()
        del contents[MANIFEST_FILE]
        with pytest.raises(DevAgentToolkitError, match=MANIFEST_FILE):
            check_toolkit_contents(synthetic_assets(), contents)

    def test_a_manifest_file_with_another_digest_is_refused(self) -> None:
        stale = manifest_file_bytes((ToolkitAsset("old@1/old.js", "sha384-zzz"),))
        contents = {**complete_volume(), MANIFEST_FILE: stale}
        with pytest.raises(DevAgentToolkitError, match="digest"):
            check_toolkit_contents(synthetic_assets(), contents)


async def _fetch_good(path: str) -> bytes:
    return SYNTHETIC_FILES[path]


@pytest.mark.asyncio
class TestFill:
    async def test_it_fills_the_volume_with_every_asset_and_the_manifest_file(self) -> None:
        fake = FakeDocker()
        digest = await fill_toolkit_volume(
            _VOLUME, synthetic_assets(), _fetch_good, image=_IMAGE, runner=fake
        )
        assert fake.volumes[_VOLUME] == complete_volume()
        assert digest == toolkit_digest(synthetic_assets())
        assert fake.containers == {}, "no helper container remains"

    async def test_a_corrupt_asset_fails_before_any_docker_call(self) -> None:
        async def corrupt(path: str) -> bytes:
            data = SYNTHETIC_FILES[path]
            return data + b"x" if path.endswith(".js") else data

        fake = FakeDocker()
        with pytest.raises(DevAgentToolkitError, match=r"differs from the pin: alpha@1\.0"):
            await fill_toolkit_volume(
                _VOLUME, synthetic_assets(), corrupt, image=_IMAGE, runner=fake
            )
        assert fake.calls == []
        assert fake.volumes == {}

    async def test_an_older_volume_stays_when_the_new_bytes_are_corrupt(self) -> None:
        async def corrupt(path: str) -> bytes:
            return b"not the pinned bytes"

        fake = FakeDocker()
        fake.volumes[_VOLUME] = complete_volume()
        with pytest.raises(DevAgentToolkitError):
            await fill_toolkit_volume(
                _VOLUME, synthetic_assets(), corrupt, image=_IMAGE, runner=fake
            )
        assert fake.volumes[_VOLUME] == complete_volume()

    async def test_it_replaces_an_older_volume(self) -> None:
        fake = FakeDocker()
        fake.volumes[_VOLUME] = {"stale.js": b"old"}
        await fill_toolkit_volume(
            _VOLUME, synthetic_assets(), _fetch_good, image=_IMAGE, runner=fake
        )
        assert fake.volumes[_VOLUME] == complete_volume()

    async def test_a_volume_in_use_is_not_replaced(self) -> None:
        fake = FakeDocker()
        fake.volumes[_VOLUME] = {"stale.js": b"old"}
        fake.containers["running-job"] = [_VOLUME]
        with pytest.raises(DevAgentToolkitError, match="volume rm"):
            await fill_toolkit_volume(
                _VOLUME, synthetic_assets(), _fetch_good, image=_IMAGE, runner=fake
            )
        assert fake.volumes[_VOLUME] == {"stale.js": b"old"}

    async def test_a_failed_copy_removes_the_container_before_the_volume(self) -> None:
        fake = FakeDocker()
        fake.fail.add("cp")
        with pytest.raises(DevAgentToolkitError, match="cp"):
            await fill_toolkit_volume(
                _VOLUME, synthetic_assets(), _fetch_good, image=_IMAGE, runner=fake
            )
        assert _VOLUME not in fake.volumes, "a half-filled volume is not left behind"
        assert fake.containers == {}
        argvs = fake.argvs()
        container_rm = next(i for i, a in enumerate(argvs) if a[1] == "rm")
        volume_rm = next(i for i, a in enumerate(argvs) if a[1:3] == ["volume", "rm"])
        assert container_rm < volume_rm

    async def test_a_read_back_that_differs_removes_the_volume(self) -> None:
        fake = FakeDocker()
        fake.tamper_on_copy_in = True
        with pytest.raises(DevAgentToolkitError, match="differs"):
            await fill_toolkit_volume(
                _VOLUME, synthetic_assets(), _fetch_good, image=_IMAGE, runner=fake
            )
        assert _VOLUME not in fake.volumes
        assert fake.containers == {}


@pytest.mark.asyncio
class TestRead:
    async def test_it_returns_the_files_and_removes_its_helper_container(self) -> None:
        fake = FakeDocker()
        fake.volumes[_VOLUME] = complete_volume()
        assert await read_toolkit_volume(_VOLUME, _IMAGE, fake) == complete_volume()
        assert fake.containers == {}
        create = next(a for a in fake.argvs() if a[1] == "create")
        assert f"{_VOLUME}:/srv/artifact-lib:ro" in create
        assert create[create.index("--network") + 1] == "none"

    async def test_a_failed_copy_still_removes_the_helper_container(self) -> None:
        fake = FakeDocker()
        fake.volumes[_VOLUME] = complete_volume()
        fake.fail.add("cp")
        with pytest.raises(DevAgentToolkitError):
            await read_toolkit_volume(_VOLUME, _IMAGE, fake)
        assert fake.containers == {}
