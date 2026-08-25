r"""Per-turn source registry with stable identifiers (ADR-0138 D2 and D3(a), FRE-1280).

Every item retrieved during a turn gets an identifier that a citation can resolve
against. The registry is the thing that makes not-knowing *derivable* rather than
introspected: a claim with no resolvable identifier has no admissible provenance.

**Identifiers are content-bound, not turn-bound.** The first design used an ordinal plus a
digest of the turn id — ``S1@86cc08`` — and a plan review broke it with a verified
collision (``sha256("turn-1884")[:6] == sha256("turn-2537")[:6]``), which is exactly the
failure the ticket's AC-1 names: one identifier, two turns, different content. Widening the
digest moves the probability without changing what the identifier is bound to, so the
digest covers the content itself::

    S{ordinal}@{sha256(turn_id \0 ordinal \0 kind \0 content)[:IDENTIFIER_DIGEST_CHARS]}

An identifier that recurs across turns now recurs only where the content is byte-identical,
where resolving it is correct rather than a defect. It also closes the same-turn case: a
registry rebuilt on the D4 retry with a different first item mints a different ``S1@…``,
so a stale marker cannot silently re-point at new content.

**D2 independence attaches to the tool's parameter schema, not to its command line.** A
tool result is admissible only to the extent its content is not derived from the model's own
arguments to that call — otherwise ``printf 'Paris has 9 million residents'``, cited as
shell output, launders parametric knowledge into evidence in one round-trip.

Enumerating laundering *shapes* is unbounded, so a blocklist loses. An allowlist of shell
command heads loses too, which is less obvious and was the first design here: ``find``
reads the filesystem and looks like external state, yet::

    find . -maxdepth 0 -printf 'Paris has 9 million residents\\n'

emits a model-authored argument verbatim. The same escape hatch exists on nearly every
Unix tool — ``git log --pretty=format:``, ``stat --printf``, ``ps -o comm=``,
``rg --replace``, ``curl --write-out``, ``psql -c "SELECT '…'"`` — so denylisting flags per
head is the same unbounded chase one level down.

The finite boundary is the **parameter schema**. A tool whose arguments are typed,
enumerated parameters — ``read(path, offset, limit)``, ``web_search(query, …)`` — has no
surface through which the model can inject content into the result: the parameters select
or address, they do not compose output. A tool taking arbitrary model-authored code or a
command line is by construction a channel for the model's own words returning wearing a
tool's identifier, and no static analysis of its argument bounds that.

**Consequence, stated plainly: a ``curl`` run through ``bash`` is not citable in v1.**
Grounding is channelled through the typed retrieval tools instead
(``fetch_url`` for ``curl``, ``read`` for ``cat``). This is stricter than ADR-0138
D2's illustration, which uses ``curl`` as its example of an admissible fetch; it preserves
the principle that illustration teaches — the page is a source, the model-chosen URL is not
— while dropping an instrument that is not mechanically decidable. Source *quality*
(reputation, allowlisting) remains explicitly out of scope for v1 per D7.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from personal_agent.captains_log.turn_evidence import mark_truncated, memory_item_identity

IDENTIFIER_DIGEST_CHARS = 16
"""Hex characters of the content digest carried by every identifier.

Sixty-four bits over ``(turn_id, ordinal, kind, content)``. An earlier draft used 40, and
a plan review broke it by brute force: ``turn-726429``/``content-726429`` and
``turn-1435878``/``content-1435878`` share the digest prefix ``dc0757a42e``. Reproduced in
this session. Forty bits is reachable by search, so the guarantee below is stated as a
bound rather than as a structure.

**What the digest does and does not guarantee.** Two turns mint the same identifier only
on a 64-bit digest collision over differing inputs, which no accidental process reaches;
below that, an identifier recurring across turns implies byte-identical content, where
resolving it is correct rather than a defect. This is a probabilistic bound on a
non-adversarial input — the model does not choose ``turn_id`` — not a structural
impossibility, and it inherits one system invariant: **``trace_id`` must be unique per
turn**, since a reused turn id with a recurring ordinal and content would legitimately
re-mint the same identifier.

Widening changes no consumer — the citation marker pattern in
:mod:`personal_agent.grounding.citations` is built from this constant.
"""

MAX_SOURCE_CONTENT_CHARS = 50_000
"""Bound on registered content, marked rather than silent (ADR-0125 D5).

