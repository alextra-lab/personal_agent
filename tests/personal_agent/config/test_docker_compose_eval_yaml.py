"""Parity check for docker-compose.eval.yml's production-behaviour settings (FRE-1372 reopen).

Pure YAML load, no docker/env needed — the goal is only to catch a future edit that
adds or changes a behaviour-relevant AGENT_* key on one eval gateway service and not
the other, silently reintroducing the arm-3/control-vs-treatment asymmetry FRE-1350
found for tools.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_COMPOSE_PATH = Path(__file__).parents[3] / "docker-compose.eval.yml"

#: The behaviour-relevant keys FRE-1372's reopen added — every one of these must be
#: interpolated identically on both eval gateway services. Substrate/infra keys
#: (AGENT_DATABASE_URL, AGENT_NEO4J_URI, ...) are deliberately excluded — those are
#: supposed to differ from production and are covered by
#: AppConfig._validate_eval_deployment_isolation instead.
#:
#: AGENT_LINEAR_API_KEY, AGENT_PERPLEXITY_API_KEY and AGENT_VOYAGE_API_KEY are
#: deliberately NOT in this set (security review on this same reopen): none is
#: load-bearing for AC-1/AC-2, and seshat-gateway-treatment's unsandboxed,
#: curl-auto-approved bash tool with no approval gate makes routing them a live
#: credential-exfiltration / production-Linear-write path.
_BEHAVIOUR_KEYS = frozenset(
    {
        "AGENT_SECOND_BRAIN_RESOURCE_GATING_ENABLED",
        "AGENT_ENABLE_MEMORY_GRAPH",
        "AGENT_PROACTIVE_MEMORY_ENABLED",
        "AGENT_MULTIPATH_RECALL_ENABLED",
        "AGENT_RELEVANCE_BOUNDED_RECALL_ENABLED",
        "AGENT_LEXICAL_ARM_ENABLED",
        "AGENT_MULTIQUERY_ARM_ENABLED",
        "AGENT_RECALL_SIMILARITY_FLOOR",
        "AGENT_GROUNDING_VERIFICATION_MODE",
        "AGENT_LOCATION_ENABLED",
        "AGENT_ENABLE_SECOND_BRAIN",
        "AGENT_SKILL_ROUTING_MODE",
        "AGENT_OWNER_NAME",
        "AGENT_CONVERSATION_MAX_HISTORY_MESSAGES",
        "AGENT_SUBSTRATE_PROFILE",
        "AGENT_MANAGED_EMBEDDING_ENDPOINT",
        "AGENT_MANAGED_EMBEDDING_MODEL",
        "AGENT_MANAGED_EMBEDDING_TOKEN",
        "AGENT_LOCAL_FALLBACK_EMBEDDING_MODEL",
        "AGENT_CAPTAINS_LOG_REFLECTION_MIN_INTERVAL_SECONDS",
    }
)

#: The three tool credentials must NEVER appear on either eval gateway — this is
#: the negative-space guarantee the security review asked for: not just "this
#: version omits them" but "a future edit re-adding one is caught".
_EXCLUDED_CREDENTIAL_KEYS = frozenset(
    {
        "AGENT_LINEAR_API_KEY",
        "AGENT_PERPLEXITY_API_KEY",
        "AGENT_VOYAGE_API_KEY",
    }
)

_EVAL_GATEWAY_SERVICES = ("seshat-gateway-control", "seshat-gateway-treatment")


def _load_service_env_keys(service_name: str) -> set[str]:
    """Return the `environment:` key set of one service in docker-compose.eval.yml."""
    with _COMPOSE_PATH.open() as f:
        # PyYAML chokes on the top-level anchor merge (`<<: *base`) only when it
        # can't find the anchor — safe_load handles anchors/aliases fine as long as
        # both are in the same document, which they are here.
        doc = yaml.safe_load(f)
    service = doc["services"][service_name]
    return set(service["environment"].keys())


class TestBehaviourSettingsParity:
    """Both eval gateways declare the same behaviour-relevant AGENT_* keys."""

    def test_control_declares_every_behaviour_key(self) -> None:
        """seshat-gateway-control interpolates every FRE-1372 behaviour key."""
        keys = _load_service_env_keys("seshat-gateway-control")
        missing = _BEHAVIOUR_KEYS - keys
        assert not missing, f"seshat-gateway-control is missing: {sorted(missing)}"

    def test_treatment_declares_every_behaviour_key(self) -> None:
        """seshat-gateway-treatment interpolates every FRE-1372 behaviour key."""
        keys = _load_service_env_keys("seshat-gateway-treatment")
        missing = _BEHAVIOUR_KEYS - keys
        assert not missing, f"seshat-gateway-treatment is missing: {sorted(missing)}"

    def test_both_services_agree_on_the_behaviour_key_set(self) -> None:
        """No behaviour key is present on one eval gateway and absent on the other."""
        control_keys = _load_service_env_keys("seshat-gateway-control") & _BEHAVIOUR_KEYS
        treatment_keys = _load_service_env_keys("seshat-gateway-treatment") & _BEHAVIOUR_KEYS
        assert control_keys == treatment_keys

    def test_behaviour_keys_interpolate_by_name_not_hardcoded(self) -> None:
        """Every behaviour key's value is `${SAME_NAME}` — interpolated, never a literal."""
        for service_name in _EVAL_GATEWAY_SERVICES:
            with _COMPOSE_PATH.open() as f:
                doc = yaml.safe_load(f)
            env = doc["services"][service_name]["environment"]
            for key in _BEHAVIOUR_KEYS:
                assert env[key] == f"${{{key}}}", (
                    f"{service_name}.{key} is {env[key]!r}, expected interpolation "
                    f"from `.env` via ${{{key}}}"
                )


class TestCredentialKeysExcluded:
    """Neither eval gateway routes the three tool credentials the security review
    flagged: none is load-bearing for AC-1/AC-2, and seshat-gateway-treatment's
    unsandboxed, curl-auto-approved bash tool with no approval gate makes routing
    them a live credential-exfiltration / production-Linear-write path.
    """

    def test_control_never_declares_the_excluded_credential_keys(self) -> None:
        """seshat-gateway-control never gains AGENT_LINEAR/PERPLEXITY/VOYAGE_API_KEY."""
        keys = _load_service_env_keys("seshat-gateway-control")
        present = _EXCLUDED_CREDENTIAL_KEYS & keys
        assert not present, f"seshat-gateway-control must not declare: {sorted(present)}"

    def test_treatment_never_declares_the_excluded_credential_keys(self) -> None:
        """seshat-gateway-treatment never gains AGENT_LINEAR/PERPLEXITY/VOYAGE_API_KEY."""
        keys = _load_service_env_keys("seshat-gateway-treatment")
        present = _EXCLUDED_CREDENTIAL_KEYS & keys
        assert not present, f"seshat-gateway-treatment must not declare: {sorted(present)}"
