# Self-Analysis Stream Model Assignment

**Date**: 2026-03-17
**Type**: Spec amendment + plan guard
**Status**: Approved

## Problem

The Cognitive Architecture Redesign v2 spec (Section 4.1) states "The 35B
model is the single reasoning center. Everything flows through it." This
language could be misread to mean background self-analysis streams (entity
extraction, Captain's Log reflection, insights analysis) should also run on
the primary model.

In practice, these streams are offloaded to a cloud model (`claude_sonnet`)
via configurable `models.yaml` process-role assignments because the current
single-GPU hardware cannot serve user-facing requests and background analysis
concurrently. This limitation is hardware-dependent, not architectural. The
config-switch mechanism must be preserved so these streams can return to local
models when hardware evolves.

## Design Decision

1. **Amend spec Section 4.1** to clarify that "single reasoning center" applies
   to the user-facing request path only. Add Section 4.1.1 documenting the
   self-analysis stream model assignment mechanism, the hardware rationale for
   separation, and an explicit invariant that Slice 1 must preserve it.

2. **Add guard-rail tests to Slice 1 plan (Task 11)** that verify the mechanism
   without hardcoding provider values. Tests assert:
   - Process-role keys resolve to valid model entries in the registry
   - The two consumers that currently implement LLM dispatch (`entity_extraction.py`,
     `reflection.py`) reference the configurable role key (not a hardcoded ModelRole)
     and branch on the `.provider` field for dispatch
   - `insights/engine.py` is noted as not yet using LLM-based analysis; when it
     does, it must follow the same pattern

## Changes Made

| File | Change |
|------|--------|
| `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` | Section 4.1 text amended; Section 4.1.1 added |
| `docs/superpowers/plans/2026-03-16-slice-1-foundation.md` | Task 11 Step 6 added (guard-rail tests); old Step 6 renumbered to Step 7 with new acceptance criterion |

## Key Constraint

Tests verify the **mechanism** (configurable keys + provider-based dispatch),
not specific values. Changing `entity_extraction_role` from `claude_sonnet` to
`reasoning` or a future local model entry must not break any test.
