---
name: get_location
description: Resolve the user's current location (coordinates + timezone) from stored device data or an explicit session mention. Use proactively for any location-aware query.
when_to_use: When the user asks about anything location-dependent — nearby places, restaurants, weather, timezone, local time, commute, travel, events near them, or says "near me", "around here", "local", "in my city". Also call it when you need timezone context for scheduling or date/time questions.
tools: [get_location]
nudge: "Call get_location first — don't ask the user to tell you where they are. If consent is off, tell them they can enable it in the PWA location toggle."
keywords:
  - near me
  - nearby
  - around here
  - in my city
  - local
  - where am I
  - my location
  - my timezone
  - what time is it here
  - restaurants near
  - weather here
  - my area
  - close to me
  - in my area
  - where I am
  - local time
  - local events
  - commute
---

# SKILL: get_location

> **Tier:** 1 — native tool
> **Tool:** `get_location`
> **Gate:** operator `AGENT_LOCATION_ENABLED=true` + per-user consent toggle in PWA
> **Spec:** FRE-230 · `docs/architecture_decisions/` (no standalone ADR; privacy model in `service/app.py` + `tools/location.py`)

---

## What this skill does

Resolves the user's current location from two sources, in priority order:

1. **Explicit session mention** — scans `session_notes` for phrases like "I'm in Lisbon today" and returns the named city.
2. **Stored device coordinates** — reads the `CURRENTLY_AT` location the user's PWA last pushed (lat/lng + IANA timezone), set when the user enables the consent toggle.

Returns coordinates, timezone, and source. City/country fields are `null` today (reverse-geocoding not yet wired); lat/lng can be used directly with search tools.

**Both gates must be on** — if the operator gate is off or the user hasn't given consent, the tool returns a clear reason code, not an error. Tell the user they can enable it in the PWA settings drawer.

---

## When to use

- User asks about **nearby places**: restaurants, cafés, shops, parks, hospitals
- User asks about **local weather** or **local time / timezone**
- User asks about **local events**, commute times, traffic
- User says **"near me"**, **"around here"**, **"in my area"**, **"locally"**
- You need **timezone context** to answer a scheduling or datetime question correctly
- User asks **"where am I?"** or **"do you know my location?"**

**Call this first** — don't ask the user to tell you where they are. The tool handles the consent check; if consent is off, tell them about the PWA toggle.

---

## Worked examples

<example>
  User: What are some good restaurants near me?
  Call: get_location(session_notes=None)
  Then: use the returned lat/lng with a search tool or answer based on the region.
</example>

<example>
  User: What time is it in my timezone right now?
  Call: get_location(session_notes=None)
  Use: returned timezone field (IANA, e.g. "Europe/Paris") to compute local time.
</example>

<example>
  User: I'm in Lisbon today — what's worth seeing?
  Call: get_location(session_notes="I'm in Lisbon today")
  The explicit provider will parse "Lisbon" without needing stored coordinates.
</example>

<anti_example>
  User: Any good spots near me?
  Do NOT ask: "Where are you located?"
  DO call get_location first. If consent is off, say:
  "I don't have your location yet — you can enable it in the PWA settings drawer."
</anti_example>

---

## Returned shape

```json
{
  "resolved": true,
  "location": {
    "city": null,
    "country": null,
    "latitude": 43.9308,
    "longitude": 5.7367,
    "timezone": "Europe/Paris",
    "source": "client",
    "precise": true
  },
  "latency_ms": 12.4
}
```

When unresolved:
```json
{ "resolved": false, "reason": "consent_not_given" }
{ "resolved": false, "reason": "no_location_stored" }
{ "resolved": false, "reason": "no_user" }
```

---

## Consent and privacy

- Consent is per-user and persists across sessions (stored on `:Person` node in Neo4j).
- Coordinates are stored at full device precision; iOS controls precise vs. approximate via its own permission dialog.
- Raw coordinates are **never logged** at any level.
- If `resolved=false` with `reason=consent_not_given`, tell the user: *"Location sharing is off — you can enable it in the settings drawer in the PWA."*
