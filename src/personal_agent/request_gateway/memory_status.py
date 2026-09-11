"""What the model may honestly conclude about recalled memory (ADR-0148 D1/D2/D3, FRE-1476).

``memory_context`` alone cannot answer that question. An empty list is the same value for
four different facts: recall ran and found nothing, an arm failed, the token budget
discarded the context, and memory was never wired. This module holds the vocabulary that
separates them, the per-stage reports that feed it, and the one composition rule.

No single stage can compute the status (D1). The cause of a failed retrieval is known at
the arm for one stack frame, the budget drop is known at ``budget.py``, and a render drop
is known only at the renderer. Each reports what it did and the turn composes those
reports here.

The rendering half (FRE-1478, ADR-0148 D5) lives in ``orchestrator/executor.py``, which
renders :data:`MEMORY_STATE_LINES` and reports the renderer's own drops back into
:class:`RenderStageReport`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from personal_agent.captains_log.turn_evidence import MemoryItemKind, memory_item_identity


class MemoryStatus(StrEnum):
    """The four states of the recall layer, on one axis (ADR-0148 D2).

    The axis is what the model may honestly conclude. It is deliberately not what went
    wrong — that axis belongs in the turn-evidence record, where more than four values are
    useful and harmless.

    Attributes:
        POPULATED: Recall admitted items and at least one carries renderable content.
        NOTHING_RELEVANT: Every path ran to completion and admitted nothing carrying
            content. Licenses "I have no *usable* record of that", never "I have no
            record" — a name can match with an empty description (FRE-1115).
        WITHHELD: Recall admitted usable items and capacity or the renderer removed all of
            them. The one non-populated state where the system knows usable items existed.
        UNAVAILABLE: A path did not run to completion, or reported nothing at all.
    """

    POPULATED = "populated"
    NOTHING_RELEVANT = "nothing_relevant"
    WITHHELD = "withheld"
    UNAVAILABLE = "unavailable"


MEMORY_STATE_LINES: dict[MemoryStatus, str] = {
    MemoryStatus.NOTHING_RELEVANT: (
        "Memory: no usable record of this exists in memory for this turn."
    ),
    MemoryStatus.WITHHELD: (
        "Memory: records on this topic exist and could not be presented in this turn."
    ),
    MemoryStatus.UNAVAILABLE: ("Memory: records could not be reached for this turn."),
}
"""The one line each non-populated state renders (ADR-0148 D2/D5, FRE-1478).

