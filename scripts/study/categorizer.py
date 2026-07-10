"""ADR-0114 D3 in-context ingest categorizer for the study sandbox (FRE-839).

An LLM reads the full conversation and, for the concepts already known to
be discussed in it (the frozen corpus's `Session-[:DISCUSSES]->Entity`
edge — not rediscovered here), proposes 1-3 associative categories per
concept with a confidence, based on how *this conversation* uses it
(encoding specificity, D3) — never a decoupled post-hoc pass.

Mirrors `second_brain/entity_extraction.py`'s established shape: one long
prompt (closed instructions + a worked example + the exact output schema
restated inline), `orjson`-style JSON parsing after fence-stripping,
Python-side normalization. Provenance (`model`/`prompt_version`/`seed`) is
stamped by Python after parsing — the prompt explicitly instructs the model
not to emit it, and any model-supplied provenance fields are ignored, never
trusted (mirrors `entity_extraction.py`'s `_build_provenance` split).

`seed` is recorded honestly as an integer run-identifier the caller
supplies — not a proven determinism guarantee. Neither `LiteLLMClient` nor
`LocalLLMClient` expose a native seed passthrough, and Anthropic's API has
no seed parameter; true reproducibility isn't achievable via the API today.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import structlog

from scripts.study.writer import ProposedMembership

log = structlog.get_logger(__name__)

CATEGORIZER_PROMPT_VERSION = "fre839-categorizer-v1"

_CATEGORIZER_SYSTEM_PROMPT = """\
You are categorizing concepts by how they are used in a specific conversation, \
for a research study into associative memory. Reason about the CONTEXT this \
conversation gives each concept — not a generic dictionary definition of it.
Your final output must be valid JSON only — no markdown fences, no explanation text."""

_CATEGORIZER_PROMPT_TEMPLATE = """\
Analyze this conversation. For each concept listed below, propose 1 to 3 \
associative categories describing what this conversation is really about \
with respect to that concept — a graded, context-derived category, not a \
generic classification.

RULES:
1. Categories are free-text, lowercase, 1-4 words (e.g. "adverse effect", \
"liver health", "health issue"). Do not invent a rigid taxonomy — propose \
whatever category best fits how THIS conversation uses the concept.
2. Each category gets a confidence 0.0-1.0 reflecting how strongly this \
conversation supports that category for that concept.
3. The SAME concept, mentioned in a DIFFERENT conversation, may deserve a \
DIFFERENT category — that is expected and correct, not an inconsistency to \
resolve here.
4. Do NOT emit `model`, `prompt_version`, `seed`, or any provenance field — \
the system stamps those, not you.
5. If a listed concept genuinely has no clear categorical fit in this \
conversation, omit it from your output rather than guessing.

WORKED EXAMPLE (illustrative only, not from this conversation): a \
conversation discussing a medication's side effects might propose, for the \
concept "Liver dysfunction": [{{"name": "adverse effect", "confidence": 0.81}}]. \
A different conversation reviewing test results might propose, for the SAME \
concept: [{{"name": "liver health", "confidence": 0.94}}].

CONCEPTS DISCUSSED IN THIS CONVERSATION:
{concepts_block}

CONVERSATION:
{conversation_text}