Generous, because a truncated source is a source that can no longer support a claim it
genuinely contains — a false rejection under D3(c), whose rate FRE-1282 must bound.
"""

_MIN_ECHO_CHARS = 3
"""Shortest argument value treated as a candidate echo.

Value-equality exclusion is bounded to text because prose is the laundering channel D2
addresses. Without the bound, ``max_results=10`` and a returned ``result_count=10`` are
equal values sharing no provenance, and stripping the count would corrupt retrieved
content on every search.
"""


class SourceKind(StrEnum):
    """One member per admissible source in ADR-0138 D2, and no more.

    The model's weights are not on the list, which is the whole decision.
    """

    MEMORY = "memory"
    TOOL = "tool"
    DOCUMENTATION = "documentation"
    USER = "user"


class Admissibility(StrEnum):
    """Whether a tool result yielded a citable source, and why not when it did not.

    The reasons are kept distinct because they call for different remedies: an
    ``UNCLASSIFIED_TOOL`` is one line on the policy table, while a
    ``MODEL_AUTHORED_INVOCATION`` is the contract working as designed.
    """

    ADMISSIBLE = "admissible"
    MODEL_AUTHORED_INVOCATION = "model_authored_invocation"
    DERIVED_FROM_TURN_WRITE = "derived_from_turn_write"
    UNCLASSIFIED_TOOL = "unclassified_tool"
    NO_CONTENT = "no_content"


class RegisteredSource(BaseModel):
    """One item retrieved this turn, with the identifier a citation resolves to.

    Attributes:
        identifier: ``S{ordinal}@{digest}`` — the token the model emits in a citation
            marker. Stable within a turn, content-bound across turns.
        kind: Which D2 source kind this is.
        label: Short human-facing descriptor for the rendered source list and for
            telemetry — an entity name, a URL, a tool name. Never the content.
        content: The admissible content, with any model-authored portion already
            excluded. Bounded by :data:`MAX_SOURCE_CONTENT_CHARS`.
        origin: Where it came from — the tool name, or the memory item's identity.
    """

    model_config = ConfigDict(frozen=True)

    identifier: str
    kind: SourceKind
    label: str
    content: str
    origin: str


class ExcludedArgument(BaseModel):
    """One model-authored argument, recorded as inadmissible.

    Recorded rather than merely dropped: D2's rule is that the model's own arguments are
    not evidence, and a reader of the turn record needs to see *what* was withheld to
    tell a correct exclusion from an over-eager one.

    Attributes:
        name: The parameter name.
        value: Its value, rendered as text.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    value: str


class ToolRegistration(BaseModel):
    """The outcome of offering one tool result to the registry.

    Attributes:
        source: The registered source, or None when nothing was admissible.
        admissibility: Whether a source was registered, and why not when it was not.
        excluded_arguments: The model-authored arguments, never part of ``source.content``.
        reason: One line of prose naming the rule that fired, for telemetry and for the
            reader of a turn that could not cite what it retrieved.
    """

    model_config = ConfigDict(frozen=True)

    source: RegisteredSource | None
    admissibility: Admissibility
    excluded_arguments: tuple[ExcludedArgument, ...] = ()
    reason: str = ""


# ── The policy table ────────────────────────────────────────────────────────────────
#
# Membership is the decision. Everything absent from all three sets is inadmissible by
# default, so a tool added to the system next quarter under-admits rather than launders.

DOCUMENTATION_TOOLS: frozenset[str] = frozenset(
    {
        "get_library_docs",
        "mcp_search_documentation",
        "mcp_get_document",
    }
)
"""D2 item 3 — documentation retrieved this turn (context7 and equivalents)."""

