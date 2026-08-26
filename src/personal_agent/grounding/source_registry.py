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

**D2 independence has two axes, and the paragraphs above are only the first (FRE-1303).**
*Invocation* independence asks whether the model's **arguments** composed the result, and the
parameter schema settles it. *Authorship* independence asks whether the agent **wrote the
content** the tool hands back, and the parameter schema says nothing about it:
``recall_personal_history(days_ago=7)`` is as typed as a call gets and returns the model's own
prose from last week. FRE-1302 shipped the premise that ``search_memory`` was the only member
needing more than a blanket ``EXTERNAL``, "every other member keeping the blanket ``EXTERNAL``
a typed, **model-independent** retrieval earns by default". That premise was false, and this
paragraph replaces it.

The second axis has its own finite boundary: **the store being read.** A typed retrieval that
reads back a store the agent itself writes into does not earn a blanket ``EXTERNAL``. That is
enumerable because the agent's *write* tools are enumerable (``tools/__init__.py``) — ``write``,
``bash``, ``run_python``, ``notes_write``, ``artifact_write``, ``artifact_draft``,
``create_linear_issue``, ``create_linear_project``, plus the turn and KG writers (episodic
capture, entity extraction). :data:`AGENT_WRITABLE_STORE_TOOLS` carries the resulting audit,
one verdict per member.

**The recall decision, recorded here because a merge commit is where reasoning goes to die.**
A ``Turn`` carries both the user's message and the agent's response, and FRE-1280 registers one
source per tool call, so there is no per-item entitlement to hand out. Three options were live:

*Chosen — most-restrictive, applied content-aware.* A call is only as entitled as its
least-entitled turn, decided from the fields that actually hold content rather than from the
tool's name — the same shape :func:`_search_memory_entitlement` applies across Claim rows, so
"aggregate to the least-entitled item" has one definition in this module rather than two. A
result carrying only ``user_message`` is ``USER_STATED``, which keeps the owner's own history
citable; anything carrying the agent's response, the generated summary, or extracted entity
names is ``AGENT_DERIVED``.

*Rejected — split the source.* Registering the user-message half and the assistant-response half
under separate identifiers is the correct end state, but :meth:`SourceRegistry.register_tool_result`
returns one :class:`ToolRegistration` holding a single ``source`` and the executor consumes a
single identifier. Making that a tuple is the per-item entitlement architecture FRE-1302
explicitly deferred, and it changes every consumer. The rule above is what is correct *until*
that lands, and is not thrown away by it.

*Rejected — drop* ``assistant_response`` *from the tool's output.* The tool exists to answer
"what did we discuss"; the agent's half of the exchange is most of that answer. Narrowing a
capability to protect a citation rule trades away more than it buys, and the chosen rule already
leaves the model able to *read* its prior reply without *citing* it.

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
from collections.abc import Callable, Mapping, Sequence
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


class Entitlement(StrEnum):
    """Whether a source is *entitled* to make the claim it contains (ADR-0138 D2).

    Verification confirms a claim was **copied from a source**; it never confirms the
    source was **entitled to make it**. Where the source is the system's own earlier
    confabulation, the loop closes and enforcement certifies the hallucination.

    Observed live, 2026-08-26 (session ``a1a496fa``), and recorded on FRE-1282: an ``Event``
    node reading *"Wednesday, July 1, 2026"* — a date the agent hallucinated in an earlier
    session, which entity extraction then wrote to the graph as a fact — was recalled and
    registered as an admissible memory source. Trace it through D3: resolution passes, it
    is in the registry; reachability passes vacuously, a memory node has no external
    referent; containment passes, because **the source is the false claim**. Three greens
    on an eight-week date error. The graph holds 42 date-shaped ``Event`` entities.

    This is D2's own independence rule one layer down. ``printf 'Paris has 9 million
    residents'`` cited as shell output is the model's words wearing a tool's identifier; a
    KG node written from an earlier agent utterance is the model's words wearing a memory
    node's identifier. The shapes are identical and so is the remedy.
    """

    EXTERNAL = "external"
    USER_STATED = "user_stated"
    AGENT_DERIVED = "agent_derived"


