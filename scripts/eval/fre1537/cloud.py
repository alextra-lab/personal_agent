"""FRE-1516 / ADR-0154 D7: the probe on a managed deployment (OVH, Anthropic).

The llama.cpp arms send ``chat_template_kwargs``, ``cache_prompt`` and a fixed URL, which OVH and
Anthropic refuse. This module sends the same planner request through the production client,
``LiteLLMClient.respond``, the way the planner call does (``expansion_controller``). So the request is the
one the gateway sends: the dialect's parameters, the Anthropic cache blocks, the provider's base URL.

Two guards keep a paid probe safe:

- ADR-0141 AC-6 confines ``litellm`` dispatch to ``llm_client/``. The probe goes through the client.
- ``respond`` reserves and records cost through the ``CostGate`` (ADR-0065). The probe registers a real
  gate and refuses to start unless ``settings.database_url`` is the eval Postgres (FRE-375). The cost rows
  never reach the production database.

A candidate mode is validated against the dialect of the deployment, so it cannot carry a field that the
catalog loader would refuse.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from scripts.eval.fre1537.common import read_jsonl

from personal_agent.config import load_model_config
from personal_agent.config.settings import get_settings
from personal_agent.cost_gate import (
    BudgetDenied,
    CostGate,
    load_budget_config,
    set_default_gate,
)
from personal_agent.llm_client.factory import get_llm_client_for_key
from personal_agent.llm_client.models import (
    DIALECT_FIELDS,
    DIALECT_VALUE_DOMAINS,
    ModelConfig,
    ModelDefinition,
    ModeSpec,
    Placement,
)
from personal_agent.llm_client.types import LLMClientError, ModelRole
from personal_agent.telemetry.trace import SystemTraceContext

MODE_NAMES = ("planner", "default")
REQUEST_TIMEOUT_S = 600.0
BUDGET_LANE = "main_inference"  # the lane of the `primary` role, which the planner call bills
EVAL_DB_PORT = 5434  # docker-compose.eval.yml, postgres-eval
_EVAL_DB_HOSTS = ("127.0.0.1", "localhost", "postgres-eval", "cloud-sim-postgres-eval")
_CHARS_PER_TOKEN = 3  # a low estimate, so the worst-case input cost is high


class CloudCallError(RuntimeError):
    """A managed deployment call failed."""


@dataclass(frozen=True)
class CloudTarget:
    """A managed deployment and the planner mode that a run measures on it.

    Attributes:
        key: The catalog key.
        provider: The catalog provider name (``ovhcloud``, ``anthropic``).
        model_id: The provider's model id.
        endpoint: The provider's declared base URL, or ``provider default``.
        mode_name: ``planner`` or ``default``.
        declared: The mode as declared (set fields only). A catalog mode and a candidate that match
            give the same value, so their fingerprints compare equal.
        definition: The effective definition, whose default mode is the measured mode.
        input_cost: Declared USD per input token.
        output_cost: Declared USD per output token.
    """

    key: str
    provider: str
    model_id: str
    endpoint: str
    mode_name: str
    declared: Mapping[str, object]
    definition: ModelDefinition
    input_cost: float
    output_cost: float


def _validate(spec: ModeSpec, dialect: Any, key: str) -> dict[str, object]:
    declared = spec.model_dump(exclude_none=True)
    outside = sorted(set(declared) - DIALECT_FIELDS[dialect])
    if outside:
        raise ValueError(f"{key!r} speaks {dialect.value}, which does not accept {outside}")
    for name, domain in DIALECT_VALUE_DOMAINS.get(dialect, {}).items():
        if name in declared and declared[name] not in domain:
            raise ValueError(
                f"{name}={declared[name]!r} is outside the {dialect.value} domain {sorted(domain)}"
            )
    return declared


def resolve_target(
    key: str,
    mode: str,
    *,
    candidate: Mapping[str, object] | None = None,
    config: ModelConfig | None = None,
) -> CloudTarget:
    """Resolve a managed deployment and one of its modes.

    Args:
        key: The catalog key of a managed deployment.
        mode: ``planner`` (the catalog's mode, or ``candidate``) or ``default`` (the catalog default).
        candidate: A candidate ``planner`` mode, for a deployment whose catalog has none yet.
        config: The catalog. Default is ``config/models.yaml``.

    Returns:
        The target.

    Raises:
        ValueError: For an unknown or local deployment, a mode the dialect refuses, a missing
            ``planner`` mode or missing declared per-token rates.
    """
    config = config or load_model_config()
    definition = config.models.get(key)
    if definition is None:
        raise ValueError(f"{key!r} is not in the model catalog")
    if config.placement_of(key) is Placement.LOCAL:
        raise ValueError(f"{key!r} is a local deployment: use the llama.cpp arms, not --deployment")
    if mode not in MODE_NAMES:
        raise ValueError(f"mode {mode!r}: use one of {MODE_NAMES}")
    if candidate is not None and mode != "planner":
        raise ValueError("--candidate describes the `planner` mode only")
    if definition.provider is None:
        raise ValueError(f"{key!r} declares no provider")
    provider = config.providers[definition.provider]
    dialect = definition.resolve_dialect(provider)
    if dialect is None:
        raise ValueError(f"{key!r} resolves no dialect")
    if mode == "default":
        spec = definition.modes[definition.default_mode or ""]
        effective = definition
    else:
        if candidate is not None:
            spec = ModeSpec.model_validate(dict(candidate))
        elif "planner" in definition.modes:
            spec = definition.modes["planner"]
        else:
            raise ValueError(
                f"{key!r} declares no `planner` mode in the catalog. Pass --candidate, then run the "
                "probe on it; ADR-0154 D7 admits a mode only with a passing result."
            )
        effective = definition.model_copy(
            update={"modes": {**definition.modes, "planner": spec}, "default_mode": "planner"}
        )
    declared = _validate(spec, dialect, key)
    if definition.input_cost_per_token is None or definition.output_cost_per_token is None:
        raise ValueError(f"{key!r} declares no per-token cost, so the cost cap cannot be enforced")
    return CloudTarget(
        key=key,
        provider=definition.provider,
        model_id=definition.id,
        endpoint=provider.base_url or "provider default",
        mode_name=mode,
        declared=declared,
        definition=effective,
        input_cost=definition.input_cost_per_token,
        output_cost=definition.output_cost_per_token,
    )


def assert_eval_database(url: str) -> None:
    """Refuse a database that is not the eval Postgres (FRE-375).

    Args:
        url: ``settings.database_url``.

    Raises:
        SystemExit: If the URL does not name the eval Postgres port on a local or eval host.
    """
    parts = urlsplit(url)
    if parts.port != EVAL_DB_PORT or parts.hostname not in _EVAL_DB_HOSTS:
        raise SystemExit(
            f"refusing to start: the cost gate writes reservations to the database, and this "
            f"database is not the eval Postgres (port {EVAL_DB_PORT}). Set AGENT_DATABASE_URL to it "
            "(`make eval-infra-up` starts it). The probe never writes cost rows to production."
        )


def _thinking_chars(blocks: object) -> int:
    total = 0
    for block in blocks if isinstance(blocks, list) else []:
        text = block.get("thinking") if isinstance(block, Mapping) else None
        total += len(str(text or ""))
    return total


def row_from_response(response: Mapping[str, Any], secs: float) -> dict[str, object]:
    """Turn a production ``LLMResponse`` into the row the scorer reads.

    The cloud client sets ``reasoning_trace`` to ``None`` whatever the model did, so the reasoning
    evidence is read from the provider message in ``raw``.

    Args:
        response: The ``LLMResponse`` of the planner call.
        secs: The call's wall time.

    Returns:
        ``secs``, ``content``, ``reasoning_chars``, ``thinking_blocks`` (a count, redacted blocks too),
        ``finish_reason``, ``usage`` (``reasoning_tokens`` when the provider reports it),
        ``served_model``, ``cost_usd`` and an empty ``timings``.
    """
    raw = response.get("raw") or {}
    choices = raw.get("choices") or [{}]
    message = (choices[0] or {}).get("message") or {}
    content = str(response.get("content") or "")
    blocks = message.get("thinking_blocks") or []
    chars = max(len(str(message.get("reasoning_content") or "")), _thinking_chars(blocks))
    inline = re.match(r"^(?:<think>)?(.*?)</think>", content, flags=re.S)
    chars += len(inline.group(1)) if inline else 0
    usage_raw = response.get("usage") or {}
    usage: dict[str, int] = {
        "prompt_tokens": int(usage_raw.get("prompt_tokens") or 0),
        "completion_tokens": int(usage_raw.get("completion_tokens") or 0),
    }
    details = (raw.get("usage") or {}).get("completion_tokens_details") or {}
    reasoning_tokens = usage_raw.get("reasoning_tokens", details.get("reasoning_tokens"))
    if reasoning_tokens is not None:
        usage["reasoning_tokens"] = int(reasoning_tokens)
    return {
        "secs": round(secs, 2),
        "content": content,
        "reasoning_chars": chars,
        "thinking_blocks": len(blocks),
        "finish_reason": response.get("finish_reason"),
        "usage": usage,
        "served_model": raw.get("model"),
        "cost_usd": round(float(response.get("cost_usd") or 0.0), 8),
        "timings": {},
    }


class CloudSession:
    """One event loop, one cost gate and one production client for a managed run."""

    def __init__(self, target: CloudTarget, *, gate: object | None = None) -> None:
        """Build the session.

        Args:
            target: The deployment and mode to measure.
            gate: A cost gate that the caller already registered. Default builds a real gate on
                ``settings.database_url``, after checking that it is the eval Postgres.

        Raises:
            SystemExit: If the database is not the eval Postgres.
        """
        self.target = target
        self._loop = asyncio.new_event_loop()
        self._gate: CostGate | None = None
        if gate is None:
            url = get_settings().database_url
            assert_eval_database(url)
            self._gate = CostGate(config=load_budget_config(), db_url=url)
            self._loop.run_until_complete(self._gate.connect())
            set_default_gate(self._gate)
        client = get_llm_client_for_key(target.key, budget_role=BUDGET_LANE)
        # The client reads its mode from `model_def` at call time. A candidate mode is not in the
        # catalog yet, so the effective definition replaces the catalog's.
        client.model_def = target.definition
        self._client = client
        self._ctx = dataclasses.replace(
            SystemTraceContext.new("fre1516_planner_probe"), session_id=str(uuid4())
        )

    def call(self, system: str, user: str) -> dict[str, object]:
        """Send one planner request and record what the scorer reads.

        Args:
            system: The planner system prompt.
            user: The planner user message.

        Returns:
            The row of :func:`row_from_response`.

        Raises:
            CloudCallError: On a provider, transport or timeout error.
            SystemExit: If the cost gate denies the call.
        """
        started = time.monotonic()
        try:
            response = self._loop.run_until_complete(
                asyncio.wait_for(
                    self._client.respond(
                        role=ModelRole.PRIMARY,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        response_format={"type": "json_object"},
                        trace_ctx=self._ctx,
                    ),
                    timeout=REQUEST_TIMEOUT_S,
                )
            )
        except BudgetDenied as exc:
            raise SystemExit(f"the cost gate denied the call: {exc}") from exc
        except (LLMClientError, TimeoutError) as exc:
            raise CloudCallError(str(exc)[:300]) from exc
        return row_from_response(response, time.monotonic() - started)

    def close(self) -> None:
        """Release the gate and the loop."""
        if self._gate is not None:
            set_default_gate(None)
            self._loop.run_until_complete(self._gate.reap_stale())
            self._loop.run_until_complete(self._gate.disconnect())
            self._gate = None
        if not self._loop.is_closed():
            self._loop.close()

    def __enter__(self) -> CloudSession:
        """Return the session."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the session."""
        self.close()


@dataclass(frozen=True)
class Budget:
    """One spending cap for every tag of a run directory.

    Attributes:
        run_dir: The run directory.
        max_usd: The cap in USD.
    """

    run_dir: Path
    max_usd: float

    def spent(self) -> float:
        """Return the cost of every recorded call of every tag of the run directory."""
        total = 0.0
        for name in ("decide", "longhist"):
            for path in sorted((self.run_dir / "rows").glob(f"*/{name}.jsonl")):
                for row in read_jsonl(path):
                    total += _row_cost(row)
        return total

    def check(self, target: CloudTarget, system: str, user: str) -> float:
        """Refuse a call that could reach the cap. Return its worst-case input cost.

        Output cost is counted when the call returns. A failed call is recorded at this estimate.

        Args:
            target: The deployment of the call.
            system: The system prompt.
            user: The user message.

        Returns:
            The estimated input cost in USD.

        Raises:
            SystemExit: If the spent total plus this estimate reaches the cap.
        """
        estimate = (len(system) + len(user)) / _CHARS_PER_TOKEN * target.input_cost
        spent = self.spent()
        if spent + estimate >= self.max_usd:
            raise SystemExit(
                f"cost cap of {self.max_usd:g} USD reached: {spent:.4f} USD spent in {self.run_dir}, "
                f"this call could add {estimate:.4f} USD"
            )
        return estimate


def _row_cost(row: Mapping[str, object]) -> float:
    total = float(row.get("cost_usd") or 0)  # type: ignore[arg-type]
    for key in ("cold", "extended"):
        inner = row.get(key)
        if isinstance(inner, Mapping):
            total += float(inner.get("cost_usd") or 0)
    return total
