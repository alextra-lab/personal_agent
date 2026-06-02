-- FRE-437: Add per-tier cache token columns to api_costs.
--
-- Anthropic prompt caching uses asymmetric pricing (cache_read ≈ 0.1×
-- standard input; cache_creation ≈ 1.25× standard input).  cost_usd already
-- reflects these tiers via litellm.completion_cost(), but the individual
-- token counts were not persisted so historical rows couldn't be audited.
--
-- NULL = non-Anthropic call or pre-migration row (not zero cache hits).

ALTER TABLE api_costs
    ADD COLUMN IF NOT EXISTS cache_read_input_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS cache_creation_input_tokens INTEGER;
