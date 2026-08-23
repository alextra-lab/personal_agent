"""Pin litellm's per-model capability data for tests (FRE-1007).

litellm resolves what a model can do from a cost/capability map it **fetches from
GitHub when the package is imported**, falling back to a bundled JSON when that
fetch fails. The bundled copy does not list newer models — it has no entry for
``claude-sonnet-5`` today — and in that state litellm reports every reasoning
parameter unsupported.

Any test that asserts on a reasoning transformation is therefore asserting on
whether the CI host had egress to GitHub at process start, which is not a
property anyone wants to test. These fixtures pin the flags in-repo so the
assertions mean what they say in both conditions.

The values are litellm's own, read from the live map on 2026-08-23; they are
pinned here as *test* data only. Production still reads the real map — the guard
deliberately reports "could not verify" rather than inventing capability facts
(``config_guard.check_reasoning_declaration``), and this helper must never be
used to make it invent them.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

#: Capability flags litellm's live map carries for the models this repo binds.
#: Only the keys the reasoning path reads are pinned; pricing is left alone.
_PINNED_CAPABILITIES: dict[str, dict[str, Any]] = {
    # `supports_adaptive_thinking` is the flag that selects litellm's adaptive
    # path over its legacy one; without it the same effort yields
    # `thinking: {type: enabled, budget_tokens: N}` and a rewritten `max_tokens`.
    # Copied from the live entry rather than reasoned about — guessing the
    # relevant subset produced exactly that wrong shape.
    "claude-sonnet-5": {
        "litellm_provider": "anthropic",
        "mode": "chat",
        "supports_reasoning": True,
        "supports_adaptive_thinking": True,
        "supports_output_config": True,
        "supports_xhigh_reasoning_effort": True,
        "supports_max_reasoning_effort": True,
    },
    "gpt-5.4-mini": {
        "litellm_provider": "openai",
        "mode": "chat",
        "supports_reasoning": True,
    },
}


@contextmanager
def pinned_litellm_capabilities() -> Iterator[None]:
    """Ensure litellm knows the models these tests assert on, then restore.

    Entries already present in the live map are left untouched — the point is to
    remove a dependency on the network, not to override litellm when it has real
    data. Only missing entries are added, and only for the duration of the block.
    """
    import litellm

    added: list[str] = []
    for model_id, flags in _PINNED_CAPABILITIES.items():
        if not litellm.model_cost.get(model_id):
            litellm.model_cost[model_id] = dict(flags)
            added.append(model_id)
    try:
        yield
    finally:
        for model_id in added:
            litellm.model_cost.pop(model_id, None)
