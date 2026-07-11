"""Shared frozen-artifact JSON writer for the ADR-0114 eval artifacts (FRE-841).

Mirrors `export_snapshot.py`'s `build_manifest`/`compute_content_hash` shape
so the corpus manifest and the two eval artifacts (AC-2 hard negatives, AC-4
abstract-cue gold) are all hashed and timestamped the same way — a reader can
independently verify a frozen artifact wasn't edited after it was committed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def compute_content_hash(payload: dict[str, Any]) -> str:
    """Sha256 over *payload*, canonicalized, excluding the `content_hash` key.

    Excluding `content_hash` itself avoids the chicken-and-egg problem of the
    hash needing to cover its own value; every other field (including
    `generated_at`) is covered.
    """
    canonical = {k: v for k, v in payload.items() if k != "content_hash"}
    serialized = json.dumps(canonical, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def freeze_json_artifact(
    payload: dict[str, Any], path: Path, *, generated_at: datetime
) -> dict[str, Any]:
    """Stamp *payload* with `generated_at`/`content_hash` and write it to *path*.

    Args:
        payload: The artifact body (not yet stamped).
        path: Destination file. Parent directories are created if missing.
        generated_at: The timestamp to stamp — caller-supplied so the
            function stays a pure transformation of its inputs (this repo's
            workflow scripts forbid `datetime.now()` at call time; ordinary
            scripts like this one may call it, but threading it through the
            parameter keeps this helper trivially unit-testable either way).

    Returns:
        The stamped payload (`generated_at` + `content_hash` added), the same
        dict that was written to *path*.
    """
    stamped = {**payload, "generated_at": generated_at.astimezone(timezone.utc).isoformat()}
    stamped["content_hash"] = compute_content_hash(stamped)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stamped, indent=2, sort_keys=True))

    return stamped
