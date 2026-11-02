"""FRE-1728: a stub run cannot reach a real model, and the stub answers each caller."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from scripts.eval.fre1728 import stub_model, stub_stack

_REPO = Path(__file__).resolve().parents[2]


def test_every_model_role_is_bound_to_the_local_stub_deployment() -> None:
    source = (_REPO / "config" / "model_roles.yaml").read_text()
    rewritten = yaml.safe_load(stub_stack.stub_role_bindings(source))
    catalog = yaml.safe_load((_REPO / "config" / "models.yaml").read_text())["models"]

    for role, binding in rewritten["bindings"].items():
        if binding["deployment"] in {"inherit", "embedding", "reranker", "reranker_fallback"}:
            continue
        assert binding["deployment"] == stub_stack.LOCAL_DEPLOYMENT, role
    assert catalog[stub_stack.LOCAL_DEPLOYMENT]["provider"] == "slm_local"


def test_every_model_host_and_the_path_to_the_owners_mac_are_blocked() -> None:
    source = (_REPO / "config" / "models.yaml").read_text()
    hosts = stub_stack.blocked_hosts(source)

    assert "caddy" in hosts
    for host in re.findall(r"://(?:[^/@\s]*@)?([A-Za-z0-9._-]+)", source):
        assert host in hosts
    assert stub_stack.MODEL_NAME not in hosts


def test_each_host_is_blocked_on_ipv4_and_ipv6() -> None:
    assert stub_stack.add_host_flags(["api.openai.com"]) == [
        "--add-host",
        "api.openai.com:127.0.0.1",
        "--add-host",
        "api.openai.com:[::1]",
    ]


def test_gateway_env_holds_placeholder_keys_and_its_own_database_and_redis_index() -> None:
    env = stub_stack.gateway_env({"POSTGRES_PASSWORD": "p", "NEO4J_PASSWORD": "n"})

    assert env["AGENT_ANTHROPIC_API_KEY"] == stub_stack.PLACEHOLDER
    assert env["AGENT_OPENAI_API_KEY"] == stub_stack.PLACEHOLDER
    assert env["AGENT_DATABASE_URL"].endswith(f"/{stub_stack.STUB_DATABASE}")
    assert env["AGENT_EVENT_BUS_REDIS_URL"].endswith(f"/{stub_stack.STUB_REDIS_INDEX}")
    assert env["AGENT_SLM_BASE_URL"] == stub_stack.STUB_URL
    assert env["AGENT_MANAGED_EMBEDDING_ENDPOINT"].startswith(stub_stack.STUB_URL)


def test_env_guard_accepts_the_stub_env_and_refuses_a_production_host() -> None:
    env = stub_stack.gateway_env({"POSTGRES_PASSWORD": "p", "NEO4J_PASSWORD": "n"})

    stub_stack.assert_stub_env_is_eval_only(env)
    with pytest.raises(SystemExit, match="production host"):
        stub_stack.assert_stub_env_is_eval_only({"AGENT_NEO4J_URI": "bolt://neo4j:7687"})


def test_extraction_reply_writes_each_token_in_the_user_message() -> None:
    prompt = 'Return ONLY valid JSON "claims": [ ]\nUser: my badge is FACT-AB12\nAssistant: noted FACT-ZZ'
    reply = json.loads(stub_model.reply_for({"messages": [{"role": "user", "content": prompt}]}))

    assert [e["name"] for e in reply["entities"]] == ["FACT-AB12"]
    assert reply["claims"][0]["content"] == "The user said: my badge is FACT-AB12"


def test_primary_echoes_only_a_token_outside_its_own_message() -> None:
    body = {
        "tools": [{"type": "function"}],
        "messages": [
            {"role": "system", "content": "Memory: FACT-SEEN1"},
            {
                "role": "user",
                "content": "<turn_context>x</turn_context>\n\nECHO-MEMORY: FACT-ASKED?",
            },
        ],
    }

    assert stub_model.reply_for(body) == "I recall: FACT-SEEN1."


def test_seeded_negative_primary_does_not_echo_when_memory_holds_nothing() -> None:
    body = {
        "tools": [{"type": "function"}],
        "messages": [
            {"role": "system", "content": "Memory: none"},
            {"role": "user", "content": "ECHO-MEMORY: is FACT-ASKED in memory?"},
        ],
    }

    assert stub_model.reply_for(body) == stub_model.NO_RECALL


def test_memaudit_prompts_get_the_shapes_that_memaudit_parses() -> None:
    def ask(system: str, user: str) -> dict[str, object]:
        reply = stub_model.reply_for(
            {
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ]
            }
        )
        return json.loads(reply)

    simulated = ask(
        "You are the user asking an AI assistant for help", "Task / original question:\nx"
    )
    assert simulated["feedback_type"] == "satisfied"
    assert "FACT-" in str(simulated["text"])
    assert ask("", "Based solely on the above, fill in this dimension about the user. FACT-A1") == {
        "label": "FACT-A1",
        "reason": "stub slot fill",
    }
    assert ask("", "Compare two short descriptions\nPredicted: FACT-A1")["score"] == 1.0
    assert ask("", "Compare two short descriptions\nPredicted: I do not recall")["score"] == 0.0
    assert ask("", "whether a single task can ever naturally invite a user")["can_invite"] is True
    assert ask("", "Did the User simulator reveal it? FACT-A1")["disclosed"] is True
    assert ask("", "Pick exactly one")["category"] == "A"


def test_embedding_is_a_repeatable_unit_vector_of_the_catalog_size() -> None:
    first = stub_model.embedding_for("badge FACT-AB12")

    assert first == stub_model.embedding_for("badge FACT-AB12")
    assert len(first) == 1024
    assert abs(sum(x * x for x in first) - 1.0) < 1e-9


def test_pilot_overlay_passes_the_reranker_key_and_no_production_store() -> None:
    """Without the Voyage key every memory turn reports that its records could not be reached."""
    overlay = yaml.safe_load(
        (_REPO / "scripts" / "eval" / "fre1728" / "docker-compose.fre1728.yml").read_text()
    )

    for service in ("seshat-gateway-control", "seshat-gateway-treatment"):
        env = overlay["services"][service]["environment"]
        assert env["AGENT_VOYAGE_API_KEY"] == "${AGENT_VOYAGE_API_KEY}"
        assert "@postgres-eval:" in env["AGENT_SYSGRAPH_DATABASE_URL"]
        stub_stack.assert_stub_env_is_eval_only(
            {k: v for k, v in env.items() if k == "AGENT_SYSGRAPH_DATABASE_URL"}
        )
