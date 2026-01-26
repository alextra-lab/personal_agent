"""Quick test with LFM 1.2B - the fast model that worked."""

import asyncio
from datetime import datetime

from personal_agent.config.settings import get_settings
from personal_agent.second_brain.entity_extraction import extract_entities_and_relationships


async def test_lfm():
    """Test LFM 1.2B extraction."""
    settings = get_settings()
    settings.entity_extraction_model = "lfm2.5-1.2b"

    print(f"Testing with: {settings.entity_extraction_model}\n")

    test_cases = [
        ("Tell me about Python", "Python is a programming language"),
        ("I'm learning Django and FastAPI", "Both are excellent Python web frameworks"),
        ("Trip to Tokyo and Kyoto", "Tokyo is modern, Kyoto is traditional Japanese city"),
    ]

    for user, assistant in test_cases:
        start = datetime.now()
        result = await extract_entities_and_relationships(user, assistant)
        elapsed = (datetime.now() - start).total_seconds() * 1000

        print(f"User: {user}")
        print(f"  Latency: {elapsed:.0f}ms")
        print(f"  Entities: {result.get('entity_names', [])}")
        print(f"  Relationships: {len(result.get('relationships', []))}")
        print(f"  Summary: {result.get('summary', '')[:80]}...")
        print()


if __name__ == "__main__":
    asyncio.run(test_lfm())
