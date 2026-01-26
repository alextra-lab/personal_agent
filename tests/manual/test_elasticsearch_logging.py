"""Manual test for Elasticsearch logging integration.

This script tests that:
1. Elasticsearch transport logs are suppressed
2. Interesting structured data is captured in Elasticsearch
3. No feedback loop occurs

Run with: uv run python tests/manual/test_elasticsearch_logging.py
"""

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path (ruff: noqa: E402)
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root / "src"))

from personal_agent.telemetry import add_elasticsearch_handler, get_logger  # noqa: E402
from personal_agent.telemetry.es_handler import ElasticsearchHandler  # noqa: E402


async def test_logging():
    """Test Elasticsearch logging with rich structured data."""
    log = get_logger(__name__)

    print("🔧 Connecting to Elasticsearch...")
    es_handler = ElasticsearchHandler("http://localhost:9200")

    if not await es_handler.connect():
        print("❌ Failed to connect to Elasticsearch")
        return False

    print("✅ Connected to Elasticsearch")

    # Add handler to logging system
    add_elasticsearch_handler(es_handler)
    print("✅ Handler added to logging system")

    # Test 1: Simple log with structured data
    print("\n📝 Test 1: Simple structured log")
    log.info(
        "test_simple_log",
        trace_id="test-trace-123",
        user_id="user-456",
        action="testing",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    # Test 2: Rich context log (like what memory service would generate)
    print("📝 Test 2: Memory query simulation")
    log.info(
        "memory_query_executed",
        trace_id="test-trace-124",
        query_type="entity_search",
        entity_names=["Python", "Django", "FastAPI"],
        conversations_found=5,
        duration_ms=42.3,
        relevance_score_avg=0.87,
    )

    # Test 3: Metrics-style log (like scheduler would generate)
    print("📝 Test 3: System metrics simulation")
    log.info(
        "system_metrics_snapshot",
        cpu_percent=23.4,
        memory_percent=67.2,
        idle_minutes=15.7,
        consolidation_due=True,
    )

    # Test 4: Error with exception
    print("📝 Test 4: Error with context")
    try:
        raise ValueError("Test error for logging")
    except ValueError:
        log.error(
            "test_error_with_context",
            trace_id="test-trace-125",
            error_type="ValueError",
            context={"operation": "testing", "expected": "success"},
            exc_info=True,
        )

    # Test 5: Task execution log (like orchestrator would generate)
    print("📝 Test 5: Task execution simulation")
    log.info(
        "task_execution_complete",
        trace_id="test-trace-126",
        session_id="session-789",
        user_message="Test user query",
        tools_used=["llm_client", "memory_service", "code_executor"],
        duration_ms=1234.5,
        outcome="SUCCESS",
        memory_context_used=True,
        conversations_retrieved=3,
    )

    # Give Elasticsearch a moment to index
    print("\n⏳ Waiting for Elasticsearch to index (2 seconds)...")
    await asyncio.sleep(2)

    # Disconnect
    await es_handler.disconnect()
    print("✅ Disconnected from Elasticsearch")

    print("\n" + "=" * 60)
    print("✅ Test complete!")
    print("=" * 60)
    print("\n📊 To view the logs in Elasticsearch:")
    print("   1. Open Kibana: http://localhost:5601")
    print("   2. Go to: Management > Stack Management > Data Views")
    print("   3. Create data view: agent-logs-*")
    print("   4. Go to: Analytics > Discover")
    print("   5. Filter by: event: test_* (to see these test logs)")
    print("\n🔍 Or query directly:")
    print('   curl "http://localhost:9200/agent-logs-*/_search?q=event:test_*&pretty"')

    return True


async def query_test_logs():
    """Query Elasticsearch for the test logs we just created."""
    import httpx

    print("\n🔍 Querying Elasticsearch for test logs...")

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                "http://localhost:9200/agent-logs-*/_search",
                params={
                    "q": "event:test_*",
                    "size": 10,
                    "sort": "@timestamp:desc",
                },
                timeout=10.0,
            )

            if response.status_code == 200:
                result = response.json()
                hits = result.get("hits", {}).get("hits", [])

                if hits:
                    print(f"✅ Found {len(hits)} test log entries!")
                    print("\n📄 Sample log entry:")
                    sample = hits[0]["_source"]
                    print(f"   Event: {sample.get('event')}")
                    print(f"   Level: {sample.get('level')}")
                    print(f"   Component: {sample.get('component')}")
                    print(f"   Message: {sample.get('message')}")

                    # Show custom fields
                    custom_fields = {
                        k: v
                        for k, v in sample.items()
                        if k
                        not in [
                            "@timestamp",
                            "event",
                            "level",
                            "logger",
                            "component",
                            "message",
                            "module",
                            "function",
                            "line_number",
                        ]
                    }
                    if custom_fields:
                        print(f"   Custom fields: {list(custom_fields.keys())}")
                else:
                    print("⚠️  No test logs found yet (may take a moment to index)")
            else:
                print(f"❌ Query failed: {response.status_code}")
        except Exception as e:
            print(f"❌ Error querying: {e}")


if __name__ == "__main__":
    print("🚀 Testing Elasticsearch Logging Integration")
    print("=" * 60)

    async def main():
        """Run the Elasticsearch logging tests."""
        success = await test_logging()
        if success:
            await query_test_logs()

    asyncio.run(main())