``POPULATED`` has no entry — the rendered items speak for themselves. The other three
carry the ADR's own licensed wording: ``NOTHING_RELEVANT`` says "no *usable* record",
never "no record", because a name can match with an empty description (FRE-1115);
``WITHHELD`` says "could not be presented", not "could not fit", because a renderer's
unsupported-kind drop is not a capacity fact; ``UNAVAILABLE`` says "could not be
reached". This is a hint, not enforcement (D5) — the existing citation contract still
does that job, and this module adds none.
"""


class RecallOutcome(StrEnum):
    """Whether a producing path established what exists (ADR-0148 D1/D3).

    Attributes:
        NOT_REPORTED: The default. A path that says nothing has not established
            completeness, so it composes UNAVAILABLE — never NOTHING_RELEVANT, and never a
            status inferred from the item count (D3).
        COMPLETED: The path ran to completion. This is the only outcome that makes absence
            reachable, which is why it is not the default.
        FAILED: The path, or an arm beneath it, did not run to completion.
    """

    NOT_REPORTED = "not_reported"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class RecallStageReport:
    """What the turn's producing path did (ADR-0148 D1).

    Attributes:
        outcome: Whether the path ran to completion. Defaults to the weaker claim, the
            same trap ``RecallDiscardReport.population`` closes for completeness claims.
        cause: A short machine-readable cause when the outcome is FAILED, for the evidence
            record. Never rendered — the rendered vocabulary is the four states only.
    """

    outcome: RecallOutcome = RecallOutcome.NOT_REPORTED
    cause: str | None = None


@dataclass(frozen=True)
class RenderStageReport:
    """What the renderer emitted of what it was handed (ADR-0148 D1, FRE-1478).

    Populated by ``_render_memory_section_with_ids`` (``orchestrator/executor.py``) after
    it runs, replacing the mirror ``classify_recall_admission`` infers from the raw
    context with the fact of what actually rendered. It is here because the composition
    cannot resolve "every arm completed but the renderer dropped every item" without it,
    and that case is one of FRE-1476's own criteria.

    Attributes:
        ran: Whether the renderer reported at all. False means the status below is the
            composition of the earlier stages only.
        recall_emitted: How many **recall-derived** lines the renderer emitted. Scoped
            deliberately: the renderer returns one identity list that also holds standing
            behavioural stances (``executor.py``), and a total count would report a turn
            as populated when only the standing layer rendered.
        cause: A short machine-readable cause when the renderer dropped everything.
    """

    ran: bool = False
    recall_emitted: int = 0
    cause: str | None = None


@dataclass(frozen=True)
class RecallAdmission:
    """The recall layer's items, classified for the status rule (ADR-0148 D2).

    A blank-description entity is a name that matched with nothing to read (FRE-1115
    counts these at 18.7% of the corpus). It is **not** an admitted item: there is nothing
    for the reader to have received. Real content the renderer does not support **is**
    admitted, and its removal is WITHHELD. The two cases license different sentences.

    Attributes:
        with_content_renderable: Items carrying content, of a kind the renderer emits.
        with_content_unsupported_kind: Items carrying content, of a kind it does not.
        contentless: Items the recall layer produced that carry nothing to read.
    """

    with_content_renderable: int = 0
    with_content_unsupported_kind: int = 0
    contentless: int = 0

    @property
    def admitted(self) -> int:
        """Items that count as admitted: those carrying content, whatever their kind."""
        return self.with_content_renderable + self.with_content_unsupported_kind


@dataclass(frozen=True)
class MemoryStatusReport:
    """Every stage's report, and the status they compose (ADR-0148 D1/D2/D3).

    ``status`` is a property rather than a stored field so the composed value cannot drift
    from the reports it is composed of. A later stage rebuilds this report with its own
    field set and the status follows.

    Attributes:
        recall: What the producing path did.
        admission: What the recall layer admitted, classified.
        budget_dropped_recall_items: Whether Stage 7 discarded a context that held
            admitted items.
        render: What the renderer emitted. FRE-1478 populates it.
    """

    recall: RecallStageReport = RecallStageReport()
    admission: RecallAdmission = RecallAdmission()
    budget_dropped_recall_items: bool = False
    render: RenderStageReport = RenderStageReport()

    @property
    def status(self) -> MemoryStatus:
        """Compose the four states under ADR-0148 D2's total precedence.

        The order is fixed because the states are not naturally disjoint — a turn can
        satisfy more than one description at once:

        1. A path that did not run to completion, or reported nothing, outranks
           everything: the turn did not establish what exists. This covers D3's default,
           so an item count can never raise the status.
        2. Nothing admitted is NOTHING_RELEVANT. It sits above the drop rules because
           there was nothing for a later stage to withhold. ADR-0148 D2 declares an
           imprecision here — the budget drop reporting WITHHELD for a context of
           contentless items, where it names NOTHING_RELEVANT "the truer state". The
           imprecision does not arise: the ADR's out-of-scope item is classifying
           *renderability* before the budget stage, while this classifies *content*,
           which FRE-1476's own AC-6 requires anyway.
        3. A budget drop of admitted items is WITHHELD.
        4. A renderer that emitted no recall line is WITHHELD.
        5. Admitted items of unsupported kinds only is WITHHELD — the system holds them
           and the reader did not get them.
        6. Otherwise POPULATED.

        A renderer that has **not** run leaves rule 4 inert, so a turn with renderable
        content composes POPULATED before the render. That is required rather than
        overlooked: rule 4 discriminates only if not dropping everything yields something
        else, and treating an unreported renderer as a failure would compose WITHHELD on
        every turn. ``render.ran`` shows a reader whether the renderer has spoken.

        Returns:
            Exactly one MemoryStatus.
        """
        if self.recall.outcome is not RecallOutcome.COMPLETED:
            return MemoryStatus.UNAVAILABLE
        if self.admission.admitted == 0:
            return MemoryStatus.NOTHING_RELEVANT
        if self.budget_dropped_recall_items:
            return MemoryStatus.WITHHELD
        if self.render.ran and self.render.recall_emitted == 0:
            return MemoryStatus.WITHHELD
        if self.admission.with_content_renderable == 0:
            return MemoryStatus.WITHHELD
        return MemoryStatus.POPULATED


_RENDERABLE_KINDS: frozenset[MemoryItemKind] = frozenset(
    {MemoryItemKind.ENTITY, MemoryItemKind.EPISODE, MemoryItemKind.STANCE}
)
"""Kinds ``_render_memory_section_with_ids`` emits (``orchestrator/executor.py``).

