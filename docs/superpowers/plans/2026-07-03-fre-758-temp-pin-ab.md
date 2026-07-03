# FRE-758 — Pin extraction temperature near-0 and A/B against the FRE-630 baseline

**Ticket:** FRE-758 (Approved → In Progress) · **Project:** Memory Recall Quality
**Parent:** FRE-630 (KG extraction-quality SOTA program) · **Related:** FRE-759 (type/claim A/B, next in queue)
**Codex plan-review:** verdict *Ship with changes* — this revision folds in both findings (§Fix
step 1 corrected; §TDD gains a config-loader regression test).

---

## Root cause (confirmed by discovery + a live smoke test)

`extract_entities_and_relationships` (`src/personal_agent/second_brain/entity_extraction.py:449`)
already resolves `model_def` (line 496) but never passes `temperature` on the cloud path's
`cloud_client.respond(...)` call (line 529-534). The `gpt-5.4-mini` model entry (used solely by
`entity_extraction_role: gpt-5.4-mini`) has no `temperature` field set in either
`config/models.yaml` or `config/models.cloud.yaml`, so the call runs at the OpenAI provider
default (~1.0). `LiteLLMClient.respond` (`llm_client/litellm_client.py:322,412-413`) does accept
a `temperature` kwarg and forwards it *only if the caller passes one explicitly* — no
config-fallback (unlike `LocalLLMClient._do_request`, which does fall back to
`model_config.temperature`, `llm_client/client.py:248-250`). That asymmetry is why the local path
is unaffected but the cloud path (the one FRE-630 benchmarked) is not.

**Live smoke test (this session, `openai/gpt-5.4-mini` via litellm, 2 calls, ~$0.00005 total):**
`temperature=0.0` → `200 OK`, response `"OK"`. `temperature=1.0` → `200 OK`. **Confirmed:
`gpt-5.4-mini` accepts a temperature override; it is not a reasoning-tier model that rejects
non-default temperature.** This resolves the ticket's open verification question — no fallback
plan needed.

## Fix (surgical — one call site, two model entries, two files)

**Codex plan-review finding (folded in):** the original draft of this plan claimed the
`gpt-5.4-mini:` entry is "used only by `entity_extraction_role: gpt-5.4-mini`" in both config
files — true for `config/models.cloud.yaml` (prod + the FRE-630 benchmark, which forces
`AGENT_MODEL_CONFIG_PATH=config/models.cloud.yaml`), **false for `config/models.yaml`**, where
`entity_extraction_role: gpt-5.4-nano` (`config/models.yaml:36`) — `gpt-5.4-nano`, not `-mini`, is
the actual local-dev extraction role. Pinning only `gpt-5.4-mini` there would leave local dev
extraction running at provider-default temperature, silently missing the ticket's intent for that
environment. Fix: pin **both** roles' models, in both files.

1. **`config/models.cloud.yaml`** — add `temperature: 0.0` to the `gpt-5.4-mini:` entry (governs
   prod and the FRE-630 benchmark harness).
2. **`config/models.yaml`** — add `temperature: 0.0` to the `gpt-5.4-nano:` entry (the actual
   local-dev `entity_extraction_role`); also add `temperature: 0.0` to its `gpt-5.4-mini:` entry
   for config-shape parity with `models.cloud.yaml` (ADR-0099 / FRE-734 lesson: keep matching
   model entries in sync across both files even when a given file doesn't currently route to
   that entry — avoids the next drift surprise).
   In each file, the entry touched is used *only* by `entity_extraction_role`; the separate
   `compressor:` entry (also `id: "gpt-5.4-mini"`) is a distinct config key and is untouched, so
   blast radius stays exactly entity extraction.
3. **`src/personal_agent/second_brain/entity_extraction.py`** — add one kwarg to the existing
   `cloud_client.respond(...)` call (~line 529-534):
   ```python
   cloud_response = await cloud_client.respond(
       role=ModelRole.PRIMARY,
       messages=[{"role": "user", "content": prompt}],
       system_prompt=_EXTRACTION_SYSTEM_PROMPT,
       temperature=model_def.temperature if model_def else None,
       trace_ctx=SystemTraceContext.new("entity_extraction", session_id=session_id),
   )
   ```
   `model_def` is already resolved in scope; `ModelDefinition.temperature` already exists in the
   schema (`llm_client/models.py:123-128`, `ge=0.0, le=2.0`) — no schema change needed.

**Not in scope:** fixing the general `LiteLLMClient.respond` / `LocalLLMClient._do_request`
config-fallback asymmetry. Every other cloud model entry currently omits `temperature`, so the
asymmetry is latent; flagging as a follow-up ticket (Step 5) rather than widening this PR's blast
radius to every `LiteLLMClient` caller.

## TDD

**New unit test** in `tests/test_second_brain/test_entity_extraction_contract.py` (mirrors the
existing local-path mock pattern at line 144-159, but exercises the cloud path):

