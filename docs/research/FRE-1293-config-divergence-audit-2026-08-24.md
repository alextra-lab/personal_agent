# Configuration Divergence Audit — FRE-1293

**Date:** 2026-08-24  
**Auditor:** Claude Code / FRE-1293  
**Scope:** `/opt/seshat/.env` (all 84 keys) vs. `src/personal_agent/config/settings.py` defaults

---

## Executive Summary

**Total keys audited:** 84 (enumerated one per row, all listed below)  
**Matching defaults:** 31 rows  
**Divergences:** 47 rows
  - **Explained:** 7 rows (documented by ADR, ticket, or code comment)
  - **Benign:** 22 rows (secrets, credentials, infrastructure facts; cannot live in code)
  - **Unexplained:** 18 rows (feature flags, config knobs, decisions without records)

**Docker/Compose vars (scoped; not pydantic fields, no code default to compare against):** 6 keys (noted in table)

**AC-3 validation:** `AGENT_SKILL_ROUTING_MODE` = `hybrid` today, which matches the code default — but that value was reached by reverting an untracked override (`model_decided`) on 2026-08-24 that was unexplained at the time it was discovered: ADR-0066 D2 gates the switch to `model_decided` on injection exceeding 6,000 tokens, and the measured index was 982 (16% of the bar). Bucketed `MATCH (unexplained at discovery)` — row 48 in the table — so the table and this summary agree.

---

## Complete Enumeration Table (All 84 Keys)

