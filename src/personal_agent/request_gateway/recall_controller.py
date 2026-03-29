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
    # Explicit memory request
    r"|(?:remind\s+me\s+(?:what|which|about|of))"
    r"|(?:refresh\s+my\s+memory)"
    # Resumptive reference
    r"|(?:the\s+\w+\s+(?:we|I)\s+(?:discussed|mentioned|talked\s+about|decided\s+on|chose|picked))",
)

# Noun phrase extraction: simple heuristic — captures up to 3 words after a determiner.
_NOUN_PHRASE_RE = re.compile(
    r"(?:our|the|that|my)\s+([\w]+(?:\s+[\w]+){0,2})",
    re.IGNORECASE,
)

# Trailing stop words to strip from noun phrases (query artifacts)
_TRAILING_STOP_RE = re.compile(
    r"\s+(?:again|now|today|here|there|then|please|yet)$",
    re.IGNORECASE,
)


def run_recall_controller(
    intent: IntentResult,
    user_message: str,
    session_messages: Sequence[dict[str, str]],
    max_candidates: int = 3,
    max_scan_turns: int = 20,
) -> RecallResult | None:
    """Run the recall controller (Stage 4b).

    Args:
        intent: Stage 4 intent classification result.
        user_message: Current user message.
        session_messages: Conversation history (most recent last).
        max_candidates: Max session fact candidates to return.
        max_scan_turns: Max turns to scan in session history.

    Returns:
        RecallResult if reclassification occurred, None if passed through.
    """
    # Gate 1: Only CONVERSATIONAL enters
    if intent.task_type != TaskType.CONVERSATIONAL:
        logger.debug(
            "recall_controller_skipped",
            original_task_type=intent.task_type.value,
        )
        return None

    # Gate 2: Cue pattern match
    cue = _detect_recall_cues(user_message)
    if cue is None:
        return None

    logger.info(
        "recall_cue_detected",
        cue_pattern=cue,
        message_excerpt=user_message[:80],
    )

    # Gate 3: Noun phrase extraction + session fact scan
    noun_phrases = _extract_noun_phrases(user_message)
    if not noun_phrases:
        logger.info(
            "recall_cue_false_positive",
            cue_pattern=cue,
            reason="no_noun_phrase",
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
    )

    if not candidates:
        logger.info(
            "recall_cue_false_positive",
            cue_pattern=cue,
            reason="no_session_match",
        )
        return RecallResult(
            reclassified=False,
            original_task_type=TaskType.CONVERSATIONAL,
            trigger_cue=cue,
            candidates=[],
        )

    # Reclassify
    logger.info(
        "recall_reclassified",
        original_type="conversational",
        new_type="memory_recall",
        trigger_cue=cue,
        top_candidate_fact=candidates[0].fact[:100],
        confidence=0.85,
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
    matches = _NOUN_PHRASE_RE.findall(message)
    # Deduplicate and clean, stripping trailing stop words
    seen: set[str] = set()
    phrases: list[str] = []
    for m in matches:
        # Strip trailing stop words before lowercasing
        stripped = _TRAILING_STOP_RE.sub("", m).strip()
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
            if phrase.lower() in content.lower():
                # Extract the sentence containing the match
                sentences = re.split(r"[.!?\n]", content)
                matching_sentence = ""
                for s in sentences:
                    if phrase.lower() in s.lower():
                        matching_sentence = s.strip()
                        break

                if not matching_sentence:
                    matching_sentence = content[:200]

                # Score by recency (newer = higher)
                turn_index = total_turns - 1 - i
                recency_score = 1.0 - (i / max(total_turns, 1))

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
