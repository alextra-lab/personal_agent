# src/personal_agent/request_gateway/recall_controller.py
"""Recall controller — Stage 4b post-classification refinement.

Detects implicit backward-reference cues in messages classified as
CONVERSATIONAL by Stage 4, corroborates against session history,
and reclassifies to MEMORY_RECALL with session fact evidence.

Three-gate design:
1. Task type gate: only CONVERSATIONAL classifications enter
2. Cue pattern gate: regex match for implicit backward-reference cues
3. Session fact gate: noun phrase extraction + session history scan

See: ADR-0037 (recall-controller)
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import structlog

from personal_agent.request_gateway.types import (
    IntentResult,
    RecallCandidate,
    RecallResult,
    TaskType,
)
from personal_agent.telemetry.compaction import get_dropped_entities

logger = structlog.get_logger(__name__)

# --- Recall cue patterns (ADR-0037 Decision 2) ---
_RECALL_CUE_PATTERNS: re.Pattern[str] = re.compile(
    r"(?i)"
    # Temporal back-reference with interrogative context
    r"(?:what\s+(?:was|were|is)\s+(?:our|the|that)\s+\w+\s+again)"
    r"|(?:(?:going|go)\s+back\s+(?:to\s+)?(?:the\s+)?(?:beginning|start|earlier))"
    r"|(?:(?:back\s+to|earlier)\s+(?:when|where|what)\s+)"
    r"|(?:at\s+the\s+(?:beginning|start)\s*[,\u2014\u2013\-])"
    # Possessive prior-decision
    r"|(?:what\s+(?:was|were|is)\s+(?:our|the)\s+(?:primary|main|original|first|chosen|selected|preferred))"
    r"|(?:what\s+did\s+(?:we|I)\s+(?:decide|pick|choose|settle|go\s+with|land\s+on))"
    r"|(?:what\s+[\w\s]{1,80}?\s+did\s+(?:we|I)\s+(?:decide|pick|choose|settle|go\s+with|land\s+on))"
    # Explicit memory request
    r"|(?:remind\s+me\s+(?:what|which|about|of))"
    r"|(?:refresh\s+my\s+memory)"
    # Resumptive reference
    r"|(?:the\s+\w+\s+(?:we|I)\s+(?:discussed|mentioned|talked\s+about|decided\s+on|chose|picked))"
    r"|(?:what\s+was\s+that\s+[\w\s]{1,80}?\s+(?:we|I)\s+(?:discussed|mentioned|talked\s+about))",
)

# Noun phrase extraction: simple heuristic — captures up to 3 words after a possessive/
# demonstrative determiner.
_NOUN_PHRASE_RE = re.compile(
    r"(?:our|the|that|my)\s+([\w]+(?:\s+[\w]+){0,2})",
    re.IGNORECASE,
)

# Interrogative noun phrases: "what/which + [non-auxiliary] noun(s)".
# Negative lookahead prevents matching "what was/is/are..." where no useful noun follows.
_INTERROG_NOUN_RE = re.compile(
    r"(?:what|which)\s+"
    r"(?!(?:was|were|is|are|did|does|do|have|had|will|would|could|should|if|when|that)\b)"
    r"([\w]+(?:\s+[\w]+)?)",
    re.IGNORECASE,
)

# Trailing stop words to strip from noun phrases (query artifacts)
_TRAILING_STOP_RE = re.compile(
    r"\s+\b(?:again|now|today|here|there|then|please|yet)\b$",
    re.IGNORECASE,
)

# Trailing verb phrases and pronouns to strip (e.g., "tool we discussed" → "tool",
# "caching system did" → "caching system").
# Uses \b and [^\n]* (no nested quantifiers) to prevent polynomial backtracking.
_TRAILING_VERB_RE = re.compile(
    r"\s+\b(?:we|i|you|they|he|she|did|does|was|were|is|are|have|had"
    r"|pick|picked|chose|choose|use|used|discuss|discussed|mention|mentioned"
    r"|decide|decided|do)\b[^\n]*$",
    re.IGNORECASE,
)


def run_recall_controller(
    intent: IntentResult,
    user_message: str,
    session_messages: Sequence[dict[str, str]],
    trace_id: str = "",
    max_candidates: int = 3,
    max_scan_turns: int = 20,
) -> RecallResult | None:
    """Run the recall controller (Stage 4b).

    Args:
        intent: Stage 4 intent classification result.
        user_message: Current user message.
        session_messages: Conversation history (most recent last).
        trace_id: Request trace identifier for telemetry correlation.
        max_candidates: Max session fact candidates to return.
        max_scan_turns: Max turns to scan in session history.

    Returns:
        RecallResult if reclassification occurred, None if passed through.
    """
    # Cue detection + telemetry before task-type gate: Stage 4 may already
    # classify as MEMORY_RECALL (regex overlap with _MEMORY_RECALL_PATTERNS), but
    # eval and operators still expect `recall_cue_detected` whenever the recall
    # cue regex matches (ADR-0037 observability).
    cue = _detect_recall_cues(user_message)
    if cue is not None:
        logger.info(
            "recall_cue_detected",
            cue_pattern=cue,
            message_excerpt=user_message[:80],
            trace_id=trace_id,
        )

    # Gate 1: Only CONVERSATIONAL enters reclassification
    if intent.task_type != TaskType.CONVERSATIONAL:
        logger.debug(
            "recall_controller_skipped",
            original_task_type=intent.task_type.value,
            had_cue_match=cue is not None,
            trace_id=trace_id,
        )
        return None

    # Gate 2: Cue pattern match (same as telemetry above; kept explicit)
    if cue is None:
        return None

    # Gate 3: Noun phrase extraction + session fact scan
    noun_phrases = _extract_noun_phrases(user_message)
    if not noun_phrases:
        logger.info(
            "recall_cue_false_positive",
            cue_pattern=cue,
            reason="no_noun_phrase",
            trace_id=trace_id,
        )
        return RecallResult(
            reclassified=False,
            original_task_type=TaskType.CONVERSATIONAL,
            trigger_cue=cue,
            candidates=[],
        )

    scan_messages = list(session_messages[-max_scan_turns:])
    candidates = _scan_session_facts(
        noun_phrases=noun_phrases,
        session_messages=scan_messages,
        max_candidates=max_candidates,
    )

    logger.info(
        "recall_session_scan",
        noun_phrases=noun_phrases,
        turns_scanned=len(scan_messages),
        candidates_found=len(candidates),
        trace_id=trace_id,
    )

    if not candidates:
        logger.info(
            "recall_cue_false_positive",
            cue_pattern=cue,
            reason="no_session_match",
            trace_id=trace_id,
        )
        return RecallResult(
            reclassified=False,
            original_task_type=TaskType.CONVERSATIONAL,
            trigger_cue=cue,
            candidates=[],
        )

    # D3: Compaction quality check — warn when a recalled noun phrase matches
    # an entity that was dropped by a recent compaction event.
    session_id_for_check = trace_id  # best available proxy when session_id not threaded here
    dropped = get_dropped_entities(session_id_for_check)
    if dropped:
        for np in noun_phrases:
            for dropped_entity in dropped:
                if np in dropped_entity.lower() or dropped_entity.lower() in np:
                    logger.warning(
                        "compaction_quality.poor",
                        entity_id=dropped_entity,
                        noun_phrase=np,
                        session_id=session_id_for_check,
                        trace_id=trace_id,
                    )

    # Reclassify
    logger.info(
        "recall_reclassified",
        original_type="conversational",
        new_type="memory_recall",
        trigger_cue=cue,
        top_candidate_fact=candidates[0].fact[:100],
        confidence=0.85,
        trace_id=trace_id,
    )

    return RecallResult(
        reclassified=True,
        original_task_type=TaskType.CONVERSATIONAL,
        trigger_cue=cue,
        candidates=candidates,
    )


def _detect_recall_cues(message: str) -> str | None:
    """Check if the message contains implicit backward-reference cues.

    Args:
        message: User message text.

    Returns:
        Matched cue string, or None if no cue detected.
    """
    match = _RECALL_CUE_PATTERNS.search(message)
    if match:
        return match.group(0).strip()
    return None


def _extract_noun_phrases(message: str) -> list[str]:
    """Extract target noun phrases from the user message.

    Uses simple heuristic: "our/the/that/my + noun phrase" (up to 3 words).
    Strips trailing query-artifact stop words (e.g., "again", "now").

    Args:
        message: User message text.

    Returns:
        List of extracted noun phrases (lowercase, deduplicated).
    """
    all_matches = _NOUN_PHRASE_RE.findall(message) + _INTERROG_NOUN_RE.findall(message)
    # Deduplicate and clean, stripping verb phrases and stop words
    seen: set[str] = set()
    phrases: list[str] = []
    for m in all_matches:
        # Strip trailing verb phrases (e.g., "tool we discussed" → "tool")
        stripped = _TRAILING_VERB_RE.sub("", m).strip()
        # Strip trailing query-artifact stop words
        stripped = _TRAILING_STOP_RE.sub("", stripped).strip()
        cleaned = stripped.lower()
        if cleaned and cleaned not in seen and len(cleaned) > 2:
            seen.add(cleaned)
            phrases.append(cleaned)
    return phrases


def _scan_session_facts(
    noun_phrases: list[str],
    session_messages: list[dict[str, str]],
    max_candidates: int = 3,
) -> list[RecallCandidate]:
    """Scan session history for facts matching the noun phrases.

    Args:
        noun_phrases: Target noun phrases to search for.
        session_messages: Conversation history to scan.
        max_candidates: Max candidates to return.

    Returns:
        List of RecallCandidate sorted by confidence (descending).
    """
    candidates: list[RecallCandidate] = []
    seen_facts: set[tuple[int, str]] = set()
    total_turns = len(session_messages)

    for i, msg in enumerate(reversed(session_messages)):
        content = msg.get("content", "")
        if not content:
            continue

        for phrase in noun_phrases:
            phrase_lower = phrase.lower()
            content_lower = content.lower()
            # Full phrase match first; fall back to individual significant words
            # for multi-word phrases (handles "caching system" matching "caching layer")
            phrase_found = phrase_lower in content_lower
            if not phrase_found and " " in phrase_lower:
                phrase_found = any(w in content_lower for w in phrase_lower.split() if len(w) > 3)
            if phrase_found:
                # Extract the sentence containing the match
                sentences = re.split(r"[.!?\n]", content)
                search_terms = [phrase_lower] + (
                    [w for w in phrase_lower.split() if len(w) > 3] if " " in phrase_lower else []
                )
                matching_sentence = ""
                for s in sentences:
                    s_lower = s.lower()
                    if any(t in s_lower for t in search_terms):
                        matching_sentence = s.strip()
                        break

                if not matching_sentence:
                    matching_sentence = content[:200]

                # Score by recency (newer = higher)
                turn_index = total_turns - 1 - i
                recency_score = 1.0 - (i / max(total_turns, 1))

                # D5 (ADR-0047): When an Entity object with KnowledgeWeight is
                # available for this fact, apply a -10 % confidence penalty for
                # low-confidence facts (weight.confidence < 0.4).
                # Example:
                #   if hasattr(entity, 'weight') and entity.weight.confidence < 0.4:
                #       recency_score *= 0.90
                # Entity objects are not threaded into this path today — the hook
                # is applied in memory/service.py relevance scoring instead.

                # Deduplicate by (turn, fact) to avoid wasting slots
                fact_key = (turn_index, matching_sentence)
                if fact_key in seen_facts:
                    continue
                seen_facts.add(fact_key)

                candidates.append(
                    RecallCandidate(
                        fact=matching_sentence,
                        source_turn=turn_index,
                        noun_phrase=phrase,
                        confidence=recency_score,
                    )
                )

                if len(candidates) >= max_candidates:
                    return sorted(candidates, key=lambda c: c.confidence, reverse=True)

    return sorted(candidates, key=lambda c: c.confidence, reverse=True)
