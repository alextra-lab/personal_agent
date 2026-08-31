"""Source derivation and containment association (ADR-0098 Amendment A · A4/A4b).

Which retrieved source justifies which extracted item, decided **in Python at write
time** while the retrieved content is still in hand. The graph stores the *result* of the
check, so no durable copy of page bytes is needed in Core to make provenance resolvable
later (D3).

Three properties this module exists to hold, each of which a plausible alternative loses:

* **The extractor never declares provenance.** ADR-0098 D6 / FRE-1020: a model permitted
  to declare its own provenance can mint the credential that makes its output
  authoritative. Every value here is derived from the captured turn.
* **Attribution is not verification.** Containment answers *"does this source mention this
  item"*, not *"does it support this assertion"*. A page mentioning SafeCart justifies
  where we learned of SafeCart — never the entity's stored ``description`` or ``type``.
  Anything stronger is ADR-0138's job, on the assertion, at read time.
* **The address is not retrieved content.** ``fetch_url`` echoes its own ``url`` argument
  in its result, so attributing against the raw result would make an entity named for the
  site ("SafeCart" ← ``safecart.com``) *contained* in a page that never mentions it. The
  echo is stripped before the check, reusing grounding's one definition of "the model's
  arguments returning".

The known false-negative class is recorded rather than discovered: lowercase, stylized and
stopword-like names (``npm``, ``iPhone``) fall to ``none`` under the ``CONTAINED``-only
rule. It is countable, not silent — the consolidator reports the rate — and narrowing it is
follow-on work, not a blocker (A4).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import orjson

from personal_agent.grounding.containment import ContainmentOutcome, check_containment
from personal_agent.grounding.source_registry import strip_argument_echo

if TYPE_CHECKING:  # pragma: no cover - typing only
    from personal_agent.tools.registry import ToolRegistry

CONTENT_HASH_SCOPE = "captured_output_stripped"
"""What :attr:`SourceRecord.content_hash` is computed over.