TYPED_RETRIEVAL_TOOLS: frozenset[str] = frozenset(
    {
        # Web and search
        "web_search",
        "mcp_search",
        "fetch_url",
        # Local state, addressed by typed parameters
        "read",
        "read_skill",
        "get_location",
        "artifact_read",
        "artifact_list",
        "expand_tool_result",
        "notes_search",
        # Memory and history
        "search_memory",
        "recall_personal_history",
        # Elasticsearch reads
        "mcp_esql",
        "mcp_get_mappings",
        "mcp_get_shards",
        "mcp_list_indices",
        # Linear reads
        "find_linear_issues",
        "list_linear_projects",
        "mcp_get_attachment",
        "mcp_get_issue",
        "mcp_get_issue_status",
        "mcp_get_milestone",
        "mcp_get_project",
        "mcp_get_team",
        "mcp_get_user",
        "mcp_list_comments",
        "mcp_list_cycles",
        "mcp_list_documents",
        "mcp_list_issue_labels",
        "mcp_list_issue_statuses",
        "mcp_list_issues",
        "mcp_list_milestones",
        "mcp_list_project_labels",
        "mcp_list_projects",
        "mcp_list_teams",
        "mcp_list_users",
        # Browser observation (not browser control, which composes rather than reads)
        "mcp_browser_snapshot",
        "mcp_browser_console_messages",
        "mcp_browser_network_requests",
    }
)
"""D2 item 2 — tool and web results whose parameters address rather than compose.

``perplexity_*`` and ``mcp_research`` are deliberately **absent**. A first draft admitted
them on the reading that D2's independence rule concerns the *caller's* arguments
returning, which a model-backed search does not do. A plan review pointed out that a typed
``query`` parameter carrying a prompt to another model is not distinguishable, by schema
shape alone, from a proxy for generation — and D2's actual decision is that parametric
knowledge is never a source. Another model's parameters are still parameters. Default-deny
therefore keeps them out until the question is decided on its own evidence.
"""

ARBITRARY_CODE_TOOLS: frozenset[str] = frozenset(
    {
        "bash",
        "run_python",
        "mcp_browser_evaluate",
        "mcp_browser_run_code",
        "mcp_sequentialthinking",
        "perplexity_query",
        "mcp_perplexity_ask",
        "mcp_perplexity_reason",
        "mcp_perplexity_research",
        "mcp_research",
    }
)
"""Tools whose output is a function of model-authored input.

Two shapes, one rule: arbitrary code or a command line (``bash``, ``run_python``, the
browser evaluate tools), and a model-authored prompt handed to another generator
(``perplexity_*``, ``mcp_research``, ``mcp_sequentialthinking``). Named explicitly even
though the default already denies them, because "this is structurally a laundering
channel" and "nobody has classified this yet" call for different remedies.
"""


def _digest(turn_id: str, ordinal: int, kind: SourceKind, content: str) -> str:
    """Return the content-bound digest carried by an identifier.

    Args:
        turn_id: This turn's trace identifier.
        ordinal: The source's 1-based position in the registry.
        kind: The source kind.
        content: The admissible content.

    Returns:
        The leading :data:`IDENTIFIER_DIGEST_CHARS` hex characters of the SHA-256 over all
        four values, NUL-separated so no concatenation of one field can imitate another.
    """
    payload = "\0".join((turn_id, str(ordinal), kind.value, content))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:IDENTIFIER_DIGEST_CHARS]


def _argument_values(arguments: Mapping[str, object]) -> tuple[object, ...]:
    """Return the argument values eligible for echo comparison.

    Only text carries prose, and prose is what D2's independence rule protects against,
    so short and non-text values are excluded — see :data:`_MIN_ECHO_CHARS`.

    Args:
        arguments: The model's arguments to the call.

    Returns:
        The eligible values: long-enough strings, and sequences of them.
    """
    eligible: list[object] = []
    for value in arguments.values():
        if isinstance(value, str) and len(value) >= _MIN_ECHO_CHARS:
            eligible.append(value)
        elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
            members = [m for m in value if isinstance(m, str) and len(m) >= _MIN_ECHO_CHARS]
            if members and len(members) == len(list(value)):
                eligible.append(list(value))
    return tuple(eligible)


def _strip_argument_echo(content: str, arguments: Mapping[str, object]) -> str:
    """Remove top-level result fields that are the call's own arguments returning.

    Driven by **value** comparison against the arguments mapping, never by a hardcoded
    field name: ``web_search`` returns the ``categories`` argument under the field name
    ``categories_used``, so a name-keyed rule would miss it, and a rule keyed to one
    tool's field names would not generalise to the next tool at all.

    Args:
        content: The tool result, as the executor recorded it.
        arguments: The model's arguments to the call.

    Returns:
        The content with echoing top-level fields removed. Returned unchanged when the
        result is not a JSON object — an unstructured result has no field to strip, and
        the arguments are recorded separately as inadmissible either way.
    """
    eligible = _argument_values(arguments)
    if not eligible:
        return content

    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        return content
    if not isinstance(parsed, dict):
        return content

    kept = {key: value for key, value in parsed.items() if value not in eligible}
    if len(kept) == len(parsed):
        return content
    return json.dumps(kept)