def _entitlement_of(item: Mapping[str, object]) -> Entitlement:
    """Classify one memory item's authorship, denying by default.

    ``asserted_by`` (FRE-1020, ADR-0098 D6) is the KG's existing co-authorship field:
    ``"user"`` where the owner stated it, ``"agent"`` where the assistant asserted or
    inferred it. It lives on ``Claim`` nodes and is **absent from most recall items
    today**, because nothing threads it from Neo4j through recall into the memory-context
    item this sees.

    Absence therefore resolves to :attr:`Entitlement.AGENT_DERIVED`, not to a benefit of
    the doubt. That is the same default-deny this module already applies to an
    unclassified tool, and it fails in the only safe direction: an owner-stated fact that
    is merely *unlabelled* loses its citation, where the alternative is the system
    certifying its own errors. Threading the field through recall is separate,
    sequenceable work and is ticketed.

    Args:
        item: A memory-context item.

    Returns:
        The entitlement this item's declared provenance supports.
    """
    return (
        Entitlement.USER_STATED if item.get("asserted_by") == "user" else Entitlement.AGENT_DERIVED
    )


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
        entitlement: Whether this source is entitled to make the claim it contains. See
            :class:`Entitlement`; the default denies, so a source registered by a path
            that has not thought about authorship under-admits rather than launders.
        referent: The external thing this source stands for — a URL — or None when it
            has none. D3(b) reachability is keyed on this field and never on the tool
            name: whether a source *has* something outside the turn to be reachable is a
            property of the source, and a verifier comparing tool-name strings would
            silently answer a different question every time the tool table changed.
            None is the common case and means D3(b) passes vacuously (D2: turn-local
            evidence, the user's words and memory nodes have no external referent — "the
            recorded result *is* the durable artifact").
    """

    model_config = ConfigDict(frozen=True)

    identifier: str
    kind: SourceKind
    label: str
    content: str
    origin: str
    referent: str | None = None
    entitlement: Entitlement = Entitlement.AGENT_DERIVED


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


REFERENT_ARGUMENTS: dict[str, str] = {
    "fetch_url": "url",
}
"""Tools that address exactly one external referent, and the parameter naming it.

The same parameter-schema boundary the rest of this module rests on, applied to a
different question. A tool listed here retrieves *one* identified external thing, so its
result stands for that thing and D3(b) has something to check. A tool whose parameters
address a query rather than a referent — ``web_search`` — returns a result *set* that was
itself retrieved this turn; under D2 that recorded set is the durable artifact, so it has
no external referent of its own and reachability is not-applicable.

**What that leaves open, recorded rather than discovered.** A search snippet naming a URL
the model never fetched is citable and its reachability is vacuous. Closing it needs
per-result referents out of the search tool, not a rule here; channelling grounding through
``fetch_url`` — which registers a real referent — is v1's answer, and it is the same answer
D2 already gives for ``curl``.
"""


def _referent_of(tool_name: str, arguments: Mapping[str, object]) -> str | None:
    """Return the single external thing this call retrieved, if it had one.

    Args:
        tool_name: The tool that ran.
        arguments: The model's arguments to it.

    Returns:
        The referent, or None when the tool addresses a query rather than a referent.
        The model *chose* the URL and under D2 it is not evidence — but it remains the
        correct address of what was fetched, which is what D3(b) checks.
    """
    parameter = REFERENT_ARGUMENTS.get(tool_name)
    if parameter is None:
        return None
    value = arguments.get(parameter)
    return value.strip() or None if isinstance(value, str) else None


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


_CLAIM_LIST_KEYS: tuple[str, ...] = ("claims", "claims_history")
"""``search_memory`` result keys carrying Claim rows (ADR-0126 D4/D5, FRE-1302).

Both are pull-only reads of the same ``:Claim`` nodes on the same tool call — ``claims`` is
always present (``memory/service.py::query_claims``); ``claims_history`` joins it only when
the caller passed ``include_history=True`` (``query_claims_history``). A Claim under either
key carries the same authorship gap, so both feed :func:`_search_memory_entitlement`.
"""


def _search_memory_entitlement(content: str) -> Entitlement:
    """Classify one ``search_memory`` tool result by its Claims' own authorship (FRE-1302).

    ``search_memory`` registers as **one** source per call (FRE-1280): matched turns,
    entities, and Claims share a single identifier and a single entitlement
    (``orchestrator/executor.py`` calls :meth:`SourceRegistry.register_tool_result` once per
    dispatched tool result). There is no per-item entitlement in this architecture, so a call
    is only as entitled as its least-entitled Claim — the aggregate is the most restrictive
    entitlement among every Claim row actually present, reusing :func:`_entitlement_of`
    (the same function :meth:`SourceRegistry.register_memory_item` calls, so "user-asserted"
    has exactly one definition) rather than re-deriving the rule here.

    Turns and entities carry no ``asserted_by`` at all today, so a call returning none of
    ``claims``/``claims_history`` keeps :attr:`Entitlement.EXTERNAL` — that gap is real but is
    this fix's sibling work (FRE-1299 covered the push path only), not this one's job.

    Fails to :attr:`Entitlement.AGENT_DERIVED`, never to :attr:`Entitlement.EXTERNAL`, on any
    shape this function does not fully understand — unparsable content, a non-object top
    level, a claim-bearing key holding something other than a list, or a list member that
    isn't itself a mapping. ``EXTERNAL`` is an *admitted* tier (:func:`verify_turn` rejects
    only ``AGENT_DERIVED``), so falling back to it on a malformed shape would readmit
    anything this parse couldn't account for — the same default-deny direction
    :func:`_entitlement_of` already documents for absent authorship. ``EXTERNAL`` is returned
    only when the content is a well-formed object whose claim-bearing keys are absent or hold
    an empty list.

    Args:
        content: The tool result exactly as registered — post argument-echo-strip, the same
            text the registered source's own ``content`` field holds, so this function and
            D3(c) containment reason about identical bytes.

    Returns:
        The entitlement this call's Claim rows support.
    """
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        return Entitlement.AGENT_DERIVED
    if not isinstance(parsed, dict):
        return Entitlement.AGENT_DERIVED

    claims: list[object] = []
    for key in _CLAIM_LIST_KEYS:
        if key not in parsed:
            continue
        value = parsed[key]
        if not isinstance(value, list):
            return Entitlement.AGENT_DERIVED
        claims.extend(value)

    if not claims:
        return Entitlement.EXTERNAL

    if all(
        isinstance(claim, Mapping) and _entitlement_of(claim) is Entitlement.USER_STATED
        for claim in claims
    ):
        return Entitlement.USER_STATED
    return Entitlement.AGENT_DERIVED


_AGENT_AUTHORED_TURN_FIELDS: frozenset[str] = frozenset(
    {"assistant_response", "summary", "entities"}
)
"""``recall_personal_history`` turn fields the agent authored (FRE-1303).

