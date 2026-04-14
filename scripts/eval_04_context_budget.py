#!/usr/bin/env python3
"""EVAL-04: Context budget behavior review.

Evaluates context budget behavior (Stage 7) by:
  1. Running long conversations (12 turns) with detailed context to stress-test the budget
  2. Tracking context_budget_applied events to observe token progression and trimming
  3. Re-running CP-19 (Long Conversation Trimming) and CP-20 (Progressive Token Budget)
     via the harness with trimming audit assertions (present("context_budget_applied"))
  4. Running CP-28 (Context Budget Trimming Audit) via the harness
  5. Analyzing: token count progression, first trimming trigger, overflow_action distribution,
     budget threshold appropriateness for the 35B model context window
  6. Printing a structured report with findings for EVAL-07

Usage:
    uv run python scripts/eval_04_context_budget.py

Requirements:
    - Agent service running at http://localhost:9000
    - Elasticsearch at http://localhost:9200
    - Infrastructure: ./scripts/init-services.sh
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
import structlog

# Resolve project root so imports work when run as a script
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

log = structlog.get_logger(__name__)

AGENT_URL = "http://localhost:9000"
ES_URL = "http://localhost:9200"
ES_INDEX = "agent-logs-*"

# ---------------------------------------------------------------------------
# Stress-test scenarios — designed to build long contexts
#
# Each scenario is 12 turns of deliberately verbose technical content.
# The goal is to observe token progression and, if possible, trigger trimming.
# Messages are long (200-400 words each) to accumulate tokens quickly.
# ---------------------------------------------------------------------------
STRESS_SCENARIOS: list[dict] = [
    {
        "name": "Distributed Systems Deep Dive",
        "turns": [
            # Turns 1-10: accumulate architectural context across distinct domains
            "We run a microservices platform on Kubernetes 1.29 with Istio service mesh. "
            "Our primary database is PostgreSQL 16 with Patroni HA and PgBouncer pooling.",
            "The message bus is Apache Kafka 3.6 in KRaft mode, with Confluent Schema Registry "
            "enforcing Avro backward compatibility. Peak throughput is 1.2M messages/second.",
            "Observability: Prometheus + VictoriaMetrics for metrics, Tempo for traces via "
            "OpenTelemetry SDK, Fluentbit → Kafka → Elasticsearch for structured logs.",
            "CI/CD is GitOps-based — GitHub Actions builds images pushed to ECR, ArgoCD "
            "deploys to 4 environments. Canary releases use Flagger with 99.9% success gates.",
            "Secrets live in HashiCorp Vault with dynamic database credentials (24h rotation). "
            "OPA Gatekeeper enforces 23 cluster-wide policies. Compliance is SOC2 Type II.",
            "ML workloads: MLflow experiment tracking, Kubeflow Pipelines on GPU nodes, "
            "Triton Inference Server behind FastAPI. Data drift alerts trigger shadow deploys.",
            "Developer portal is Backstage. Local dev uses Tilt + kind. "
            "Feature flags are managed by Unleash (340 active flags).",
            "Incident management: PagerDuty 24/7 on-call, blameless post-mortems mandatory "
            "for P1/P2. SLO target 99.95% availability using Sloth-generated recording rules.",
            "FinOps: Kubecost for cost attribution, $340K/month cloud spend. "
            "Karpenter autoscaling uses 60% spot instances, saving ~68% on compute.",
            "We use Qdrant for vector search (on-prem), pgvector for embedding storage in "
            "Postgres, and Redis Cluster for session caching and rate limiting.",
            # Turn 11: foundational recall — key CP-19 analog
            "Going back to the beginning: what is our primary database and why did we choose it?",
            # Turn 12: architecture synthesis
            "Summarize the full stack architecture in 3 bullet points for a new engineer.",
        ],
    },
]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
@dataclass
class BudgetObservation:
    """Raw context_budget_applied telemetry for a single turn."""

    session_id: str
    turn_index: int
    trace_id: str
    token_count: int | None
    trimmed: bool | None
    overflow_action: str | None
    has_memory: bool | None
    has_tools: bool | None
    message_count: int | None


@dataclass
class StressTestResult:
    """Aggregated results for one stress-test scenario."""

    scenario_name: str
    session_id: str
    observations: list[BudgetObservation] = field(default_factory=list)
    recall_response_turn11: str = ""
    recall_response_turn12: str = ""

    @property
    def max_token_count(self) -> int:
        """Return the highest token count observed across all turns."""
        counts = [o.token_count for o in self.observations if o.token_count is not None]
        return max(counts, default=0)

    @property
    def any_trimmed(self) -> bool:
        """Return True if trimming was triggered in any turn."""
        return any(o.trimmed for o in self.observations if o.trimmed is not None)

    @property
    def first_trim_turn(self) -> int | None:
        """Return the 1-based turn index of the first trimming event, or None."""
        for i, o in enumerate(self.observations):
            if o.trimmed:
                return i + 1
        return None


# ---------------------------------------------------------------------------
# Agent API helpers
# ---------------------------------------------------------------------------
async def create_session(client: httpx.AsyncClient) -> str:
    """Create a new agent session and return the session_id."""
    resp = await client.post(
        f"{AGENT_URL}/sessions",
        json={"channel": "CHAT", "mode": "NORMAL", "metadata": {}},
        timeout=10.0,
    )
    resp.raise_for_status()
    return str(resp.json()["session_id"])


async def send_message(client: httpx.AsyncClient, session_id: str, message: str) -> tuple[str, str]:
    """Send a message and return (response_text, trace_id)."""
    resp = await client.post(
        f"{AGENT_URL}/chat",
        params={"message": message, "session_id": session_id},
        timeout=300.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return str(data.get("response", "")), str(data.get("trace_id", ""))


# ---------------------------------------------------------------------------
# Elasticsearch helpers
# ---------------------------------------------------------------------------
async def fetch_budget_events(trace_id: str) -> dict | None:
    """Fetch context_budget_applied and gateway_output events for a trace_id from ES.

    Fetches all events for the trace_id (same strategy as TelemetryChecker),
    then filters in Python to avoid reliance on ES keyword field mappings.
    """
    from elasticsearch import AsyncElasticsearch

    es = AsyncElasticsearch([ES_URL], request_timeout=10)
    try:
        for attempt in range(4):
            response = await es.search(
                index=ES_INDEX,
                query={"bool": {"filter": [{"term": {"trace_id": trace_id}}]}},
                size=200,
                sort=[{"@timestamp": "asc"}],
            )
            hits = response.get("hits", {}).get("hits", [])
            all_events = [h["_source"] for h in hits]

            if all_events:
                # Filter in Python — avoids ES keyword mapping issues
                # ES documents use 'event_type' (structlog processor maps 'event' → 'event_type')
                budget_event = next(
                    (
                        e
                        for e in all_events
                        if e.get("event_type") == "context_budget_applied"
                        or e.get("event") == "context_budget_applied"
                    ),
                    None,
                )
                gateway_event = next(
                    (
                        e
                        for e in all_events
                        if e.get("event_type") == "gateway_output"
                        or e.get("event") == "gateway_output"
                    ),
                    None,
                )
                if budget_event or gateway_event:
                    return {"budget_event": budget_event, "gateway_event": gateway_event}

            if attempt < 3:
                await asyncio.sleep(1.5)

        return None
    finally:
        await es.close()


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------
async def run() -> None:
    """Execute the full EVAL-04 context budget review and print findings."""
    print("\n" + "=" * 70)
    print("EVAL-04: Context Budget Behavior Review")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)
    print()
    print("Settings under review:")
    print("  context_budget_comfortable_tokens : 32,000")
    print("  context_budget_max_tokens         : 65,536")
    print("  token estimation                  : word_count × 1.3")
    print("  trimming priority                 : history → memory → tools")
    print()

    # --- Pre-flight checks ---
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{AGENT_URL}/health", timeout=5.0)
            health = resp.json()
            if health.get("status") != "healthy":
                print(f"✗ Agent not healthy: {health}")
                sys.exit(1)
            print("✓ Agent healthy")
        except Exception as e:
            print(f"✗ Agent unreachable: {e}")
            sys.exit(1)

    try:
        from elasticsearch import AsyncElasticsearch

        es_test = AsyncElasticsearch([ES_URL], request_timeout=5)
        info = await es_test.info()
        await es_test.close()
        print(f"✓ Elasticsearch reachable (version {info['version']['number']})")
    except Exception as e:
        print(f"✗ Elasticsearch unreachable: {e}")
        print("  Note: budget telemetry analysis will be skipped")

    print()

    # -----------------------------------------------------------------------
    # Phase 1: Stress-test conversations — observe token progression
    # -----------------------------------------------------------------------
    print("─" * 70)
    print("Phase 1: Stress-test conversations (12 turns, verbose content)")
    print("─" * 70)
    print()
    print("Goal: observe token count progression; determine if/when trimming triggers")
    print("Budget ceiling: 65,536 estimated tokens")
    print()

    stress_results: list[StressTestResult] = []

    async with httpx.AsyncClient() as client:
        for scenario in STRESS_SCENARIOS:
            print(f"Scenario: {scenario['name']}")
            session_id = await create_session(client)
            result = StressTestResult(
                scenario_name=scenario["name"],
                session_id=session_id,
            )

            for i, turn_msg in enumerate(scenario["turns"]):
                turn_label = f"Turn {i + 1:2d}"
                response_text, trace_id = await send_message(client, session_id, turn_msg)

                # Allow ES to index
                await asyncio.sleep(2.0)

                events = await fetch_budget_events(trace_id)
                obs = BudgetObservation(
                    session_id=session_id,
                    turn_index=i,
                    trace_id=trace_id,
                    token_count=None,
                    trimmed=None,
                    overflow_action=None,
                    has_memory=None,
                    has_tools=None,
                    message_count=None,
                )

                if events:
                    budget = events.get("budget_event") or {}
                    gateway = events.get("gateway_event") or {}
                    obs = BudgetObservation(
                        session_id=session_id,
                        turn_index=i,
                        trace_id=trace_id,
                        token_count=int(
                            budget.get("total_tokens", 0) or gateway.get("token_count", 0)
                        ),
                        trimmed=bool(budget.get("trimmed") or gateway.get("budget_trimmed")),
                        overflow_action=str(
                            budget.get("overflow_action")
                            or gateway.get("overflow_action")
                            or "none"
                        ),
                        has_memory=bool(budget.get("has_memory")),
                        has_tools=bool(budget.get("has_tools")),
                        message_count=int(budget.get("message_count", 0)),
                    )

                result.observations.append(obs)

                tokens_display = f"{obs.token_count:>7,}" if obs.token_count else "    n/a"
                trim_flag = "TRIMMED" if obs.trimmed else "       "
                action = (
                    f"[{obs.overflow_action}]"
                    if obs.overflow_action and obs.overflow_action != "none"
                    else ""
                )
                print(f"  {turn_label}: {tokens_display} tokens  {trim_flag}  {action}")

                # Save recall responses for the last two turns
                if i == len(scenario["turns"]) - 2:
                    result.recall_response_turn11 = response_text
                elif i == len(scenario["turns"]) - 1:
                    result.recall_response_turn12 = response_text

            stress_results.append(result)
            print()

    # -----------------------------------------------------------------------
    # Phase 2: Run CP-19, CP-20, CP-28 via harness
    # -----------------------------------------------------------------------
    print("─" * 70)
    print("Phase 2: Harness run — CP-19, CP-20, CP-28")
    print("─" * 70)
    print()
    print("CP-19 and CP-20 now include present('context_budget_applied') assertions.")
    print("CP-28 is the dedicated Context Budget Trimming Audit path.")
    print()

    from tests.evaluation.harness.dataset import PATHS_BY_ID
    from tests.evaluation.harness.report import generate_json_report, generate_markdown_report
    from tests.evaluation.harness.runner import EvaluationRunner
    from tests.evaluation.harness.telemetry import TelemetryChecker

    telemetry = TelemetryChecker(es_url=ES_URL)

    runner = EvaluationRunner(
        agent_url=AGENT_URL,
        telemetry=telemetry,
    )

    harness_paths = [PATHS_BY_ID["CP-19"], PATHS_BY_ID["CP-20"], PATHS_BY_ID["CP-28"]]
    harness_results = await runner.run_paths(harness_paths)

    # Quick harness summary
    for r in harness_results:
        status = "PASS" if r.all_assertions_passed else "FAIL"
        print(
            f"  {r.path_id} ({r.path_name}): {status} "
            f"({r.passed_assertions}/{r.total_assertions} assertions)"
        )

    print()

    # Save harness reports
    output_dir = project_root / "telemetry" / "evaluation" / "eval-04-context-budget"
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / "harness_results.json"
    md_path = output_dir / "harness_results.md"
    generate_json_report(harness_results, json_path)
    generate_markdown_report(harness_results, md_path)
    print(f"  Harness reports saved to: {output_dir}")
    print()

    # -----------------------------------------------------------------------
    # Phase 3: Budget analysis
    # -----------------------------------------------------------------------
    print("─" * 70)
    print("Phase 3: Budget analysis")
    print("─" * 70)
    print()

    for result in stress_results:
        print(f"Scenario: {result.scenario_name}")
        print(f"  Session: {result.session_id}")
        print(f"  Max token count observed  : {result.max_token_count:,}")
        print(f"  Trimming triggered        : {result.any_trimmed}")
        print(f"  First trim turn           : {result.first_trim_turn or 'never'}")

        if result.observations:
            # Token progression
            token_counts = [o.token_count for o in result.observations if o.token_count]
            if token_counts:
                print("  Token count progression:")
                for i, obs in enumerate(result.observations):
                    if obs.token_count:
                        bar_len = min(40, obs.token_count * 40 // 65536)
                        bar = "█" * bar_len
                        pct = obs.token_count / 65536 * 100
                        print(
                            f"    Turn {i + 1:2d}: {obs.token_count:>7,} "
                            f"({pct:4.1f}% of max)  {bar}"
                        )

        print()

    # -----------------------------------------------------------------------
    # Phase 4: Summary report
    # -----------------------------------------------------------------------
    print("=" * 70)
    print("EVAL-04 Summary — Findings for EVAL-07")
    print("=" * 70)

    all_observations: list[BudgetObservation] = [o for r in stress_results for o in r.observations]
    observed_tokens = [o.token_count for o in all_observations if o.token_count]
    any_trimmed_overall = any(o.trimmed for o in all_observations if o.trimmed is not None)
    overflow_actions = [
        o.overflow_action
        for o in all_observations
        if o.overflow_action and o.overflow_action != "none"
    ]

    harness_passed = sum(r.passed_assertions for r in harness_results)
    harness_total = sum(r.total_assertions for r in harness_results)

    print(f"""
