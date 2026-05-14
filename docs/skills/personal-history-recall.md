---
name: personal-history-recall
description: Retrieve the connected user's own past turns within a time window via the recall_personal_history tool. Use only when the user refers to *their* history; for general questions, use search_memory.
when_to_use: When the user's phrasing scopes to themselves — 'we talked about', 'what did I ask', 'remind me what I said', 'last week', 'yesterday', 'days ago'. Not for general knowledge questions ('what do we know about X') — those stay on search_memory.
tools: [recall_personal_history]
nudge: "Match the user's scoping. 'We/I/my/us' → recall_personal_history. 'What do we know about X' → search_memory (shared graph)."
keywords:
  - what did we
  - what did I
  - we talked about
  - we discussed
  - did we
  - remind me what
  - last time we
  - my conversation
  - my history
  - I told you
  - I mentioned
  - I asked
  - last week
  - yesterday
  - earlier this week
  - days ago
---

# SKILL: personal-history-recall

> **Tier:** 1 — native tool
> **Tool:** `recall_personal_history`
> **ADR:** [ADR-0052 §Update 2026-05-14](../architecture_decisions/ADR-0052-seshat-owner-identity-primitive.md)

---

## What this skill does

Retrieve the **connected user's own past turns** within a time window. This is the explicit, opt-in narrowing of memory recall — the agent's default is the shared knowledge graph (`search_memory`), which surfaces what *anyone* has contributed. Use this skill only when the user's phrasing scopes to themselves.

---

## When to use vs `search_memory`

<when_to_use>
  Use recall_personal_history when the user scopes to themselves:
    - "we talked about …", "what did we discuss …"
    - "I asked", "I told you", "I mentioned"
    - "my conversation last week", "remind me what I said"

  Use search_memory (the default) when the user asks a general question:
    - "what do we know about X"
    - "tell me about the Acropolis"
    - "find conversations about travel planning"

  The shared graph is the default. Personal-history is an explicit narrowing.
</when_to_use>

---

## Worked examples

<example>
  User: What did we talk about last Tuesday?
  Today is Wednesday; "last Tuesday" = 8 days ago.
  Call: recall_personal_history(days_ago=8)
</example>

<example>
  User: Remind me what I told you about the Athens trip.
  "Remind me" — personal scope. Topic substring: "Athens". 30 days is a safe default.
  Call: recall_personal_history(days_ago=30, topic="Athens")
</example>

<anti_example>
  User: What do we know about the Acropolis?
  This is a general knowledge question — the agent should surface anyone's
  contributions, not just the connected user's. Use the shared graph.
  Call: search_memory(query_text="Acropolis")
  Do NOT call recall_personal_history — that would hide shared knowledge.
</anti_example>

---

## Time-phrase cheat sheet

| Phrase | `days_ago` |
|---|---|
| yesterday | 1 |
| earlier this week | 3 |
| last week | 7 |
| earlier this month | 14 |
| last month | 30 |
| last quarter | 90 |

For specific weekdays ("last Tuesday"), compute the offset from today. The LLM does the math; the tool only takes integer `days_ago`.

---

## Returned shape

```json
{
  "turns": [
    {
      "turn_id": "trace-abc123",
      "timestamp": "2026-05-12T18:30:00+00:00",
      "session_id": "sess-xyz",
      "user_message": "Let's plan a trip to Athens...",
      "summary": "discussed Athens itinerary",
      "entities": ["Athens", "Acropolis"]
    }
  ],
  "total": 1,
  "window_days": 7,
  "user_id": "..."
}
```

---

## Notes

- The tool fails loudly if `ctx.user_id` is missing — that is a bug after FRE-343, not a fallback condition.
- For purely topical recall ("what's a good Greek restaurant?"), prefer `search_memory` — it surfaces other users' contributions.
- The `topic` filter is a case-insensitive substring on `user_message`. It does not yet do semantic search; for fuzzy matches use `search_memory(query_text=..., recency_days=N)`.

See also: [search_memory tool](../skills/seshat-knowledge.md)