``assistant_response`` is the model's prior output verbatim; ``summary`` is generated; the
``entities`` names come from extraction (ADR-0098). ``user_message`` is deliberately absent:
it is the owner's own words, and keeping it citable is this fix's AC-2.
"""

_NEUTRAL_TURN_FIELDS: frozenset[str] = frozenset(
    {"turn_id", "timestamp", "session_id", "topic_matched", "user_message"}
)
"""``recall_personal_history`` turn fields that carry no agent authorship.

Addresses, a timestamp, a match flag, and the owner's own message — the rest of the dict
literal ``tools/personal_history.py`` builds.

Split out from :data:`_AGENT_AUTHORED_TURN_FIELDS` so the rule can work as an **allowlist**
rather than a denylist. A denylist classifies an unrecognised field as harmless, which is the
wrong default in a module whose whole posture is that an unaudited shape denies — and it is
the precise way FRE-1302's premise went stale: a field added to the tool a quarter from now
would silently carry unaudited content into ``USER_STATED``. Unreachable today, since the
executor builds every turn from a fixed dict literal; the point is that it stays unreachable
without anyone having to remember this rule exists.
"""


def _recall_personal_history_entitlement(content: str) -> Entitlement:
    """Classify one ``recall_personal_history`` result by whose words it carries (FRE-1303).

    The bug this closes: the tool returns ``assistant_response`` verbatim
    (``tools/personal_history.py:192``), so before this rule the model could retrieve
    something it said last week and cite it at :attr:`Entitlement.EXTERNAL` — the most-trusted
    tier the contract has, and an *admitted* one, since :func:`verify_turn` rejects only
    :attr:`Entitlement.AGENT_DERIVED`. That is the closed loop D2 exists to prevent, with the
    aggravation that the content never even passed through extraction and adjudication.

    **Value-sensitive, not presence-sensitive**, and the distinction is the whole rule. The
    executor emits all three of :data:`_AGENT_AUTHORED_TURN_FIELDS` on every turn, falling back
    to ``""`` / ``[]`` where the Turn has none, so a key-presence check would deny every
    production result and make the user-stated branch unreachable. Each field is tested for
    content instead.

    Aggregates to most-restrictive across the returned turns, for the reason
    :func:`_search_memory_entitlement` documents: FRE-1280 registers one source per tool call,
    so a call is only as entitled as its least-entitled item.

    An empty ``turns`` list denies rather than keeping ``EXTERNAL``. This is the one place the
    rule diverges from its ``search_memory`` sibling, and deliberately: ``turns`` is this tool's
    *only* payload, so an empty window carries no user-stated content to be entitled to, where
    ``search_memory`` with no Claims still returns matched turns and entities.

    Fails to :attr:`Entitlement.AGENT_DERIVED` on any shape it does not fully understand —
    unparsable content, a non-object top level, a ``turns`` value that is not a list, a member
    that is not a mapping, or **a turn carrying a field this rule has not audited**. The content
    is attacker-influenced (it is whatever the tool returned), so falling back to ``EXTERNAL``
    on a malformed shape would let a crafted result readmit itself at the most-trusted tier.
    The unknown-field clause is an allowlist rather than a denylist for the reason
    :data:`_NEUTRAL_TURN_FIELDS` gives: a denylist treats a field nobody has classified as
    harmless, which is how FRE-1302's premise went stale in the first place.

    Args:
        content: The tool result exactly as registered — post argument-echo-strip, the same text
            the registered source's own ``content`` field holds, so this function and D3(c)
            containment reason about identical bytes.

    Returns:
        The entitlement this call's turns support.
    """
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        return Entitlement.AGENT_DERIVED
    if not isinstance(parsed, dict):
        return Entitlement.AGENT_DERIVED

    turns = parsed.get("turns")
    if not isinstance(turns, list) or not turns:
        return Entitlement.AGENT_DERIVED

    for turn in turns:
        if not isinstance(turn, Mapping):
            return Entitlement.AGENT_DERIVED
        if not turn.keys() <= (_AGENT_AUTHORED_TURN_FIELDS | _NEUTRAL_TURN_FIELDS):
            return Entitlement.AGENT_DERIVED
        if any(turn.get(field) for field in _AGENT_AUTHORED_TURN_FIELDS):
            return Entitlement.AGENT_DERIVED

    return Entitlement.USER_STATED


def _get_location_entitlement(content: str) -> Entitlement:
    """Classify one ``get_location`` result by which resolver answered (FRE-1303).

    ``get_location`` takes ``session_notes`` — free text the *model* writes — and
    ``ExplicitLocationProvider`` extracts a city from it and returns it as ``location.city``
    (``tools/location.py:179, 237``). That is D2's ``printf 'Paris'`` shape wearing a typed
    parameter, and it is invisible to :func:`_strip_argument_echo`, which compares whole
    top-level values: the returned city is a *substring* of the argument, nested one level down.

    ``LocationResolution.source`` is a ``Literal["explicit", "client"]`` carried into the output
    through ``asdict``, so the split is exact rather than heuristic. ``"client"`` is the stored
    device-provided location — genuinely external, and the reason this is a content-aware rule
    instead of a flat denial. Anything else, including a shape this cannot read, denies.

    Args:
        content: The tool result exactly as registered.

    Returns:
        The entitlement this resolution supports.
    """
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        return Entitlement.AGENT_DERIVED
    if not isinstance(parsed, dict):
        return Entitlement.AGENT_DERIVED

    location = parsed.get("location")
    if not isinstance(location, Mapping):
        return Entitlement.AGENT_DERIVED
    return Entitlement.EXTERNAL if location.get("source") == "client" else Entitlement.AGENT_DERIVED


_CONTENT_AWARE_ENTITLEMENT: dict[str, Callable[[str], Entitlement]] = {
    "search_memory": _search_memory_entitlement,
    "recall_personal_history": _recall_personal_history_entitlement,
    "get_location": _get_location_entitlement,
}
"""Members whose *result* carries a field settling authorship, and the rule that reads it.

