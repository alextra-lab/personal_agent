"""Quick test with LFM 1.2B (router model) - the fast model that worked.

Uses entity_extraction_role from config/models.yaml. This script patches it to
'router' so the router model (e.g. LFM 1.2B) is used for extraction.
"""

import asyncio
from datetime import datetime
from unittest.mock import patch

from personal_agent.config import load_model_config
from personal_agent.second_brain.entity_extraction import extract_entities_and_relationships


async def test_lfm():
    """Test LFM 1.2B extraction (router role)."""
    real_config = load_model_config()
    test_config = real_config.model_copy(update={"entity_extraction_role": "router"})

    print("Testing with: entity_extraction_role=router (e.g. LFM 1.2B)\n")

    with patch("personal_agent.second_brain.entity_extraction.load_model_config", return_value=test_config):
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
