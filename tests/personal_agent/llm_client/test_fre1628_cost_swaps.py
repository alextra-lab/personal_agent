"""FRE-1628 — Haiku 5.5 deep mode and the measured cost-saving bindings.

Offline: ``reasoning_wire_shape`` asks litellm's own transformation what the
declared effort becomes on the wire, and sends nothing.
"""

from __future__ import annotations

from personal_agent.config import load_model_config
from personal_agent.config.config_guard import reasoning_wire_shape


class TestHaikuDeepMode:
    """AC-1: ``claude_haiku_5_5`` declares ``deep`` at effort high (ADR-0157 D5, note §8)."""

    def test_deep_mode_sends_effort_high_on_the_wire(self) -> None:
        """The mode's effort reaches Anthropic as adaptive thinking at ``high``."""
        definition = load_model_config().models["claude_haiku_5_5"]
        deep = definition.modes["deep"]
        assert deep.effort == "high"
        shape, error = reasoning_wire_shape(definition.id, "anthropic", {}, deep.effort)
        assert error is None
        assert shape["thinking"]["type"] == "adaptive"
        assert shape["output_config"] == {"effort": "high"}
        assert definition.default_mode == "default"

    def test_luna_has_no_deep_mode(self) -> None:
        """Luna at any effort above none sends tool calls to the refused Responses bridge."""
        assert "deep" not in load_model_config().models["gpt-6-luna"].modes