class SourceRegistry:
    """The sources one turn retrieved, and the identifiers citations resolve against.

    Turn-scoped by construction: one registry per :class:`ExecutionContext`, so D3(a)'s
    "present in *this turn's* retrieved source set" is a property of the object rather
    than a check someone must remember to perform.

    Not thread-safe, and deliberately so — a turn's tool calls are gathered concurrently
    but recorded sequentially in the executor's phase 3, which is the only place that
    registers tool results.
    """

    def __init__(self, turn_id: str) -> None:
        """Create an empty registry for one turn.

        Args:
            turn_id: This turn's trace identifier. Feeds the identifier digest, so two
                turns cannot mint the same identifier for different content.
        """
        self._turn_id = turn_id
        self._sources: list[RegisteredSource] = []
        self._by_identifier: dict[str, RegisteredSource] = {}
        self._by_dedupe_key: dict[tuple[SourceKind, str, str], RegisteredSource] = {}
        self._tainted: set[str] = set()

    @property
    def turn_id(self) -> str:
        """The trace identifier of the turn this registry belongs to."""
        return self._turn_id

    def sources(self) -> tuple[RegisteredSource, ...]:
        """Return every registered source, in registration order."""
        return tuple(self._sources)

    def resolve(self, identifier: str) -> RegisteredSource | None:
        """Resolve a citation identifier against this turn's registry (D3(a)).

        Membership, never a syntactic check: an identifier that merely *looks* well
        formed, including one carrying this turn's own digest shape, does not resolve
        unless a source was actually registered under it.

        Args:
            identifier: The identifier from a citation marker, without brackets.

        Returns:
            The source, or None when this turn registered nothing under that identifier.
        """
        return self._by_identifier.get(identifier)

    def register_user_message(self, message: str) -> RegisteredSource | None:
        """Register the user's own words as a source (D2 item 4).

        Args:
            message: This turn's user message.

        Returns:
            The registered source, or None when the message is empty.
        """
        text = message.strip()
        if not text:
            return None
        return self._register(
            kind=SourceKind.USER,
            label="user message",
            content=text,
            origin="user_message",
        )

    def register_memory_item(self, item: Mapping[str, object]) -> RegisteredSource:
        """Register one admitted memory-context item (D2 item 1).

        Identity comes from
        :func:`personal_agent.captains_log.turn_evidence.memory_item_identity`, the single
        definition the ADR-0125 evidence contract already uses, so the registry and the
        evidence record cannot disagree about what an item is called.

        An item carrying no usable text is still registered rather than dropped: AC-1
        requires every retrieved item to appear, and a source with empty content simply
        never satisfies the containment check FRE-1282 adds.

        Args:
            item: A memory-context item, in the shape ``memory/proactive.py`` emits.

        Returns:
            The registered source.
        """
        _, identity = memory_item_identity(item)
        content = ""
        for key in ("description", "summary", "content", "text", "user_message"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                content = value.strip()
                break
        return self._register(
            kind=SourceKind.MEMORY,
            label=identity or "memory item",
            content=content,
            origin=identity,
        )

    def register_tool_result(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        content: str,
        success: bool = True,
    ) -> ToolRegistration:
        """Offer one tool result to the registry, subject to D2's independence rule.

        Args:
            tool_name: The tool that ran.
            arguments: The model's own arguments to the call. Never admissible content.
            content: The result as recorded by the executor.
            success: Whether the call succeeded. A failed call retrieved nothing.

        Returns:
            The outcome, naming the source when one was registered and the rule that
            fired when none was.
        """
        excluded = tuple(
            ExcludedArgument(name=name, value=str(value)) for name, value in arguments.items()
        )

        if tool_name in ARBITRARY_CODE_TOOLS:
            self._taint(arguments)
            return ToolRegistration(
                source=None,
                admissibility=Admissibility.MODEL_AUTHORED_INVOCATION,
                excluded_arguments=excluded,
                reason=(
                    f"{tool_name} takes arbitrary model-authored code or a command line, so its "
                    "output is not independent of the model's own words (ADR-0138 D2)"
                ),
            )

        if tool_name in DOCUMENTATION_TOOLS:
            kind = SourceKind.DOCUMENTATION
        elif tool_name in TYPED_RETRIEVAL_TOOLS:
            kind = SourceKind.TOOL
        else:
            self._taint(arguments)
            return ToolRegistration(
                source=None,
                admissibility=Admissibility.UNCLASSIFIED_TOOL,
                excluded_arguments=excluded,
                reason=(
                    f"{tool_name} is not on the admissible-retrieval table; default-deny "
                    "(ADR-0138 D2)"
                ),
            )

        if self._reads_tainted(arguments):
            return ToolRegistration(
                source=None,
                admissibility=Admissibility.DERIVED_FROM_TURN_WRITE,
                excluded_arguments=excluded,
                reason=(
                    f"{tool_name} addresses state an inadmissible call wrote earlier this turn, "
                    "so its content is the model's own words returning (ADR-0138 D2)"
                ),
            )

        admissible = _strip_argument_echo(content, arguments) if success else ""
        if not admissible.strip():
            return ToolRegistration(
                source=None,
                admissibility=Admissibility.NO_CONTENT,
                excluded_arguments=excluded,
                reason=f"{tool_name} returned no content to cite",
            )

        source = self._register(
            kind=kind,
            label=tool_name,
            content=admissible,
            origin=tool_name,
        )
        return ToolRegistration(
            source=source,
            admissibility=Admissibility.ADMISSIBLE,
            excluded_arguments=excluded,
            reason="",
        )

    def _taint(self, arguments: Mapping[str, object]) -> None:
        """Record an inadmissible call's argument values as turn-tainted.

        Closes the two-call laundering shape, which neither the policy table nor the
        argument-exclusion rule catches because each call is admissible-looking in
        isolation::

            write(path="/tmp/x", content="Paris has 9 million residents")   # no source
            read(path="/tmp/x")                                             # ← would be one

        The write registers nothing, so a first draft concluded there was nothing left to
        close; a plan review pointed out that the *read* is the admissible half. Tainting
        the write's own argument values makes the pair visible without needing a
        per-tool table of which parameter names are paths.

        Args:
            arguments: The inadmissible call's arguments.
        """
        for value in arguments.values():
            if isinstance(value, str) and len(value) >= _MIN_ECHO_CHARS:
                self._tainted.add(value.strip())

    def _reads_tainted(self, arguments: Mapping[str, object]) -> bool:
        """Whether this call addresses state an inadmissible call wrote this turn.

        Exact value match, deliberately: a looser rule would deny an unrelated read whose
        path merely resembles a written one, and a false denial costs a legitimate
        citation. The residual is a write and a read that name the same target
        differently — an absolute path against a relative one — which needs the tool layer
        to report its writes rather than the registry to guess them.

        Args:
            arguments: The candidate read's arguments.

        Returns:
            True when any argument value is turn-tainted.
        """
        return any(
            isinstance(value, str) and value.strip() in self._tainted
            for value in arguments.values()
        )

    def _register(
        self,
        *,
        kind: SourceKind,
        label: str,
        content: str,
        origin: str,
    ) -> RegisteredSource:
        """Mint or reuse the identifier for one source.

        Reuse is keyed on ``(kind, origin, content)`` so re-registering the same item —
        which the D4 retry loop does by construction — returns the existing entry rather
        than minting a second ordinal for the same thing.

        Args:
            kind: The source kind.
            label: Short descriptor for rendering and telemetry.
            content: The admissible content.
            origin: The tool name or memory identity it came from.

        Returns:
            The registered source, new or existing.
        """
        bounded = mark_truncated(content, MAX_SOURCE_CONTENT_CHARS)
        dedupe_key = (kind, origin, bounded)
        existing = self._by_dedupe_key.get(dedupe_key)
        if existing is not None:
            return existing

        ordinal = len(self._sources) + 1
        source = RegisteredSource(
            identifier=f"S{ordinal}@{_digest(self._turn_id, ordinal, kind, bounded)}",
            kind=kind,
            label=label,
            content=bounded,
            origin=origin,
        )
        self._sources.append(source)
        self._by_identifier[source.identifier] = source
        self._by_dedupe_key[dedupe_key] = source
        return source
