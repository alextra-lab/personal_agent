"""FRE-1122 — the effective-probe manifest that binds the phases together.

The first draft ran each phase off the original YAML independently. That made two
acceptance criteria vacuous at once (Codex round 1, findings 2 and 6):

* preflight could replace an absent probe whose subject turned out to be present,
  record the substitution, and exit zero — while ``run`` went on to fire the
  **original** probe, because it re-read the YAML and never looked at preflight's
  output. The report then labelled a present subject absent.
* ``report`` read only the answers artifact, so an empty or stale one produced a
  clean "0 / 0" baseline and exited zero.

The manifest closes both. Preflight writes exactly one immutable record of what
the run *is* — the effective probes after replacement, the owner, the probe-set
hash, and whether ground truth held. Every later phase loads it, refuses on
mismatch, and stamps its own artifacts with its digest. A phase that cannot prove
it is operating on the same probes as the phase before it does not run.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import asdict, dataclass

from scripts.eval.fre1122_absence_probe.probes import Probe, ProbeSet

__all__ = ["Manifest", "ManifestError", "load_manifest", "write_manifest"]

MANIFEST_NAME = "effective_manifest.json"


class ManifestError(RuntimeError):
    """A phase cannot prove it is operating on the run the manifest describes."""


@dataclass(frozen=True)
class Manifest:
    """The immutable record of what a run is measuring.

    Attributes:
        probes: The effective probes, after any AC-1 replacement — the exact
            questions that will be, or were, fired.
        user_id: The owner the ground truth was established against. A run under
            a different identity measures a different corpus.
        probe_set_digest: Digest of the source probe-set file, so an edit between
            preflight and run is detectable.
        ground_truth_holds: Whether every probe's claimed status was evidenced.
            False blocks the run rather than warning about it.
        replacements: Absent probes swapped out, each with why.
        created_at: When preflight wrote this, ISO-8601.
    """

    probes: tuple[Probe, ...]
    user_id: str
    probe_set_digest: str
    ground_truth_holds: bool
    replacements: tuple[dict[str, str], ...]
    created_at: str

    @property
    def digest(self) -> str:
        """Stable digest of the effective probe list, for cross-phase binding.

        Returns:
            A hex digest over every field of every probe, in order.

        Note:
            Digesting only id and question left status, subject_terms, expected
            tokens and expected_source editable after preflight without
            invalidating existing artifacts (Codex round 2) — an answer set
            could be re-attributed to probes whose expected content had changed.
        """
        payload = json.dumps(
            [asdict(p) for p in self.probes], sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def present_probes(self) -> tuple[Probe, ...]:
        """The effective present half."""
        return tuple(p for p in self.probes if p.status == "present")

    @property
    def absent_probes(self) -> tuple[Probe, ...]:
        """The effective absent half."""
        return tuple(p for p in self.probes if p.status == "absent")


def file_digest(path: pathlib.Path) -> str:
    """Digest a file's bytes.

    Args:
        path: The file.

    Returns:
        Its hex SHA-256.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_manifest(
    root: pathlib.Path,
    *,
    probes: tuple[Probe, ...],
    user_id: str,
    probe_set_path: pathlib.Path,
    ground_truth_holds: bool,
    replacements: tuple[dict[str, str], ...],
    created_at: str,
) -> Manifest:
    """Write the effective manifest for this run.

    Args:
        root: The run's artifact root.
        probes: The effective probes, after replacement.
        user_id: The owner ground truth was established against.
        probe_set_path: The source YAML, digested into the manifest.
        ground_truth_holds: Whether every probe's status was evidenced.
        replacements: The AC-1 substitutions made, each with its reason.
        created_at: ISO-8601 timestamp.

    Returns:
        The manifest as written.
    """
    manifest = Manifest(
        probes=probes,
        user_id=user_id,
        probe_set_digest=file_digest(probe_set_path),
        ground_truth_holds=ground_truth_holds,
        replacements=replacements,
        created_at=created_at,
    )
    root.mkdir(parents=True, exist_ok=True)
    (root / MANIFEST_NAME).write_text(
        json.dumps(
            {
                "probes": [asdict(p) for p in manifest.probes],
                "user_id": manifest.user_id,
                "probe_set_digest": manifest.probe_set_digest,
                "ground_truth_holds": manifest.ground_truth_holds,
                "replacements": [dict(r) for r in manifest.replacements],
                "created_at": manifest.created_at,
                "digest": manifest.digest,
            },
            indent=2,
        )
    )
    return manifest