A tool belongs here rather than in :data:`AGENT_WRITABLE_STORE_TOOLS` only when its output
exposes something to split on — ``asserted_by`` on a Claim, which turn fields hold content,
which location resolver answered. Where the store is mixed but the tool exposes no author
field, most-restrictive is the only sound rule and the flat denial is correct.
"""


AGENT_WRITABLE_STORE_TOOLS: frozenset[str] = frozenset(
    {
        "notes_search",
        "expand_tool_result",
        "artifact_read",
        "artifact_list",
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
    }
)
"""Typed retrievals that read back a store the agent writes into (FRE-1303).

Still admissible retrievals — they register a source, so D4's terminal no-source statement can
still name what was read. What they lose is the unqualified ``EXTERNAL``.

**The audit AC-3 asks for, one verdict per member.** The question is not "is this a typed
retrieval?" but "can this return text the model itself authored?"

* ``notes_search`` — yes. ``notes_write`` is the store's only writer: durable scratch space for
  the agent (``tools/notes_tools.py``). No external-author half at all.
* ``artifact_list`` — yes. Queries ``type = 'artifact'`` only, so it lists exactly what
  ``artifact_write``/``artifact_draft`` produced. No external-author half.
* ``expand_tool_result`` — yes, and this denial is load-bearing rather than precautionary. It
  replays any digested result verbatim from R2, and the digest pipeline explicitly handles
  ``tool_name == "bash"`` (``orchestrator/tool_result_digest.py``), so
  :data:`ARBITRARY_CODE_TOOLS`' own excluded stdout is reachable through a typed tool one hop
  later.
