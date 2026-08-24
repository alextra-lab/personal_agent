---
name: web-search
description: Search the live web via web_search when a question needs a fact about the real world — a specific brand, product, shop, price, current name, or availability — that cannot be answered reliably from training data alone. Complements search_memory (the shared graph) and recall_personal_history (the user's own history); use this when the answer isn't in either.
when_to_use: >
  When the user asks you to recommend, identify, compare, price, or locate a specific
  real-world brand, product, person, organisation, or shop — even when nothing about the
  question is time-sensitive. Also for factual claims about the world you are not certain
  of from training alone ("is X high in Y", "does X cause Y"). Not for questions already
  answered by search_memory or recall_personal_history, and not for pure reasoning/math
  that needs no outside fact.
tools: [web_search]
nudge: "A question about the world, not about this conversation or the user's own history, goes to web_search — not search_memory. Naming a brand or product in your answer without a tool result behind it is a guess, not an answer."
keywords:
  - which brand
  - where can i buy
  - where to buy
  - is it still
  - is x still
  - what is the best
  - what's the best
  - which is better
  - recommend a
  - is it true that
  - is it high in
  - does it cause
---

# SKILL: web-search

> **Tier:** 1 — native tool
> **Tool:** `web_search`
> **ADR:** [ADR-0034](../architecture_decisions/ADR-0034-searxng-self-hosted-web-search.md) (self-hosted SearXNG) · [ADR-0138](../architecture_decisions/ADR-0138-the-model-may-generate-but-may-not-assert.md) (grounding contract this skill implements one leg of)

---

## What this skill does

Search the live web for a fact about the real world that you cannot answer reliably from
training data alone — a specific brand, product, shop, price, current name, or availability.
This is the outward-facing counterpart to `search_memory` (the shared graph) and
`recall_personal_history` (the user's own history): those two answer "what has this user or
this system already been told"; `web_search` answers "what is actually true about the world
right now."

---

## When to use vs `search_memory` / `recall_personal_history`

<when_to_use>
  Use web_search when the question needs a specific real-world fact:
    - "which brand of X should I buy", "where can I buy X", "is X still available"
    - "what's the best X for Y" (a live recommendation, not a stored preference)
    - a factual claim you are not certain of ("is X high in Y", "does X cause Y")

  Use search_memory when the question is about what's already known in the shared graph:
    - "what do we know about X", "have we discussed X before"

  Use recall_personal_history when the user scopes to their own history:
    - "what did I ask you last week"

  Naming a specific brand, product, or shop in your answer without a tool result behind it
  is a guess, not an answer — even when the question names an entity itself. The question
  containing a name does not mean you already know the answer; it means you know what to
  search for.
</when_to_use>

---

## Worked examples

<example>
  User: Which brand of tinned tuna should I buy in a French supermarket?
  No stored preference exists, and no training-data snapshot can be current on French
  supermarket stock. Call: web_search(query="best tinned tuna brand France supermarket", categories="general")
</example>

<example>
  User: Is Aldi Süd still selling their reusable produce bags in Germany?
  Availability claims decay — search rather than assert from a training-data snapshot.
  Call: web_search(query="Aldi Süd reusable produce bags Germany 2026", categories="general")
</example>

<anti_example>
  User: What's 15% of 240?
  Pure arithmetic — no outside fact needed. Do NOT call web_search; answer directly (36).
</anti_example>

<anti_example>
  User: What do we know about the Acropolis?
  A shared-graph recall question, not a live-web question.
  Call: search_memory(query_text="Acropolis") — do NOT call web_search here.
</anti_example>

See also: [personal-history-recall](personal-history-recall.md), [seshat-knowledge](seshat-knowledge.md)