SESSION is absent deliberately: not rendering session items is an explicit FRE-1010
non-goal, because two incompatible session shapes exist. A session item carrying real
content is therefore admitted, unsupported, and WITHHELD.

This classification runs at context assembly, before the renderer exists to speak for
itself, so it stays a mirror of the renderer's own filters. ``RenderStageReport``
(FRE-1478) carries the fact this mirror predicts, sourced from the render call itself.
"""


def _text(value: object) -> str:
    """The stripped text of a payload field, tolerating None and non-strings."""
    return value.strip() if isinstance(value, str) else ""


def _item_content(item: Mapping[str, Any], kind: MemoryItemKind) -> str:
    """The content one item carries, read the way the renderer reads it.

    Mirrors ``executor.py``'s own filters: an entity needs a description, an episode falls
    back from its summary to its user message, and a stance needs an affect.

    Args:
        item: One memory-context item.
        kind: Its kind, already resolved through ``memory_item_identity``.

    Returns:
        The item's content, stripped. Empty when the item carries nothing to read.
    """
    if kind is MemoryItemKind.ENTITY:
        return _text(item.get("description"))
    if kind is MemoryItemKind.EPISODE:
        return _text(item.get("summary")) or _text(item.get("user_message"))
    if kind is MemoryItemKind.SESSION:
        return _text(item.get("summary"))
    if kind is MemoryItemKind.STANCE:
        return _text(item.get("affect"))
    return ""


def classify_recall_admission(
    memory_context: Sequence[Any] | None,
) -> RecallAdmission:
    """Classify a turn's memory context into the recall layer's admitted items.

    Scoped to the recall layer (ADR-0148 D2). Two kinds are excluded, for different
    reasons:

    * **Standing behavioural stances** are outside the recall layer entirely.
      ``_inject_behavioural_stances`` runs on every authenticated turn and never reads
      ``memory_context`` for its targets, so counting them would read POPULATED on nearly
      every turn and make the whole design inert.
    * **A stance with no parent entity in this context** is not admitted. Enrichment
      inherits, it never qualifies (D4): a stance's target came from recall, so a stance
      whose parent is present is recall-derived content — including when that parent's own
      description is blank, which is the case where the renderer emits a stance line and
      no entity line. A parentless stance never raises the status.

    Args:
        memory_context: The turn's memory items after enrichment and stance injection, or
            None when nothing was recalled.

    Returns:
        The classified counts. All zero for None, an empty context, or a context holding
        nothing from the recall layer.
    """
    if not memory_context:
        return RecallAdmission()

    entity_names: set[str] = set()
    for item in memory_context:
        kind, identity = memory_item_identity(item)
        if kind is MemoryItemKind.ENTITY and identity:
            entity_names.add(identity)

    renderable = 0
    unsupported = 0
    contentless = 0
    for item in memory_context:
        if not isinstance(item, Mapping):
            continue
        kind, _ = memory_item_identity(item)
        if kind is MemoryItemKind.STANCE and _text(item.get("target")) not in entity_names:
            continue
        if kind not in {
            MemoryItemKind.ENTITY,
            MemoryItemKind.EPISODE,
            MemoryItemKind.SESSION,
            MemoryItemKind.STANCE,
        }:
            continue
        if not _item_content(item, kind):
            contentless += 1
        elif kind in _RENDERABLE_KINDS:
            renderable += 1
        else:
            unsupported += 1

    return RecallAdmission(
        with_content_renderable=renderable,
        with_content_unsupported_kind=unsupported,
        contentless=contentless,
    )