* ``artifact_read`` — yes. Returns the inline content of agent-generated artifacts.
* ``find_linear_issues`` / ``list_linear_projects`` — yes. ``create_linear_issue`` and
  ``create_linear_project`` set ``title``/``description`` verbatim from model-authored arguments,
  and these tools read them straight back with no author field in the result.
* the ``mcp_*`` Linear reads — yes, **on the store, which is verifiable here, not on their
  schemas, which are not**. ``create_linear_issue`` writes to the same workspace they read, so
  the store is agent-writable whatever any connector returns. This repo holds only their registry
  membership and auto-discovered governance descriptions, not their executors, so whether any of
  them exposes an author field is unverified — recorded rather than assumed. Under
  most-restrictive that changes the remedy, not the verdict.

**Members audited and left on ``EXTERNAL``**, so the next reader does not have to re-derive it:
``web_search``, ``mcp_search``, ``fetch_url`` (external web, no agent write path); ``read_skill``
(repo files, not agent-writable at runtime); ``mcp_get_mappings``, ``mcp_get_shards``,
``mcp_list_indices`` (cluster structure, not document content); the three ``mcp_browser_*``
observation tools (a third-party page). ``search_memory``, ``recall_personal_history`` and
``get_location`` are content-aware instead — see :data:`_CONTENT_AWARE_ENTITLEMENT`.

