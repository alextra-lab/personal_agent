"""FRE-1511 / ADR-0154 D7 Requalification: the configuration fingerprint of a probe run.

A configuration is the engine and its build, the model and quant, the planner mode's parameters and
the rendered planner system prompt. A change to any of them is a routing change and needs a new passing
run. The fingerprint also records the shape of the captured primary request, so a changed primary prompt
is visible.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx
from scripts.eval.fre1537.common import RunPaths
from scripts.eval.fre1537.fixtures import REPO_ROOT
from scripts.eval.fre1537.llama import Inputs, PlannerMode, sampling

if TYPE_CHECKING:
    from scripts.eval.fre1537.cloud import CloudTarget

MANAGED_BUILD = "managed (provider reports no build)"
MANAGED_QUANT = "managed (not reported)"
_QUANT = re.compile(r"(?:UD-)?(?:I?Q\d(?:_[A-Z0-9]+)+|BF16|F16|F32)", re.IGNORECASE)
# The fields whose change makes two runs different configurations.
_IDENTITY_PATHS = (
    ("engine", "build"),
    ("engine", "served_model"),
    ("model", "name"),
    ("model", "quant"),
    ("planner_mode",),
    ("system_prompt_sha256",),
    ("response_format_sha256",),
    ("digest_sha256",),
)


def _engine_props(client: httpx.Client, url: str) -> Mapping[str, object]:
    """Read the llama.cpp ``/props`` document. An unreachable or foreign server reads as empty."""
    parts = urlsplit(url)
    try:
        response = client.get(f"{parts.scheme}://{parts.netloc}/props", timeout=15.0)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _captured_summary(paths: RunPaths, inputs: Inputs) -> dict[str, object]:
    """Summarise the captured primary requests: tool count, tool names and system prompt length."""
    tool_sets: set[tuple[str, ...]] = set()
    system_chars: set[int] = set()
    for label in inputs.captured:
        body = inputs.body(label)
        tools = body.get("tools") or []
        assert isinstance(tools, list)
        tool_sets.add(tuple(sorted(str(t.get("function", {}).get("name")) for t in tools)))
        messages = body.get("messages") or []
        assert isinstance(messages, list)
        first = messages[0] if messages else {}
        system_chars.add(len(str(first.get("content", ""))) if first.get("role") == "system" else 0)
    names = sorted({n for s in tool_sets for n in s})
    return {
        "fixtures": len(inputs.captured),
        "tool_count": len(names),
        "tool_names_sha256": hashlib.sha256(",".join(names).encode()).hexdigest(),
        "tool_sets_differ": len(tool_sets) > 1,
        "system_prompt_chars": sorted(system_chars),
    }


def _git_head() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def build_fingerprint(
    client: httpx.Client,
    url: str,
    model: str,
    mode: PlannerMode,
    inputs: Inputs,
    paths: RunPaths,
    quant: str | None,
    engine_build: str | None,
    digest: str | None,
) -> dict[str, object]:
    """Build the configuration fingerprint of a run.

    Args:
        client: HTTP client, for the engine's ``/props``.
        url: The chat-completions URL.
        model: The served model name.
        mode: The planner mode.
        inputs: The run inputs.
        paths: The run paths.
        quant: Quant override. Used when the engine does not report one.
        engine_build: Engine build override. Used when the engine does not report one.
        digest: The digest text of a digest run, or ``None``.

    Returns:
        The fingerprint. A field that nothing reports reads ``"unknown"``, and the scorer fails it.
    """
    props = _engine_props(client, url)
    model_path = str(props.get("model_path") or "")
    quant_match = _QUANT.search(Path(model_path).name) if model_path else None
    base_sampling = sampling(inputs.body(next(iter(inputs.captured))))
    fingerprint: dict[str, object] = {
        "engine": {
            "name": "llama.cpp",
            "build": engine_build or props.get("build_info") or "unknown",
            "build_source": "cli"
            if engine_build
            else "props"
            if props.get("build_info")
            else "unknown",
            "url": f"{urlsplit(url).scheme}://{urlsplit(url).netloc}",
        },
        "model": {
            "name": model,
            "quant": quant or (quant_match.group(0) if quant_match else "unknown"),
            "quant_source": "cli" if quant else "props" if quant_match else "unknown",
            "path": model_path or "unknown",
        },
        "planner_mode": {"name": mode.name, "params": dict(mode.params), "sampling": base_sampling},
        "system_prompt_sha256": inputs.prompts["prompt_hash"],
        "captured_primary": _captured_summary(paths, inputs),
        "git_head": _git_head(),
    }
    if digest:
        fingerprint["digest_sha256"] = hashlib.sha256(digest.encode()).hexdigest()
    return fingerprint


def _identity(fingerprint: Mapping[str, object]) -> list[object]:
    out: list[object] = []
    for path in _IDENTITY_PATHS:
        cur: object = fingerprint
        for key in path:
            cur = cur.get(key) if isinstance(cur, Mapping) else None
        out.append(cur)
    return out


def _same_configuration(existing: Mapping[str, object], new: Mapping[str, object]) -> bool:
    """Compare two fingerprints. A value that the new one cannot report yet is not a difference."""
    return all(
        old == fresh or (fresh in (None, "unknown") and old not in (None, "unknown"))
        for old, fresh in zip(_identity(existing), _identity(new), strict=True)
    )


def ensure_compatible(paths: RunPaths, fingerprint: Mapping[str, object]) -> None:
    """Write the fingerprint, or check it against the one the tag already holds.

    Rows of two configurations must never share a tag. A second run of the same configuration may resume.

    Args:
        paths: The run paths.
        fingerprint: The fingerprint of the run about to start.

    Raises:
        SystemExit: If the tag holds the fingerprint of a different configuration.
    """
    if paths.fingerprint.exists():
        existing = json.loads(paths.fingerprint.read_text())
        if not _same_configuration(existing, fingerprint):
            raise SystemExit(
                f"tag {paths.tag!r} holds rows of a different configuration "
                f"({paths.fingerprint}); use a new --tag"
            )
        return
    paths.rows.mkdir(parents=True, exist_ok=True)
    paths.fingerprint.write_text(json.dumps(fingerprint, indent=2, sort_keys=True))


def fill_engine_build(paths: RunPaths, system_fingerprint: object) -> None:
    """Record the engine build that a reply reports, when the fingerprint has none.

    llama.cpp puts its build in the ``system_fingerprint`` of every reply chunk. The ``/props`` document
    that would also carry it is not always reachable through the proxy. A build that is already known,
    from ``/props`` or from ``--engine-build``, is never overwritten.

    Args:
        paths: The run paths.
        system_fingerprint: The ``system_fingerprint`` of a reply, or ``None``.
    """
    if not isinstance(system_fingerprint, str) or not system_fingerprint:
        return
    stored = json.loads(paths.fingerprint.read_text())
    engine = stored.get("engine", {})
    if engine.get("build") not in (None, "", "unknown"):
        return
    engine.update(build=system_fingerprint, build_source="reply:system_fingerprint")
    stored["engine"] = engine
    paths.fingerprint.write_text(json.dumps(stored, indent=2, sort_keys=True))


def build_cloud_fingerprint(
    target: CloudTarget, inputs: Inputs, paths: RunPaths, digest: str | None
) -> dict[str, object]:
    """Build the configuration fingerprint of a managed-deployment run.

    A managed API reports no engine build and no quant, so those two fields say so. The model name that a
    reply reports is kept apart, in ``engine.served_model``, and a change of it is a different
    configuration. It reads ``unknown`` until the first reply fills it.

    Args:
        target: The managed deployment and its mode.
        inputs: The run inputs.
        paths: The run paths.
        digest: The digest text of a digest run, or ``None``.

    Returns:
        The fingerprint. The credential is not in it.
    """
    from scripts.eval.fre1537.cloud import response_format_of  # lazy: cloud imports the client

    fingerprint: dict[str, object] = {
        "engine": {
            "name": target.provider,
            "build": MANAGED_BUILD,
            "build_source": "managed",
            "served_model": "unknown",
            "url": target.endpoint,
        },
        "model": {
            "name": target.model_id,
            "quant": MANAGED_QUANT,
            "quant_source": "managed",
            "path": "n/a",
        },
        "planner_mode": {"name": target.mode_name, "params": dict(target.declared)},
        "system_prompt_sha256": inputs.prompts["prompt_hash"],
        # FRE-1548: the schema is part of the request, so a change of it is a routing change.
        "response_format_sha256": hashlib.sha256(
            json.dumps(response_format_of(target), sort_keys=True).encode()
        ).hexdigest(),
        "captured_primary": _captured_summary(paths, inputs),
        "client": {"litellm": importlib.metadata.version("litellm")},
        "git_head": _git_head(),
    }
    if digest:
        fingerprint["digest_sha256"] = hashlib.sha256(digest.encode()).hexdigest()
    return fingerprint


def fill_served_model(paths: RunPaths, served_model: object) -> None:
    """Record the model name that a managed reply reports, when the fingerprint has none.

    A value that is already known is never overwritten.

    Args:
        paths: The run paths.
        served_model: The ``model`` of a reply, or ``None``.
    """
    if not isinstance(served_model, str) or not served_model:
        return
    stored = json.loads(paths.fingerprint.read_text())
    engine = stored.get("engine", {})
    if engine.get("served_model") not in (None, "", "unknown"):
        return
    engine["served_model"] = served_model
    stored["engine"] = engine
    paths.fingerprint.write_text(json.dumps(stored, indent=2, sort_keys=True))