Return ONLY this JSON shape, no markdown fences, no other text:
{{"memberships": [{{"concept": "<concept name, exactly as listed above>", \
"categories": [{{"name": "<category>", "confidence": <float 0-1>}}, ...]}}, ...]}}
"""


def _build_categorizer_prompt(conversation_text: str, concepts: list[tuple[str, str]]) -> str:
    """Build the categorizer's user prompt for one conversation.

    Args:
        conversation_text: The full conversation transcript.
        concepts: `(name, kind)` pairs already known to be discussed in it.

    Returns:
        The rendered prompt string.
    """
    concepts_block = "\n".join(f"  - {name} ({kind})" for name, kind in concepts)
    return _CATEGORIZER_PROMPT_TEMPLATE.format(
        concepts_block=concepts_block, conversation_text=conversation_text
    )


def _strip_json_fences(content: str) -> str:
    """Strip markdown code fences and surrounding text (mirrors
    `entity_extraction.py`'s exact fence-stripping shape).
    """
    content = content.strip()
    if "```json" in content:
        start = content.find("```json") + 7
        end = content.find("```", start)
        content = content[start:end].strip()
    elif "```" in content:
        start = content.find("```") + 3
        end = content.find("```", start)
        content = content[start:end].strip()

    if not content.startswith("{"):
        brace_start = content.find("{")
        if brace_start != -1:
            content = content[brace_start:]
    return content


def get_categorizer_model_id() -> str:
    """The model id the categorizer dispatches to — a pure, fast, local
    config lookup (no network call), safe to call once for
    `AssertionProvenance` stamping and again inside `_call_llm` for the
    actual dispatch; both resolve the same deterministic config value.

    Reuses `entity_extraction`'s configured model (no new required
    `model_roles.yaml` entry) — only cost accounting is isolated, via the
    `study` cost-gate role in `_call_llm`.
    """
    from personal_agent.config import load_model_config, resolve_role_model_key

    entity_extraction_role = resolve_role_model_key("entity_extraction")
    model_def = load_model_config().models[entity_extraction_role]
    return str(model_def.id)


async def _call_llm(prompt: str, *, trace_id: str | None) -> dict[str, Any]:
    """Dispatch the categorizer prompt to the configured LLM, budget_role=`study`."""
    from personal_agent.config import load_model_config, resolve_role_model_key
    from personal_agent.llm_client import ModelRole
    from personal_agent.llm_client.litellm_client import LiteLLMClient
    from personal_agent.telemetry.trace import SystemTraceContext

    entity_extraction_role = resolve_role_model_key("entity_extraction")
    model_def = load_model_config().models[entity_extraction_role]

    client = LiteLLMClient(
        model_id=model_def.id,
        provider=model_def.provider or "anthropic",
        max_tokens=4096,
        budget_role="study",
    )
    response = await client.respond(
        role=ModelRole.SUB_AGENT,
        messages=[{"role": "user", "content": prompt}],
        system_prompt=_CATEGORIZER_SYSTEM_PROMPT,
        trace_ctx=SystemTraceContext.new("study_categorizer", session_id=trace_id),
    )
    return dict(response)


async def categorize_conversation(
    conversation_text: str,
    concepts: list[tuple[str, str]],
    *,
    seed: int,
    trace_id: str | None = None,
) -> list[ProposedMembership]:
    """Categorize every listed concept in-context for one conversation.

    Args:
        conversation_text: The full conversation transcript.
        concepts: `(name, kind)` pairs already known to be discussed in it
            (from the frozen corpus's `Session-[:DISCUSSES]->Entity` edge).
        seed: Caller-supplied run identifier, recorded honestly as
            provenance — not a proven determinism guarantee (see module
            docstring).
        trace_id: Optional trace identifier threaded into the LLM call's
            trace context.

    Returns:
        The proposed memberships (fail-open to `[]` on a malformed/empty
        response — a bad LLM response drops that episode's assertions, it
        does not crash the corpus run).
    """
    prompt = _build_categorizer_prompt(conversation_text, concepts)
    kind_by_name = {name: kind for name, kind in concepts}

    response = await _call_llm(prompt, trace_id=trace_id)
    content = response.get("content") or ""
    if not content:
        log.warning("study_categorizer_empty_response", trace_id=trace_id)
        return []

    try:
        parsed = json.loads(_strip_json_fences(content))
    except json.JSONDecodeError:
        log.warning("study_categorizer_malformed_json", trace_id=trace_id)
        return []

    try:
        return _parse_memberships(parsed, kind_by_name=kind_by_name)
    except (TypeError, ValueError, AttributeError):
        # Code-review finding (FRE-839): the surrounding `except
        # JSONDecodeError` above only catches syntactically invalid JSON —
        # syntactically VALID JSON with an unexpected shape (e.g.
        # `"categories": null`, a non-numeric `confidence`) previously
        # raised uncaught here, crashing the whole corpus run instead of
        # just dropping this one episode's assertions, contradicting this
        # function's own documented fail-open guarantee.
        log.warning("study_categorizer_unexpected_response_shape", trace_id=trace_id)
        return []


def _parse_memberships(
    parsed: dict[str, Any], *, kind_by_name: dict[str, str]
) -> list[ProposedMembership]:
    """Parse the categorizer's validated-JSON response into memberships.

    Isolated from `categorize_conversation` so the caller can wrap it in one
    fail-open `try/except` covering every "valid JSON, wrong shape" failure
    mode, not just a `JSONDecodeError`.
    """
    memberships: list[ProposedMembership] = []
    for entry in parsed.get("memberships") or []:
        concept_name = entry.get("concept")
        kind = kind_by_name.get(concept_name)
        if concept_name is None or kind is None:
            continue
        for category in entry.get("categories") or []:
            name = category.get("name")
            confidence = category.get("confidence")
            if not name or confidence is None:
                continue
            memberships.append(
                ProposedMembership(
                    concept_name=concept_name,
                    kind=kind,
                    category_name=str(name),
                    proposed_confidence=float(confidence),
                )
            )
    return memberships


def new_assertion_id() -> str:
    """A fresh id for a `MembershipAssertion` node."""
    return str(uuid4())