**Two members are audited "yes, in principle" and deliberately left ``EXTERNAL``**, ticketed
rather than folded in because neither is fixable here (FRE-1305, FRE-1306). ``read``: the agent has a ``write`` tool,
but ``read`` is D2's designated channel for local state, the filesystem is overwhelmingly
authored outside the agent, and :meth:`SourceRegistry._taint` already closes the intra-turn
``write``→``read`` pair; the residual is *cross-session*, which a turn-scoped registry cannot
see. ``mcp_esql``: ES|QL's ``ROW a = "…"`` emits a model-authored literal with no index involved
— the same escape hatch ``find -printf`` represents for ``bash`` — but ADR-0138 D2 explicitly
blesses the shape ("a database query — the returned **rows** are a source"), so reclassifying it
contradicts the ADR's own illustration and needs an amendment rather than a bugfix.

**The accepted regression, stated rather than discovered later.** ``artifact_read`` also serves
``type = 'upload'`` rows, which carry ``created_by = 'user'`` — but ``created_by`` is in neither
its ``SELECT`` nor its output, so a genuine user upload is denied too. The Linear reads deny
owner-filed issues for the same reason. This is not a new tradeoff: :func:`_entitlement_of`
already documents it for Claims lacking ``asserted_by`` — an owner-stated fact that is merely
*unlabelled* loses its citation, because the alternative is the system certifying its own errors.
The remedy is also the same and is FRE-1299's shape, threading the author field through each
tool's output, which is separately sequenceable per store and is FRE-1304. Meanwhile D1's
``ATTRIBUTED_RESTATEMENT`` exemption means restating the owner's words never needed one of these
citations, and an expanded result's originating call registered its own source in the turn that
made it.
"""


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
            entitlement=Entitlement.USER_STATED,
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
        # "affect" is last: a stance/behavioural-stance item (ADR-0126 T1/T2) carries
        # only `target`/`affect` — no `description`, `summary`, `content` or `text` —
        # so without this key every such source registered with empty content (found
        # in FRE-1296 review). Harmless while nothing rendered the identifier; FRE-1296
        # makes it citable, and an uncontained citation can never pass D3(c) (FRE-1282).
        for key in ("description", "summary", "content", "text", "user_message", "affect"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                content = value.strip()
                break
        return self._register(
            kind=SourceKind.MEMORY,
            label=identity or "memory item",
            content=content,
            origin=identity,
            entitlement=_entitlement_of(item),
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

        # FRE-1303: EXTERNAL is what a retrieval earns when it is independent of the model on
        # *both* of D2's axes — the arguments did not compose the result (the parameter schema,
        # settled above by the policy table) *and* the agent did not author what the store hands
        # back. FRE-1302 shipped this branch treating search_memory as the sole exception; that
        # was wrong, and recall_personal_history is why — it returns assistant_response verbatim
        # and so held the most-trusted tier on the model's own prior words. A new typed retrieval
        # belongs in one of the two sets below unless neither axis touches it; the module
        # docstring and AGENT_WRITABLE_STORE_TOOLS carry the audit and the reasoning.
        rule = _CONTENT_AWARE_ENTITLEMENT.get(tool_name)
        if rule is not None:
            entitlement = rule(admissible)
        elif tool_name in AGENT_WRITABLE_STORE_TOOLS:
            entitlement = Entitlement.AGENT_DERIVED
        else:
            entitlement = Entitlement.EXTERNAL
        source = self._register(
            kind=kind,
            label=tool_name,
            content=admissible,
            origin=tool_name,
            entitlement=entitlement,
            referent=_referent_of(tool_name, arguments),
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
        entitlement: Entitlement,
        referent: str | None = None,
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
            entitlement: Whether this source is entitled to make the claim it holds.
            referent: The single external thing it stands for, or None.

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
            entitlement=entitlement,
            referent=referent,
        )
        self._sources.append(source)
        self._by_identifier[source.identifier] = source
        self._by_dedupe_key[dedupe_key] = source
        return source