def load_manifest(
    root: pathlib.Path,
    *,
    probe_set: ProbeSet,
    probe_set_path: pathlib.Path,
    user_id: str,
    require_ground_truth: bool = True,
) -> Manifest:
    """Load the manifest and refuse unless it describes this exact run.

    Args:
        root: The run's artifact root.
        probe_set: The probe set this phase was invoked with, for a shape check.
        probe_set_path: The source YAML, re-digested and compared.
        user_id: The owner this phase was invoked with.
        require_ground_truth: When True, a manifest recording a failed preflight
            is refused. Postcheck sets this False so a failed run can still be
            cleaned up.

    Returns:
        The manifest.

    Raises:
        ManifestError: If it is missing, or describes a different probe set,
            owner, or a preflight that did not hold.
    """
    path = root / MANIFEST_NAME
    if not path.exists():
        raise ManifestError(
            f"no {MANIFEST_NAME} at {root} — run the preflight phase first; it is "
            "what establishes ground truth and fixes the effective probe list"
        )

    raw = json.loads(path.read_text())

    # Strict, not coercive. bool("false") is True, and a tuple field arriving as
    # a string would silently collapse to () — either would let a corrupt
    # manifest pass as a valid one (Codex round 2).
    holds = raw.get("ground_truth_holds")
    if not isinstance(holds, bool):
        raise ManifestError(
            f"ground_truth_holds must be a JSON boolean, got {holds!r} — a "
            "coerced value could turn a failed preflight into a passing one"
        )

    manifest = Manifest(
        probes=tuple(Probe(**{**p, **_tuplify(p)}) for p in raw["probes"]),
        user_id=raw["user_id"],
        probe_set_digest=raw["probe_set_digest"],
        ground_truth_holds=holds,
        replacements=tuple(raw.get("replacements") or []),
        created_at=raw["created_at"],
    )

    # Required, not optional. Enforcing only when present meant deleting the
    # field skipped verification entirely (Codex round 3).
    stored = raw.get("digest")
    if not stored:
        raise ManifestError(
            "the manifest carries no digest — it was not written by preflight, "
            "or the field was removed to evade the integrity check"
        )
    if stored != manifest.digest:
        raise ManifestError(
            "the manifest's recorded digest does not match its contents "
            f"({str(stored)[:12]} != {manifest.digest[:12]}) — it has been edited "
            "in place since preflight wrote it"
        )

    if manifest.user_id != user_id:
        raise ManifestError(
            f"manifest was built for owner {manifest.user_id}, this phase was "
            f"invoked with {user_id} — that is a different corpus"
        )

    current = file_digest(probe_set_path)
    if manifest.probe_set_digest != current:
        raise ManifestError(
            "the probe-set file changed after preflight "
            f"({manifest.probe_set_digest[:12]} -> {current[:12]}); re-run preflight "
            "so ground truth matches the probes that will be fired"
        )

    if require_ground_truth and not manifest.ground_truth_holds:
        raise ManifestError(
            "preflight recorded that ground truth did NOT hold for every probe; "
            "refusing to proceed — a probe whose status is wrong makes the "
            "baseline number meaningless"
        )

    if len(manifest.probes) != len(probe_set.probes):
        raise ManifestError(
            f"manifest holds {len(manifest.probes)} probes, the supplied set has "
            f"{len(probe_set.probes)} — they are not the same run"
        )

    return manifest


def _tuplify(raw: dict[str, object]) -> dict[str, tuple[str, ...]]:
    """Restore tuple-typed probe fields from their JSON list form.

    Args:
        raw: One probe's JSON mapping.

    Returns:
        The fields needing conversion back to tuples.

    Raises:
        ManifestError: If a tuple-typed field is present but not a JSON list.
    """
    out: dict[str, tuple[str, ...]] = {}
    for field_name in ("subject_terms", "expected_tokens"):
        value = raw.get(field_name)
        if value is None:
            out[field_name] = ()
            continue
        if not isinstance(value, list):
            raise ManifestError(
                f"{field_name} must be a JSON list, got {type(value).__name__} — "
                "silently collapsing it to empty would discard the probe's "
                "ground-truth terms"
            )
        out[field_name] = tuple(str(v) for v in value)
    return out