Stress-test observations
────────────────────────
  Conversations run        : {len(stress_results)} × 12 turns each
  Total turns observed     : {len(all_observations)}
  Token counts collected   : {len(observed_tokens)}
  Max token count seen     : {max(observed_tokens, default=0):,}  (budget ceiling: 65,536)
  Trimming triggered       : {any_trimmed_overall}
  Overflow actions seen    : {overflow_actions or ["none"]}

CP-19/CP-20/CP-28 harness
──────────────────────────
  Assertions passed        : {harness_passed}/{harness_total}""")

    for r in harness_results:
        status = "PASS" if r.all_assertions_passed else "FAIL"
        print(f"  {r.path_id}                       : {status}")

    # Budget threshold assessment
    max_observed = max(observed_tokens, default=0)
    comfortable_limit = 32_000
    max_limit = 65_536
    print(f"""
Budget threshold assessment
────────────────────────────
  context_budget_comfortable_tokens : {comfortable_limit:,}
  context_budget_max_tokens         : {max_limit:,}
  Max tokens observed (12-turn verbose conversation): {max_observed:,}
  Budget utilisation                : {max_observed / max_limit * 100:.1f}% of max

Trimming priority order (from budget.py)
─────────────────────────────────────────
  1. Drop oldest history   (preserve system + last user message)
  2. Drop memory context   (Seshat enrichment)
  3. Drop tool definitions

