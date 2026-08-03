"""FRE-1122 — the probe schema, and the load-time rules that keep it honest.

A probe's ground truth is known **by construction**: the present half targets
facts confirmed in the store before the run, the absent half targets subjects
confirmed to be in neither the graph nor the message history. That construction
is what removed the judge from this design (see the FRE-1063 decision record) —
but only if the construction rules are actually enforced.

They are enforced here, at load. A probe set that violates AC-2's stored-row
requirement or AC-7's personal-scoping rule does not load, so it cannot reach a
run and produce a number that silently means something else.

**AC-7 is applied to both halves, not only the absent one.** The criterion
mandates it for absent probes, where a publicly knowable subject would be
answered from training and scored as confabulation-avoidance it did not earn.
The same argument holds on the present half in mirror image: a correct answer to
a publicly knowable present probe evidences the model's world knowledge, not
recall from the store. Requiring the rationale on both is what makes
``ASSERTED_CORRECT`` mean what the report says it means.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

import yaml
from scripts.eval.fre1122_absence_probe.classify import ProbeStatus

__all__ = ["Probe", "ProbeSet", "ProbeSetError", "load_probe_set", "validate_run_shape"]

_REQUIRED_HALF_SIZE = 10


class ProbeSetError(ValueError):
    """A probe set violates a construction rule an acceptance criterion depends on."""


@dataclass(frozen=True)
class Probe:
    """One probe with its construction-time ground truth.

    Attributes:
        probe_id: Stable identifier. AC-6 requires the baseline and the second
            run to name the same identifiers, so this is the join key of the
            whole before-and-after comparison.
        status: Whether the subject is in the store. Known by construction and
            evidenced by the AC-1/AC-2 queries, never assumed.
        question: The question put to the system, verbatim.
        subject_terms: Terms the ground-truth queries search for, in the graph
            and in the message history. Also what AC-3 re-queries after the run
            and after cleanup.
        personal_scope_rationale: Why this subject is scoped to the owner and
            unobtainable by any route other than the store (AC-7).
        expected_tokens: Text a correct answer must reproduce, quoted from the
            stored row. Present probes only — empty on the absent half, where
            no correct answer exists.
        expected_source: Identifier of the stored row holding the fact (AC-2).
            Present probes only.
    """

    probe_id: str
    status: ProbeStatus
    question: str
    subject_terms: tuple[str, ...]
    personal_scope_rationale: str
    expected_tokens: tuple[str, ...] = ()
    expected_source: str | None = None


@dataclass(frozen=True)
class ProbeSet:
    """A loaded, validated probe set plus its pre-registered replacements.

    Attributes:
        probes: The probes for this run, in file order.
        absent_pool: Pre-registered absent probes, built by the same rules, used
            to replace a probe whose absence query returns rows (AC-1) and to
            supply a fresh absent half if AC-3 finds cleanup does not restore
            the corpus (AC-6's fallback branch).
    """

    probes: tuple[Probe, ...]
    absent_pool: tuple[Probe, ...] = field(default_factory=tuple)

    @property
    def present_probes(self) -> tuple[Probe, ...]:
        """The present half, in file order."""
        return tuple(p for p in self.probes if p.status == "present")

    @property
    def absent_probes(self) -> tuple[Probe, ...]:
        """The absent half, in file order."""
        return tuple(p for p in self.probes if p.status == "absent")


def _require(condition: bool, message: str) -> None:
    """Raise :class:`ProbeSetError` when a construction rule is violated.

    Args:
        condition: The rule, as a boolean that must hold.
        message: What was violated, naming the offending probe and field.

    Raises:
        ProbeSetError: When ``condition`` is false.
    """
    if not condition:
        raise ProbeSetError(message)


def _parse_probe(raw: object, *, where: str) -> Probe:
    """Build and validate one probe from its YAML mapping.

    Args:
        raw: The mapping as loaded from YAML.
        where: ``"probes"`` or ``"absent_pool"``, for error messages.

    Returns:
        The validated probe.

    Raises:
        ProbeSetError: If any construction rule is violated.
    """
    _require(isinstance(raw, dict), f"{where}: each entry must be a mapping, got {type(raw)}")
    assert isinstance(raw, dict)  # narrowed by _require; keeps mypy honest

    probe_id = str(raw.get("probe_id", "")).strip()
    _require(bool(probe_id), f"{where}: every probe needs a non-empty probe_id")

    status = raw.get("status")
    _require(
        status in ("present", "absent"),
        f"{probe_id}: status must be 'present' or 'absent', got {status!r}",
    )
    assert status in ("present", "absent")

    question = str(raw.get("question", "")).strip()
    _require(bool(question), f"{probe_id}: question must be non-empty")

    subject_terms = tuple(str(t).strip() for t in raw.get("subject_terms", ()) if str(t).strip())
    _require(
        bool(subject_terms),
        f"{probe_id}: subject_terms must be non-empty — AC-1 and AC-3 have no "
        "query to produce evidence with otherwise",
    )

    rationale = str(raw.get("personal_scope_rationale", "")).strip()
    _require(
        bool(rationale),
        f"{probe_id}: personal_scope_rationale must be non-empty (AC-7) — a "
        "publicly knowable subject is answered from the model's weights, so the "
        "outcome would score knowledge rather than recall",
    )

    expected_tokens = tuple(
        str(t).strip() for t in raw.get("expected_tokens", ()) if str(t).strip()
    )
    raw_source = raw.get("expected_source")
    expected_source = str(raw_source).strip() if raw_source is not None else None

    if status == "present":
        _require(
            bool(expected_tokens),
            f"{probe_id}: a present probe must carry expected_tokens — the text "
            "a correct answer must reproduce, quoted from the stored row (AC-2)",
        )
        _require(
            bool(expected_source),
            f"{probe_id}: a present probe must name its expected_source — the "
            "specific stored row holding the fact, by identifier (AC-2)",
        )
    else:
        _require(
            not expected_tokens,
            f"{probe_id}: an absent probe must not carry expected_tokens — its "
            "subject is not in the store, so no correct answer exists",
        )
        _require(
            expected_source is None,
            f"{probe_id}: an absent probe must not name an expected_source",
        )

    return Probe(
        probe_id=probe_id,
        status=status,
        question=question,
        subject_terms=subject_terms,
        personal_scope_rationale=rationale,
        expected_tokens=expected_tokens,
        expected_source=expected_source,
    )


def load_probe_set(path: pathlib.Path) -> ProbeSet:
    """Load and validate a probe set from YAML.

    Args:
        path: Path to the probe-set YAML, with ``probes`` and optionally
            ``absent_pool`` top-level keys.

    Returns:
        The validated probe set.

    Raises:
        ProbeSetError: If the file is malformed or any construction rule an
            acceptance criterion depends on is violated.
    """
    document = yaml.safe_load(path.read_text())
    _require(isinstance(document, dict), f"{path}: top level must be a mapping")
    assert isinstance(document, dict)

    probes = tuple(_parse_probe(r, where="probes") for r in document.get("probes", ()))
    pool = tuple(_parse_probe(r, where="absent_pool") for r in document.get("absent_pool", ()))

    _require(
        all(p.status == "absent" for p in pool),
        "absent_pool: may contain absent probes only — it exists to replace a "
        "probe whose absence query returned rows",
    )

    seen: set[str] = set()
    for probe in (*probes, *pool):
        _require(
            probe.probe_id not in seen,
            f"duplicate probe_id {probe.probe_id!r} — AC-6 joins the baseline "
            "and the second run on these, so they must be unique",
        )
        seen.add(probe.probe_id)

    return ProbeSet(probes=probes, absent_pool=pool)


def validate_run_shape(probe_set: ProbeSet) -> None:
    """Check the set is the ten-and-ten shape a reportable run requires.

    Kept separate from :func:`load_probe_set` so partial sets stay loadable
    while they are being authored and their ground truth verified; the runner
    calls this before firing any turn.

    Args:
        probe_set: The loaded set.

    Raises:
        ProbeSetError: If either half is not exactly ten probes.
    """
    present, absent = len(probe_set.present_probes), len(probe_set.absent_probes)
    _require(
        present == _REQUIRED_HALF_SIZE and absent == _REQUIRED_HALF_SIZE,
        f"a reportable run needs ten present and ten absent probes; "
        f"this set has {present} present and {absent} absent",
    )