Stored on the node so the truncation limit stays visible rather than implied. The hash
certifies *the content the containment check actually saw* — the captured tool output with
the call's own arguments stripped. Hashing full retained bytes we never read would make
"the page moved" undetectable in exactly the truncation case while appearing to detect it.
"""


@dataclass(frozen=True)
class SourceRecord:
    """One external artifact a turn retrieved, as Core will store it.

    Two identities are deliberately separated (A4b). *Provenance-version identity* is
    :attr:`source_id`, derived from ``(referent, content_hash)``: the same URL re-fetched
    unchanged is the same Source, and a changed page mints a new one. *Corroborating-
    authority identity* is :attr:`authority`, the referent's origin — two versions of one
    page are **one** authority. Collapsing them would let a single page changing over time
    satisfy D6's requirement for a second distinct source: repetition wearing a new hash.

    Attributes:
        source_id: Provenance-version identity; the value relationships carry in
            ``source_ids`` and nodes reach by ``SOURCED_FROM``.
        referent: The address of the thing retrieved.
        authority: Corroborating-authority identity — the resolved host for an
            ``http(s)`` referent, else the referent itself.
        retrieved_at: When the turn retrieved it.
        content_hash: ``sha256`` over :attr:`content`.
        retained_pointer: Where the bytes are retained, in the Docs layer.
        content: The retrieved content, held in memory for the containment check only.
            Never written to Core — see :meth:`to_cypher_map`.
    """

    source_id: str
    referent: str
    authority: str
    retrieved_at: datetime
    content_hash: str
    retained_pointer: str
    content: str

    @classmethod
    def build(
        cls,
        *,
        referent: str,
        content: str,
        retrieved_at: datetime,
        retained_pointer: str,
    ) -> SourceRecord:
        """Derive a record's identities from the referent and the content checked.

        Args:
            referent: The address of the thing retrieved.
            content: The retrieved content, as the containment check will see it.
            retrieved_at: When the turn retrieved it.
            retained_pointer: Where the bytes are retained.

        Returns:
            The record, with both identities computed.
        """
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        source_id = hashlib.sha256(f"{referent}\x00{content_hash}".encode()).hexdigest()[:32]
        return cls(
            source_id=source_id,
            referent=referent,
            authority=_authority_of(referent),
            retrieved_at=retrieved_at,
            content_hash=content_hash,
            retained_pointer=retained_pointer,
            content=content,
        )

    def to_cypher_map(self) -> dict[str, str]:
        """Return the node properties, as driver-encodable primitives.

        :attr:`content` is deliberately absent: Core holds the small keyed pointer and the
        bytes live in the isolatable Docs layer, so no hot query traverses into it (D3).

        Returns:
            The ``:Source`` node's properties.
        """
        return {
            "source_id": self.source_id,
            "referent": self.referent,
            "authority": self.authority,
            "retrieved_at": self.retrieved_at.isoformat(),
            "content_hash": self.content_hash,
            "content_hash_scope": CONTENT_HASH_SCOPE,
            "retained_pointer": self.retained_pointer,
        }


def _authority_of(referent: str) -> str:
    """Return the referent's corroborating-authority identity (A4b).

    ``urlsplit`` raises on a malformed IPv6 literal (``http://[abc``), and the referent is
    a *model-chosen* URL reaching this from a captured turn. Letting that propagate would
    abort the whole consolidation pass over an unparseable address, losing every other
    item's provenance with it — so it degrades to the referent instead, which is the
    honest authority for a string we cannot resolve.

    Args:
        referent: The address of the thing retrieved.

    Returns:
        The lowercased host for an ``http(s)`` URL, else the referent unchanged — an
        ingested document identifies its own authority.
    """
    try:
        split = urlsplit(referent)
    except ValueError:
        return referent
    if split.scheme in ("http", "https") and split.hostname:
        return split.hostname.lower()
    return referent


def _render_content(output: object, arguments: Mapping[str, object]) -> str:
    """Render a captured tool result to the text the containment check will see.

    The call's own arguments are stripped first: a result field echoing an argument is the
    model's words returning, not retrieved content, and attributing against it is how an
    entity named for the fetched host acquires a source that never mentions it.

    Args:
        output: The captured ``tool_results[i]["output"]``.
        arguments: The model's arguments to that call.

    Returns:
        The content to hash and to check containment against.
    """
    text = output if isinstance(output, str) else orjson.dumps(output).decode()
    return strip_argument_echo(text, arguments)


def sources_from_tool_results(
    tool_results: Sequence[Mapping[str, object]],
    *,
    retrieved_at: datetime,
    capture_trace_id: str,
    tool_registry: ToolRegistry,
) -> list[SourceRecord]:
    """Derive the external artifacts a captured turn retrieved.

    The tool contract is the single source of referents (A2): a result becomes a
    ``:Source`` only when its tool declares ``referent_parameter`` on its own
    ``ToolDefinition`` and the call supplied a value for it. A tool addressing a query
    rather than a referent (``web_search``) contributes nothing — its result is a set
    retrieved this turn, with no external address to walk to.

    Args:
        tool_results: The capture's ``tool_results``.
        retrieved_at: The turn's timestamp. Tool results carry ``latency_ms`` but no
            per-call timestamp, so this is the turn's time, not each fetch's.
        capture_trace_id: The capture holding the retained bytes.
        tool_registry: Registry to read each tool's declaration from.

    Returns:
        One record per successful referent-declaring call, in capture order.
    """
    sources: list[SourceRecord] = []
    for index, result in enumerate(tool_results):
        if not result.get("success"):
            continue
        tool_name = result.get("tool_name")
        if not isinstance(tool_name, str):
            continue
        registered = tool_registry.get_tool(tool_name)
        if registered is None:
            continue
        referent_parameter = registered[0].referent_parameter
        if referent_parameter is None:
            continue
        arguments = result.get("arguments")
        if not isinstance(arguments, Mapping):
            continue
        referent = arguments.get(referent_parameter)
        if not isinstance(referent, str) or not referent.strip():
            continue
        content = _render_content(result.get("output"), arguments)
        if not content.strip():
            continue
        sources.append(
            SourceRecord.build(
                referent=referent.strip(),
                content=content,
                retrieved_at=retrieved_at,
                retained_pointer=f"capture://{capture_trace_id}#tool_results/{index}",
            )
        )
    return sources


def attribution_for_relationship(source_name: str, predicate: str, target_name: str) -> str:
    """Return a relationship's attribution string (A4).

    Args:
        source_name: The edge's source entity name.
        predicate: The relationship type, e.g. ``BASED_IN``.
        target_name: The edge's target entity name.

    Returns:
        The verbalization ``source-name predicate target-name``, with the predicate's
        underscores opened out so it tokenizes as words rather than one opaque symbol.
    """
    return f"{source_name} {predicate.replace('_', ' ').lower()} {target_name}"


def associate(attribution: str, sources: Sequence[SourceRecord]) -> list[str]:
    """Return the ids of the sources whose content contains this item (A4).

    ``CONTAINED`` **only**: ``ENTAILMENT_REQUIRED`` and ``UNVERIFIABLE`` create no
    reference, because an attribution that needed an entailment judgement is not the
    mechanical, model-independent link this rule exists to provide.

    Multiple matches are recorded rather than treated as ambiguity — provenance is
    append-only, so an item contained in two fetched pages legitimately carries both.

    Args:
        attribution: The item's attribution string — an entity's name, a claim's content,
            or a relationship's verbalization.
        sources: The candidate sources this turn retrieved.

    Returns:
        The matching ``source_id`` values, in source order. Empty when nothing contains
        the item, which is the honest outcome for a fact the agent produced rather than
        read.
    """
    if not attribution.strip():
        return []
    return [
        source.source_id
        for source in sources
        if check_containment(attribution, source.content).outcome is ContainmentOutcome.CONTAINED
    ]


__all__ = [
    "CONTENT_HASH_SCOPE",
    "SourceRecord",
    "associate",
    "attribution_for_relationship",
    "sources_from_tool_results",
]