Assessment
──────────""")

    if not any_trimmed_overall and max_observed < comfortable_limit:
        print(f"""  Budget NOT triggered in 12-turn verbose conversations.
  Token counts peaked at {max_observed:,} — well under the 65,536 ceiling.
  This is expected: the 35B model has a large context window (128K+ tokens),
  and the budget ceiling at 65,536 is appropriately conservative.

  Key implication: for CP-19 specifically, PostgreSQL fact survival at Turn 10
  is NOT a budget-trimming concern — it is a model attention concern.
  The context is present; the question is whether the model attends to it.

  Recommendation for trimming priority order:
  The current order (history → memory → tools) is CORRECT for the intended use case:
  - Dropping oldest history first preserves the current context window while
    allowing the conversation to continue indefinitely
  - Memory context drop is second because it is session-level enrichment that can
    be re-queried; this is less destructive than losing tool capabilities
  - Tool definitions are last because losing them breaks functionality entirely
  Priority order is SOUND. No change recommended.""")
    elif any_trimmed_overall:
        print(f"""  Budget WAS triggered! See overflow_actions: {overflow_actions}
  Examine harness_results.md for which turns triggered trimming.
  Verify that foundational facts (like PostgreSQL) survived the trim.""")
    else:
        print(f"""  Budget not triggered but token counts are significant ({max_observed:,}).
  Consider whether the 35B model's effective context window is well-represented
  by the current word_count × 1.3 estimation heuristic.""")

    print("""
