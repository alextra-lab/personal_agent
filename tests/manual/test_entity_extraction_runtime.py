"""Manual runtime test for entity extraction with local SLM models.

Run this after starting:
1. docker compose up -d (infrastructure)
2. SLM server on port 8000
3. Personal agent service (optional for this test)
"""

import asyncio
import json
from datetime import datetime

from personal_agent.config.settings import get_settings
from personal_agent.second_brain.entity_extraction import extract_entities_and_relationships
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


async def test_extraction(model_name: str, user_msg: str, assistant_msg: str) -> dict:
    """Test entity extraction with a specific model."""
    settings = get_settings()
    original_model = settings.entity_extraction_model

    try:
        # Set model
        settings.entity_extraction_model = model_name

        print(f"\n{'=' * 80}")
        print(f"Testing: {model_name}")
        print(f"{'=' * 80}")
        print(f"User: {user_msg[:100]}...")
        print(f"Assistant: {assistant_msg[:100]}...")

        start_time = datetime.now()
        result = await extract_entities_and_relationships(
            user_message=user_msg,
            assistant_response=assistant_msg,
        )
        elapsed = (datetime.now() - start_time).total_seconds() * 1000

        print(f"\n✓ Extraction completed in {elapsed:.0f}ms")
        print(f"  - Entities: {len(result.get('entity_names', []))}")
        print(f"  - Relationships: {len(result.get('relationships', []))}")
        print(f"  - Summary: {result.get('summary', 'N/A')[:100]}...")

        if result.get("entity_names"):
            print(f"  - Entity names: {', '.join(result['entity_names'][:5])}")

        return {
            "model": model_name,
            "success": True,
            "latency_ms": elapsed,
            "entities": len(result.get("entity_names", [])),
            "relationships": len(result.get("relationships", [])),
            "has_summary": bool(result.get("summary")),
        }

    except Exception as e:
        print(f"\n✗ Extraction failed: {e}")
        import traceback

        traceback.print_exc()
        return {
            "model": model_name,
            "success": False,
            "error": str(e),
        }
    finally:
        settings.entity_extraction_model = original_model


async def main():
    """Run extraction tests with multiple models and conversations."""
    # Test conversations (simple to complex)
    test_cases = [
        {
            "name": "Simple Tech Discussion",
            "user": "Tell me about Python programming",
            "assistant": "Python is a high-level programming language created by Guido van Rossum. It's known for its simplicity, readability, and extensive standard library. Python is widely used in web development, data science, machine learning, and automation.",
        },
        {
            "name": "Multi-Entity Conversation",
            "user": "I'm planning a trip to Tokyo and Kyoto in Japan. What should I know about the culture?",
            "assistant": "Tokyo and Kyoto offer different experiences in Japan. Tokyo is the modern capital with skyscrapers and technology, while Kyoto is known for traditional temples and gardens. Japanese culture values respect, punctuality, and politeness. You should learn basic phrases, understand bowing etiquette, and be mindful in temples and shrines.",
        },
        {
            "name": "Complex Technical Discussion",
            "user": "Explain how Neo4j graph databases differ from PostgreSQL relational databases for storing knowledge graphs",
            "assistant": "Neo4j and PostgreSQL serve different purposes. Neo4j is a native graph database optimized for relationship traversal using Cypher query language. It stores nodes and relationships as first-class citizens, making it ideal for knowledge graphs with complex connections. PostgreSQL is a relational database using SQL and tables with foreign keys to represent relationships. For knowledge graphs with many-to-many relationships and deep traversals, Neo4j performs better. PostgreSQL excels at structured tabular data with ACID guarantees.",
        },
    ]

    # Models to test
    models = [
        "qwen3-8b",  # Default reasoning model
        "lfm2.5-1.2b",  # Fast experimental model
    ]

    results = []

    for test_case in test_cases:
        print(f"\n\n{'#' * 80}")
        print(f"# Test Case: {test_case['name']}")
        print(f"{'#' * 80}")

        for model in models:
            result = await test_extraction(
                model_name=model,
                user_msg=test_case["user"],
                assistant_msg=test_case["assistant"],
            )
            result["test_case"] = test_case["name"]
            results.append(result)

            # Small delay between tests
            await asyncio.sleep(1)

    # Summary report
    print(f"\n\n{'=' * 80}")
    print("SUMMARY REPORT")
    print(f"{'=' * 80}")

    for model in models:
        model_results = [r for r in results if r["model"] == model]
        successful = [r for r in model_results if r.get("success")]

        if successful:
            avg_latency = sum(r["latency_ms"] for r in successful) / len(successful)
            avg_entities = sum(r["entities"] for r in successful) / len(successful)
            avg_relationships = sum(r["relationships"] for r in successful) / len(successful)

            print(f"\n{model}:")
            print(f"  Success rate: {len(successful)}/{len(model_results)}")
            print(f"  Avg latency: {avg_latency:.0f}ms")
            print(f"  Avg entities: {avg_entities:.1f}")
            print(f"  Avg relationships: {avg_relationships:.1f}")
        else:
            print(f"\n{model}:")
            print("  ✗ All tests failed")

    # Save detailed results
    output_file = "/tmp/entity_extraction_test_results.json"
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n✓ Detailed results saved to: {output_file}")

    # Return summary for caller
    return results


if __name__ == "__main__":
    print("Starting entity extraction runtime tests...")
    print("Prerequisites: SLM server running on port 8000")
    print()

    results = asyncio.run(main())

    # Exit code based on success
    all_success = all(r.get("success", False) for r in results)
    exit(0 if all_success else 1)