```python
async def test_cloud_path_passes_configured_temperature():
    """The cloud extraction call must forward model_def.temperature (FRE-758)."""
    with (
        patch("personal_agent.second_brain.entity_extraction.load_model_config") as mock_cfg,
        patch("personal_agent.llm_client.factory.get_llm_client") as mock_get_client,
    ):
        mock_model_def = SimpleNamespace(provider="openai", id="gpt-5.4-mini", temperature=0.0)
        mock_cfg.return_value.entity_extraction_role = "gpt-5.4-mini"
        mock_cfg.return_value.models = {"gpt-5.4-mini": mock_model_def}
        mock_client = mock_get_client.return_value
        mock_client.respond = AsyncMock(
            return_value={"content": orjson.dumps(_OPERATIONAL_MODEL_JSON).decode("utf-8")}
        )

        await extract_entities_and_relationships(_OPERATIONAL_USER_MSG, "assistant reply")

        assert mock_client.respond.call_args.kwargs["temperature"] == 0.0
```

- Run first (`make test-file FILE=tests/test_second_brain/test_entity_extraction_contract.py`) —
  confirm it **fails** (no `temperature` kwarg currently forwarded → `KeyError`/`None != 0.0`).
- Implement the two-line fix above.
- Re-run — confirm it **passes**.

**Second test (Codex finding — catches exactly the role-mapping mistake above, which the mocked
call-site test cannot see since it hand-supplies `model_def`):** a real config-loader assertion,
parametrized over both files, in `tests/test_config/test_model_loader.py` (or wherever existing
`load_model_config` tests live — confirm location first):

```python
@pytest.mark.parametrize("config_path", ["config/models.yaml", "config/models.cloud.yaml"])
def test_entity_extraction_role_has_pinned_temperature(config_path):
    """The real entity_extraction_role model must resolve temperature=0.0 (FRE-758)."""
    config = load_model_config(Path(config_path))
    role_model = config.models[config.entity_extraction_role]
    assert role_model.temperature == 0.0
```

This asserts through the *actual* YAML + role indirection, not a mock — it would have failed
against the original (uncorrected) plan's `models.yaml` change.

## A/B against the FRE-630 baseline (the ticket's actual acceptance criteria)

Baseline (`docs/research/2026-07-03-fre-630-extraction-quality-sota.md` Part 4, run-id
`baseline-20260703`, 27 gold cases × 3 samples, temp≈1.0 uncontrolled):

| metric | baseline mean±std |
|---|---:|
| entity_type_accuracy | 0.80±0.35 |
| claim_emission_recall | 0.33±0.47 |
| entity_recall | 0.90±0.23 |
| relationship_type_correctness | 0.89±0.30 |

Steps:
0. **Confound check (Codex finding):** the harness uses `os.environ.setdefault(...)` for
   `AGENT_MODEL_CONFIG_PATH`, so a pre-set shell env var would silently override the intended
   cloud config and re-benchmark the wrong file. Verified clean this session: `echo
   $AGENT_MODEL_CONFIG_PATH` → unset. Re-verify immediately before running.
1. `make test-infra-up`
2. `uv run python -m scripts.eval.fre630_extraction_quality.harness --run-id temp-pin-20260703 --samples 3`
   (same 27 cases, same prompt/matcher/gold-schema versions — temperature is the only variable)
3. Compare mean±std against the table above per-metric, focused on:
   - **std bands collapse materially** (same-input samples agree) — this is the primary claim
     the ticket makes about non-determinism.
   - **entity_type_accuracy does not regress**, target improves toward ≥0.95.
4. Append a curated summary (not raw JSON — gitignored per repo convention) to the research doc,
   Part 4, as a new dated subsection with the run-id and before/after table.
5. `make test-infra-down`

If entity_type_accuracy or claim_emission_recall do **not** move despite std collapsing, that is
still a valid, useful result (temperature was one candidate lever, not the only one — FRE-759
already queued for the prompt/DSPy angle) — record it plainly rather than reframing the AC.

## Acceptance criteria (proof of done)

| # | Criterion | Proof |
|---|-----------|-------|
| AC-1 | `gpt-5.4-mini` accepts a temperature override | Live smoke test this session (2 calls, both 200 OK) — recorded above |
| AC-2 | Cloud extraction call forwards `model_def.temperature` | New unit test, failing→passing |
| AC-3 | Per-metric std bands collapse materially vs baseline | A/B run `temp-pin-20260703` vs `baseline-20260703`, same 27×3 protocol |
| AC-4 | `entity_type_accuracy` does not regress (target ≥0.95) | Same A/B run, per-metric table |
| AC-5 | Both model config files' actual `entity_extraction_role` resolves `temperature == 0.0` | Config-loader test, parametrized over `config/models.yaml` (role: `gpt-5.4-nano`) and `config/models.cloud.yaml` (role: `gpt-5.4-mini`) — not just "the mini entry exists" |

## Follow-up ticket to file (Step 5)

`LiteLLMClient.respond` has no config-temperature fallback (unlike `LocalLLMClient`) — every
other cloud model entry currently has no `temperature` set so this is latent, but the next cloud
model config that sets `temperature` and relies on it being picked up automatically (rather than
threaded explicitly at every call site, as this ticket does) will silently get provider-default
behavior instead. File as Needs Approval under Memory Recall Quality once this PR lands.