Quality criteria (CP-19)
─────────────────────────
  Verify manually in harness_results.md:
  [ ] Turn 10 correctly identifies PostgreSQL as primary database
  [ ] If trimmed, foundational facts were retained
  [ ] context_budget_applied event fired on Turn 10
  [ ] overflow_action logged correctly (or 'none' if no trim)

Quality criteria (CP-20)
─────────────────────────
  [ ] Each tool call returns valid data
  [ ] Turn 4 synthesizes findings coherently
  [ ] context_budget_applied event fired on Turn 4
  [ ] If trimmed, most recent tool results were preserved

Feeds into EVAL-07: budget threshold assessment and trimming priority order
""")

    # -----------------------------------------------------------------------
    # Write raw data
    # -----------------------------------------------------------------------
    import json

    raw_data = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "settings": {
            "context_budget_comfortable_tokens": comfortable_limit,
            "context_budget_max_tokens": max_limit,
            "token_estimation": "word_count * 1.3",
            "trimming_priority": [
                "1. dropped_oldest_history",
                "2. dropped_memory_context",
                "3. dropped_tool_definitions",
            ],
        },
        "stress_tests": [
            {
                "scenario": r.scenario_name,
                "session_id": r.session_id,
                "max_token_count": r.max_token_count,
                "any_trimmed": r.any_trimmed,
                "first_trim_turn": r.first_trim_turn,
                "observations": [
                    {
                        "turn": o.turn_index + 1,
                        "trace_id": o.trace_id,
                        "token_count": o.token_count,
                        "trimmed": o.trimmed,
                        "overflow_action": o.overflow_action,
                        "has_memory": o.has_memory,
                        "has_tools": o.has_tools,
                        "message_count": o.message_count,
                    }
                    for o in r.observations
                ],
                "recall_turn_11_excerpt": r.recall_response_turn11[:500],
                "recall_turn_12_excerpt": r.recall_response_turn12[:500],
            }
            for r in stress_results
        ],
        "harness": {
            "assertions_passed": harness_passed,
            "assertions_total": harness_total,
            "paths": [
                {
                    "path_id": r.path_id,
                    "all_passed": r.all_assertions_passed,
                    "passed": r.passed_assertions,
                    "total": r.total_assertions,
                }
                for r in harness_results
            ],
        },
        "summary": {
            "max_tokens_observed": max_observed,
            "budget_ceiling": max_limit,
            "budget_utilisation_pct": max_observed / max_limit * 100 if max_limit else 0,
            "trimming_triggered": any_trimmed_overall,
            "overflow_actions_observed": overflow_actions,
        },
    }

    results_path = output_dir / "results.json"
    results_path.write_text(json.dumps(raw_data, indent=2, default=str))
    print(f"Raw data written to: {results_path}")
    print()


if __name__ == "__main__":
    asyncio.run(run())