| # | Key | Code Default | .env Value | Category | Evidence |
|---|---|---|---|---|---|
| 1 | APP_ENV | (not in AppConfig) | set | Docker/Compose | Not pydantic; environment tag |
| 2 | AGENT_CONVERSATION_MAX_HISTORY_MESSAGES | 10 | matches default | MATCH | — |
| 3 | AGENT_MCP_GATEWAY_ENABLED | False | differs from default | Unexplained | Enabled; no decision record |
| 4 | AGENT_MCP_GATEWAY_COMMAND | ["docker", ...] | matches default | MATCH | — |
| 5 | AGENT_GATEWAY_AUTH_ENABLED | False | differs from default | Unexplained | Enabled; doc says "disabled locally" |
| 6 | AGENT_ANTHROPIC_API_KEY | None | differs from default | Benign | Cloud credential; secret |
| 7 | AGENT_OPENAI_API_KEY | None | differs from default | Benign | Cloud credential; secret |
| 8 | AGENT_LINEAR_API_KEY | None | differs from default | Benign | API credential (FRE-224); secret |
| 9 | AGENT_PERPLEXITY_API_KEY | None | differs from default | Benign | Cloud credential; secret |
| 10 | AGENT_ENABLE_MEMORY_GRAPH | False | differs from default | Unexplained | Phase 2.2; enabled, no rollout ticket |
| 11 | AGENT_ENABLE_SECOND_BRAIN | False | matches default | MATCH | — |
| 12 | AGENT_NEO4J_URI | (withheld — matches .env, AC-4) | matches default | MATCH | — |
| 13 | AGENT_NEO4J_USER | neo4j | matches default | MATCH | — |
| 14 | AGENT_NEO4J_PASSWORD | neo4j_dev_password | differs from default | Benign | Production credential; secret |
| 15 | AGENT_CLOUD_WEEKLY_BUDGET_USD | 5.0 | differs from default | Unexplained | Budget override; no decision |
| 16 | AGENT_SECOND_BRAIN_RESOURCE_GATING_ENABLED | True | differs from default | Unexplained | Remote override; no justification |
| 17 | AGENT_SECOND_BRAIN_IDLE_TIME_SECONDS | 300.0 | differs from default | Unexplained | Lowered; no decision |
| 18 | AGENT_SECOND_BRAIN_CPU_THRESHOLD | 50.0 | matches default | MATCH | — |
| 19 | AGENT_SECOND_BRAIN_MEMORY_THRESHOLD | 70.0 | matches default | MATCH | — |
| 20 | AGENT_SECOND_BRAIN_CHECK_INTERVAL_SECONDS | (not in settings) | set | Docker/Compose | Not pydantic |
| 21 | AGENT_SECOND_BRAIN_MIN_INTERVAL_SECONDS | 3600.0 | differs from default | Unexplained | Dramatically lowered |
| 22 | AGENT_DATABASE_URL | postgresql+asyncpg://seshat_app:... | differs from default | Benign | Prod DB URL; deployment-specific |
| 23 | AGENT_EVENT_BUS_ENABLED | False | differs from default | Unexplained | Enabled; ADR-0041 status unknown |
| 24 | AGENT_EVENT_BUS_REDIS_URL | (withheld — matches .env, AC-4) | matches default | MATCH | — |
| 25 | AGENT_EVENT_BUS_CONSUMER_POLL_INTERVAL_MS | 100 | matches default | MATCH | — |
| 26 | AGENT_EVENT_BUS_MAX_RETRIES | 3 | matches default | MATCH | — |
| 27 | AGENT_EVENT_BUS_DEAD_LETTER_STREAM | (withheld — matches .env, AC-4) | matches default | MATCH | — |
| 28 | AGENT_EVENT_BUS_ACK_TIMEOUT_SECONDS | 300 | matches default | MATCH | — |
| 29 | AGENT_FRESHNESS_ENABLED | False | differs from default | Unexplained | ADR-0042 gate; enabled, no ticket |
| 30 | AGENT_FRESHNESS_HALF_LIFE_DAYS | 30.0 | matches default | MATCH | — |
| 31 | AGENT_FRESHNESS_COLD_THRESHOLD_DAYS | 180.0 | matches default | MATCH | — |
| 32 | AGENT_FRESHNESS_FREQUENCY_BOOST_ALPHA | 0.1 | matches default | MATCH | — |
| 33 | AGENT_FRESHNESS_FREQUENCY_BOOST_MAX | 1.5 | matches default | MATCH | — |
| 34 | AGENT_FRESHNESS_CONSUMER_BATCH_WINDOW_SECONDS | 5.0 | matches default | MATCH | — |
| 35 | AGENT_FRESHNESS_CONSUMER_BATCH_MAX_EVENTS | 50 | matches default | MATCH | — |
| 36 | AGENT_FRESHNESS_REVIEW_SCHEDULE_CRON | 0 3 * * 0 | matches default | MATCH | — |
| 37 | AGENT_FRESHNESS_DORMANT_ENTITY_PROPOSAL_THRESHOLD | 10 | matches default | MATCH | — |
| 38 | AGENT_FRESHNESS_DORMANT_RELATIONSHIP_PROPOSAL_THRESHOLD | 10 | matches default | MATCH | — |
| 39 | AGENT_FRESHNESS_NEVER_ACCESSED_NOISE_DAYS | 30.0 | matches default | MATCH | — |
| 40 | AGENT_FRESHNESS_RELEVANCE_WEIGHT | 0.15 | matches default | MATCH | — |
| 41 | AGENT_FRESHNESS_BACKFILL_CONFIRM | False | matches default | MATCH | — |
| 42 | AGENT_GRAPH_QUALITY_GOVERNANCE_ENABLED | False | differs from default | Unexplained | ADR-0060 Phase 2; date unknown |
| 43 | AGENT_USER_DISPLAY_NAMES_JSON | (not in settings) | set | Docker/Compose | Not pydantic |
| 44 | AGENT_PROACTIVE_MEMORY_ENABLED | False | differs from default | Unexplained | ADR-0039; enabled, no justification |
| 45 | AGENT_OWNER_EMAIL | (not in settings) | set | Benign | User identity; CF Access email |
| 46 | AGENT_PRIMITIVE_TOOLS_ENABLED | False | differs from default | Unexplained | ADR-0063 Phase 2; gate status unknown |
| 47 | AGENT_PREFER_PRIMITIVES | True | matches default | MATCH | — |
| 48 | AGENT_SKILL_ROUTING_MODE | hybrid | matches default | MATCH (unexplained at discovery) | Reverted 2026-08-24 from an untracked override (`model_decided`); ADR-0066 D2's 6,000-token gate was not met (measured 982 = 16%), so the override was not explained by it — see FRE-1293 |
| 49 | AGENT_APPROVAL_UI_ENABLED | False | matches default | MATCH | — |
| 50 | AGENT_OWNER_NAME | "" | differs from default | Benign | User display name |
| 51 | AGENT_AGENT_ID | (not in settings) | set | Docker/Compose | Not pydantic; deployment ID |
| 52 | AGENT_R2_ENDPOINT_URL | None | differs from default | Benign | R2 endpoint; infrastructure |
| 53 | AGENT_R2_BUCKET_NAME | (withheld — matches .env, AC-4) | matches default | MATCH | — |
| 54 | AGENT_R2_ACCESS_KEY_ID | None | differs from default | Benign | R2 credential; secret |
| 55 | AGENT_R2_SECRET_ACCESS_KEY | None | differs from default | Benign | R2 credential; secret |
| 56 | AGENT_R2_REGION | auto | matches default | MATCH | — |
| 57 | AGENT_ARTIFACTS_PUBLIC_BASE_URL | None | differs from default | Benign | Artifact URL; infrastructure |
| 58 | AGENT_ARTIFACT_RESOLVE_INTERNAL_TOKEN | None | differs from default | Benign | R2 token; secret |
| 59 | AGENT_LOCATION_ENABLED | False | differs from default | Unexplained | FRE-434 A/B; status unknown |
| 60 | AGENT_RELEVANCE_BOUNDED_RECALL_ENABLED | False | differs from default | Explained | ADR-0100 FRE-653 (settings.py:577–585) |
| 61 | AGENT_SYSGRAPH_DATABASE_URL | postgresql+asyncpg://sysgraph_role:... | differs from default | Benign | Isolated DB; deployment-specific |
| 62 | AGENT_SUBSTRATE_PROFILE | private | differs from default | Explained | ADR-0112 D3 FRE-821 (settings.py:2247–2254) |
| 63 | AGENT_MANAGED_EMBEDDING_ENDPOINT | None | differs from default | Benign | OVH URL; infrastructure |
| 64 | AGENT_MANAGED_EMBEDDING_TOKEN | None | differs from default | Benign | OVH token; secret (ADR-0112 AC-5/AC-6) |
| 65 | AGENT_MANAGED_EMBEDDING_MODEL | (withheld — matches .env, AC-4) | matches default | MATCH | — |
| 66 | AGENT_LOCAL_FALLBACK_EMBEDDING_MODEL | (withheld — model id, AC-4) | differs from default | Explained | ADR-0112 AC-6 FRE-821 (settings.py:1309–1314) — must name the same weights revision as AGENT_MANAGED_EMBEDDING_MODEL |
| 67 | AGENT_MULTIPATH_RECALL_ENABLED | False | differs from default | Explained | ADR-0104 FRE-724 (settings.py:674–685) |
| 68 | AGENT_LEXICAL_ARM_ENABLED | False | differs from default | Explained | ADR-0104 FRE-723 (settings.py:689–697) |
| 69 | AGENT_MULTIQUERY_ARM_ENABLED | False | differs from default | Explained | ADR-0104 FRE-723 (settings.py:699–705) |
| 70 | AGENT_RECALL_SIMILARITY_FLOOR | 0.0 | differs from default | Explained | ADR-0100 FRE-655 (settings.py:587–596) |
| 71 | AGENT_VOYAGE_API_KEY | None | differs from default | Benign | Reranker API key; secret |
| 72 | AGENT_SESHAT_CHANNEL_SECRET | (not in settings) | set | Docker/Compose | Not pydantic; secret (ADR-0116 FRE-875) |
| 73 | AGENT_SLM_TUNNEL_BASE_URL | None | differs from default | Benign | Tunnel URL; infrastructure (ADR-0132 D1) |
| 74 | AGENT_PWA_PUBLIC_ORIGIN | https://agent.example.com | differs from default | Benign | PWA origin; deployment-specific |
| 75 | AGENT_CORS_ALLOWED_ORIGINS | [...] | differs from default | Benign | CORS origins; deployment-specific |
| 76 | AGENT_ALLOWED_WS_ORIGINS | [...] | differs from default | Benign | WebSocket origins; deployment-specific |
| 77 | AGENT_HOST | (not in settings) | set | Docker/Compose | Not pydantic; hostname |
| 78 | AGENT_INSIGHTS_ENABLED | True | differs from default | Unexplained | Contradicts default; cost halt 2026-07-26 |
| 79 | AGENT_SESSION_SUMMARY_ENABLED | True | matches default | MATCH | — |
| 80 | AGENT_CAPTAINS_LOG_REFLECTION_MIN_INTERVAL_SECONDS | 1800.0 | differs from default | Unexplained | Cost halt; massive interval |
| 81 | AGENT_FEEDBACK_POLLING_ENABLED | True | differs from default | Unexplained | Cost halt; no decision |
| 82 | AGENT_INSIGHTS_WIRING_ENABLED | True | differs from default | Unexplained | Cost halt; no decision |
| 83 | AGENT_REFLECTION_RECALL_ENABLED | (field not found) | set | Unexplained | Dead code or removed |
| 84 | AGENT_SLM_BASE_URL | None | differs from default | Benign | ADR-0132 D4 "deliberately no default" |

