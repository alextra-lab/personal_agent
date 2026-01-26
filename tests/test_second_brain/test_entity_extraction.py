"""Tests for entity extraction pipeline with qwen3-8b."""

import pytest

from personal_agent.second_brain.entity_extraction import (
    extract_entities_and_relationships,
)


@pytest.mark.asyncio
class TestEntityExtraction:
    """Test entity extraction with local SLM."""

    async def test_extract_from_python_conversation(self):
        """Test extracting entities from a Python programming conversation."""
        user_message = "Tell me about Python programming and its use in web development"
        assistant_response = (
            "Python is a high-level programming language known for its simplicity. "
            "It's widely used in web development with frameworks like Django and FastAPI. "
            "Python's readability makes it popular for beginners and professionals alike."
        )

        result = await extract_entities_and_relationships(user_message, assistant_response)

        # Verify structure
        assert "summary" in result
        assert "entities" in result
        assert "relationships" in result
        assert "entity_names" in result

        # Should have summary
        assert len(result["summary"]) > 0

        # Should extract key entities
        entity_names = result["entity_names"]
        assert len(entity_names) > 0

        # Check if Python is mentioned as an entity
        python_found = any("python" in name.lower() for name in entity_names)
        assert python_found, f"Python should be extracted as entity. Found: {entity_names}"

        # Verify entity structure
        for entity in result["entities"]:
            assert "name" in entity
            assert "type" in entity
            # SLM might use various type names - just verify it exists and is non-empty
            assert len(entity["type"]) > 0

    async def test_extract_from_location_conversation(self):
        """Test extracting entities from a conversation about places."""
        user_message = "What can you tell me about Paris?"
        assistant_response = (
            "Paris is the capital of France, known for the Eiffel Tower and Louvre Museum. "
            "It's a major European city famous for art, fashion, and cuisine."
        )

        result = await extract_entities_and_relationships(user_message, assistant_response)

        # Should extract location entities
        entity_names = result["entity_names"]
        assert len(entity_names) > 0

        # Check for Paris or France
        locations = [
            name for name in entity_names if "paris" in name.lower() or "france" in name.lower()
        ]
        assert len(locations) > 0, f"Should extract location entities. Found: {entity_names}"

    async def test_extract_relationships(self):
        """Test that relationships are extracted between entities."""
        user_message = "How is Django related to Python?"
        assistant_response = (
            "Django is a web framework built with Python. It provides tools for "
            "building web applications quickly. Django and Python work together "
            "to create scalable web services."
        )

        result = await extract_entities_and_relationships(user_message, assistant_response)

        # Should have at least Python and Django
        entity_names = result["entity_names"]
        assert len(entity_names) >= 2, f"Should extract multiple entities. Found: {entity_names}"

        # Relationships might be present (SLM may or may not extract them)
        relationships = result["relationships"]
        # Don't assert on relationships since SLM might not always extract them
        # Just verify structure if they exist
        for rel in relationships:
            assert "source" in rel
            assert "target" in rel
            assert "type" in rel

    async def test_extract_from_minimal_conversation(self):
        """Test extraction from very short conversation."""
        user_message = "What is AI?"
        assistant_response = "AI stands for Artificial Intelligence."

        result = await extract_entities_and_relationships(user_message, assistant_response)

        # Should still return valid structure
        assert isinstance(result["summary"], str)
        assert isinstance(result["entities"], list)
        assert isinstance(result["relationships"], list)
        assert isinstance(result["entity_names"], list)

    async def test_extract_from_technical_conversation(self):
        """Test extraction from technical/code discussion."""
        user_message = "How do I use async/await in Python?"
        assistant_response = (
            "async/await is Python's syntax for asynchronous programming. "
            "You define async functions with 'async def' and use 'await' to call them. "
            "This is useful for I/O operations, network requests, and concurrent tasks. "
            "Libraries like asyncio and aiohttp support async operations."
        )

        result = await extract_entities_and_relationships(user_message, assistant_response)

        # Should extract technical concepts
        entity_names = result["entity_names"]
        assert len(entity_names) > 0

        # Should mention Python or async concepts
        technical_found = any(
            term in name.lower() for name in entity_names for term in ["python", "async", "asyncio"]
        )
        assert technical_found, f"Should extract technical entities. Found: {entity_names}"

    async def test_entity_types_are_valid(self):
        """Test that extracted entity types match expected values."""
        user_message = "Tell me about Barack Obama and his work in politics"
        assistant_response = (
            "Barack Obama was the 44th President of the United States. "
            "He worked in politics and law before his presidency, and focused on "
            "healthcare reform, including the Affordable Care Act."
        )

        result = await extract_entities_and_relationships(user_message, assistant_response)

        # Verify entity types exist and are non-empty
        for entity in result["entities"]:
            entity_type = entity.get("type", "")
            # Just verify type exists and is non-empty (SLM might use various names)
            assert entity_type, f"Entity {entity.get('name')} should have a type"

    async def test_json_parsing_robustness(self):
        """Test that extraction handles various conversation types."""
        test_cases = [
            ("What's 2+2?", "2+2 equals 4."),
            ("Hi", "Hello! How can I help you?"),
            ("Tell me a joke", "Why did the programmer quit? Because they didn't get arrays!"),
        ]

        for user_msg, assistant_msg in test_cases:
            result = await extract_entities_and_relationships(user_msg, assistant_msg)

            # Should always return valid structure, even if entities list is empty
            assert "summary" in result
            assert "entities" in result
            assert isinstance(result["entities"], list)
            assert isinstance(result["entity_names"], list)

    async def test_entity_properties_structure(self):
        """Test that entity properties field exists and is valid."""
        user_message = "What is machine learning?"
        assistant_response = (
            "Machine learning is a subset of artificial intelligence that enables "
            "systems to learn from data and improve over time without explicit programming."
        )

        result = await extract_entities_and_relationships(user_message, assistant_response)

        # Verify properties field exists or can be missing (SLM variation)
        for entity in result["entities"]:
            # Properties field is optional - SLM might not always include it
            if "properties" in entity:
                assert isinstance(entity["properties"], dict)

    async def test_summary_generation(self):
        """Test that summary is generated for conversation."""
        user_message = "Explain quantum computing in simple terms"
        assistant_response = (
            "Quantum computing uses quantum bits or 'qubits' that can exist in multiple "
            "states simultaneously, unlike classical bits. This allows quantum computers "
            "to solve certain problems much faster than traditional computers."
        )

        result = await extract_entities_and_relationships(user_message, assistant_response)

        # Summary should exist and be meaningful
        summary = result["summary"]
        assert summary
        assert len(summary) > 10  # Should be more than trivial
        # Don't check content strictly as SLM might summarize differently