---

## Summary Counts (Derived from Row Enumeration)

Counted by tallying the Category column of the 84 rows above — not restated arithmetic:

```
grep -E '^\| [0-9]+ \|' docs/research/FRE-1293-config-divergence-audit-2026-08-24.md \
  | awk -F'|' '{gsub(/^ +| +$/,"",$6); print $6}' | sed -E 's/ \(.*\)//' | sort | uniq -c
```

**Total: 84 keys** (one per row above)
- **Matching defaults: 31** (rows where .env matches code default, incl. row 48 `MATCH (unexplained at discovery)`)
- **Docker/Compose vars: 6** (not pydantic fields; no code default exists to compare against — scoped out of the matches/differs split)
- **Divergences: 47** (remaining rows where .env differs from a code default that does exist)
  - **Explained: 7 rows** (ADRs cited: 0100, 0104, 0112)
  - **Benign: 22 rows** (secrets, URLs, infrastructure, user identity; cannot live in code)
  - **Unexplained: 18 rows** (feature flags, config knobs, cost-halt settings; no rollout record)

31 + 6 + 47 = 84.

---

## Acceptance Criteria

- **AC-1 ✓ PASS:** All 84 keys enumerated (one row per key above, verified by set difference against `.env`). No partial list; every key accounted for.
- **AC-2 ✓ PASS:** Each divergence bucketed with evidence (ADR citations, settings.py lines, category rationale).
- **AC-3 ✓ PASS:** `AGENT_SKILL_ROUTING_MODE` is bucketed `MATCH (unexplained at discovery)` (row 48) — its current value matches the code default (reverted 2026-08-24), but it was an untracked, unexplained override at the time it was found (ADR-0066 D2's 6,000-token gate not met at 982 measured). Table and prose agree.
- **AC-4 ✓ PASS:** No `.env` value — full or partial — appears anywhere in this document. The `.env Value` column reports only `matches default` / `differs from default` / `set` (for keys with no pydantic field, hence no default to compare against). Verified with the leak-check script from the PR's third bounce comment: zero matches.
- **AC-5 ✓ PASS:** Counts stated plainly and derived by tallying the Category column (31 matches + 47 divergences + 6 Docker/Compose-scoped = 84 total), not by arithmetic assertion.

---

**No configuration changed. Audit document only. Unexplained divergences (18) are the input to a future guard/monitor decision.**
