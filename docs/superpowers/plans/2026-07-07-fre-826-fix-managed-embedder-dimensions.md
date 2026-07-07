# FRE-826 — Fix managed embedder ignoring embedding_dimensions [BUG]

Backing: ADR-0112 §D4/§D6 (same chain as FRE-821). Blocks FRE-821's one-way
re-embed deploy — must land before master runs it.

## Problem recap

`_embed_managed_batch` (`src/personal_agent/memory/embeddings.py`) sends
`{"model", "input"}` to the OVH endpoint with no `dimensions` param, so it
fetches OVH's native 4096-dim vector instead of the measured-sweet-spot 1024.
A dimension-controlled re-run of the FRE-817 corpus A/B (2026-07-07, live OVH
endpoint) shows the 8B model peaks at **1024** (nDCG@5 0.9585), beating both
4096 (0.9566) and 2048 (0.9552) — FRE-694's middle-dim finding transfers to
OVH cloud. `AGENT_EMBEDDING_DIMENSIONS` default is already `1024`
(`settings.py:522`) — the bug is that the managed-embed request never tells
OVH to honor it, and the local-fallback path has no client-side truncation
either, so a `managed_embedder` deploy would silently bake native-width
vectors into the KG.

## Root-cause detail: two independent gaps, two different fixes

1. **Managed (OVH) path** — OVH *does* honor the OpenAI `dimensions` request
   param (evidence in the ticket: verified live). The fix is to send it and
   then **strictly enforce** the response actually came back at that width
   (fail loud on any mismatch — if OVH silently ignored the param, that is a
   server-side regression worth surfacing immediately, not silently patching
   around).
2. **Local-fallback path** — the same-model local llama.cpp server is not
   known to honor the OpenAI `dimensions` param (unlike OVH), so it will
   return its native width (4096 for the 8B model) regardless of what we
   request. The fix there is **permissive client-side truncate + renormalize**
   (Matryoshka/MRL convention — the leading N components of an MRL-trained
   embedding are themselves a valid lower-dimensional embedding).

These are deliberately different enforcement policies, not one shared
helper applied uniformly — a length mismatch means something different in
each case (a managed misconfiguration vs. an expected local limitation).

## What does NOT change

- The primary **non-managed** local path (`private`/`dev`/`test` profiles,
  Qwen3-Embedding-0.6B, native 1024) is untouched — it was never broken (its
  native width already equals the target), and FRE-821 explicitly protected
  this path with a regression-guard test suite
  (`TestPrivateProfileUnaffected`). Threading the new dimension-enforcement
  logic through `_generate_vectors`'s managed/fallback branches only (not the
  `if _resolve_embedder_kind(settings) != "managed"` branch) keeps that
  guarantee intact.
- `settings.embedding_dimensions` stays the single shared knob — no second
  field (same reasoning FRE-821's plan already established).
- No deploy in this ticket. `AGENT_EMBEDDING_DIMENSIONS` is already `1024` by
  default; there is no env flip to make. Master's eventual `managed_embedder`
  deploy runbook changes (see below) because the corpus re-embed no longer
  also triggers a Neo4j vector-index width change (1024 → 1024 is a no-op for
  `ensure_vector_index()`).

## Code changes

### `src/personal_agent/memory/embeddings.py`

1. `_embed_managed_batch(texts, base_url, token, model, dimensions, client)`
   — new `dimensions` param (positional, before `client`):
   - Payload becomes `{"model": model, "input": texts, "dimensions": dimensions}`.
   - After building `ordered` vectors (existing row-count check unchanged),
     add: for each vector, `len(vec) != dimensions` → raise
     `EmbeddingResponseError` (new message, references the configured
     dimension and that the endpoint didn't honor the requested width).
   - Return `[_renormalize(vec) for vec in vectors]` instead of the raw
     vectors — OVH's server-side MRL truncation does not renormalize, so a
     truncated-but-not-renormalized vector would silently corrupt cosine
     similarity downstream.
2. `_embed_managed(..., *, client=None, dimensions: int | None = None)` — if
   `dimensions is None`, resolve `get_settings().embedding_dimensions` (keeps
   `scripts/eval/fre821_embedder_failover_probe/probe.py`'s existing
   no-dimensions call site working unchanged). Threads `dimensions` to every
   `_embed_managed_batch` call.
3. `_generate_vectors` — only the `managed` branch changes:
   - Pass `dimensions=settings.embedding_dimensions` to `_embed_managed`.
   - In the fallback branch (after `_call_embeddings_api` for
     `local_fallback_embedding_endpoint`), apply
     `_to_target_dimension(vec, settings.embedding_dimensions)` to each
     returned vector before returning.
   - The non-managed branch (`if _resolve_embedder_kind(settings) !=
     "managed"`) is untouched — byte-identical to today.
4. New helpers:
   - `_renormalize(vec: list[float]) -> list[float]` — L2-renormalize; raises
     `EmbeddingResponseError` on a zero vector (degenerate embedding, never
     silently score a zero).
   - `_to_target_dimension(vec: list[float], dimensions: int) -> list[float]`
     — truncate to `dimensions` (raise if `vec` is *shorter* than
     `dimensions` — a genuine failure, never zero-pad) then `_renormalize`.
     Mirrors `scripts/eval/fre435_memory_recall/separation_report.truncate_renormalize`'s
     convention (duplicated, not imported — `scripts/eval` is dev/eval
     tooling, not a runtime dependency of `src/`).
5. `EmbeddingResponseError` docstring: broaden from "row-count mismatch" to
   also cover dimension mismatches and degenerate (zero) vectors.

### `src/personal_agent/config/settings.py`

`embedding_dimensions` field description: currently says "1024 native for
Qwen3-Embedding-0.6B" only. Update to also note it's the measured MRL
sweet-spot for the managed Qwen3-Embedding-8B profile (peaks at 1024, not
native 4096 — FRE-826), since the same field now serves double duty by
design (no second field, per FRE-821's decision).

### `scripts/eval/fre821_embedder_failover_probe/probe.py`

`_embed_both()` currently returns `fallback_vecs` as raw, untruncated
vectors extracted directly from `_call_embeddings_api`'s response. Once the
managed path returns 1024-dim vectors (this fix) while the local-8B fallback
server still answers at its native width, `_cosine()`'s `zip(..., strict=True)`
and `run_retrieval_overlap`'s query against the (1024-dim) Neo4j index would
both break on a length mismatch. Fix: truncate+renormalize `fallback_vecs` to
`get_settings().embedding_dimensions` using
`scripts.eval.fre435_memory_recall.separation_report.truncate_renormalize`
(both live under `scripts/eval`, so importing it here is not a layering
violation the way importing it into `src/` would be). This script is
live-deploy-only (master runs it, build cannot) so it has no unit-test
coverage — this is a correctness fix, not new tested behavior.

**Codex finding (blocking, addressed):** `_embed_both()` also has a
pre-existing, independent bug it's worth fixing while this file is already
open — it only applies the query instruction-prefix to the fallback texts
(`fallback_texts = [f"{prefix}{t}" for t in texts]`, line ~86) and calls
`_embed_managed(list(texts), ...)` with the **raw, unprefixed** texts (line
79). `_embed_managed`'s contract assumes callers already mode-prefixed the
input (its docstring: "Already mode-prefixed input texts to embed"). Without
this fix, `run_retrieval_overlap` (which passes `mode="query"`) would compare
the managed side's *document-mode* embedding against the fallback side's
*query-mode* embedding — a formulation mismatch that has nothing to do with
dimensions but would silently corrupt the AC-6 overlap number the moment
someone runs this probe. Fix: build `fallback_texts` first, pass the same
prefixed list to both `_embed_managed` and `_call_embeddings_api`.

## Test changes (TDD — failing first, then made to pass)

`tests/personal_agent/memory/test_embeddings_managed.py`:

- **New**: `test_request_includes_dimensions_param` — asserts the payload
  `_embed_managed_batch` sends includes `dimensions == <configured>` (AC #1,
  first half).
- **New**: `test_raises_on_wrong_dimension` — a response vector at the wrong
  width (e.g. native 4096 when 1024 was requested) raises
  `EmbeddingResponseError` (AC #1, second half).
- **New**: `test_renormalizes_to_unit_length` — a non-unit-norm response
  vector comes back L2-normalized.
- **New**: `test_managed_path_returns_1024_unit_vectors` — end-to-end
  `generate_embedding` under the default `managed_embedder` settings
  (`embedding_dimensions=1024`) returns a 1024-length, unit-norm vector, and
  the request payload carries `dimensions: 1024` (AC #2).
- **Updated** `test_sends_bearer_auth`, `test_chunks_at_25`: add explicit
  `dimensions=<matches fixture vector length>` kwarg (previously unset,
  defaulted to nothing — now required to avoid tripping the new length
  check against the real default 1024).
- **Updated** `test_reorders_out_of_order_rows`: single-component fixture
  vectors degenerate under renormalization (a lone scalar always renormalizes
  to ±1, destroying the value the test uses to prove reordering). Switch to
  2-component vectors (`[1.0, float(i)]`) and assert on the
  renormalization-invariant ratio (`v[1] / v[0]`) instead of raw values.
- **Updated** `test_raises_on_truncated_response`, `test_raises_on_http_error`:
  add an explicit `dimensions=` kwarg (isolates the test from the real
  default settings; doesn't change what's being tested — both raise before
  the length check runs).
- **Updated** `test_managed_success_returns_managed_vectors`: override
  `embedding_dimensions=1` on the settings fixture (matches the existing
  1-element fake vector) and update the expected value to the renormalized
  `[1.0]` (a lone positive scalar renormalizes to unit sign).
- **Updated** `test_managed_failure_falls_back_to_local`: change the fake
  fallback vector from a 1-element stand-in to a native-4096 vector, and
  assert the returned embedding is truncated to 1024 and unit-norm — this
  test now actually exercises the bug this ticket fixes (previously it
  passed a 1-element vector straight through untouched, which is exactly the
  no-op behavior the ticket says is wrong).
- `TestPrivateProfileUnaffected`: unchanged (proves the non-managed path is
  untouched).

`tests/personal_agent/memory/test_embeddings.py`: no changes — confirms the
non-managed path is byte-identical (this file never touches
`_embed_managed`/managed-branch code).

## Commands

```bash
make test-file FILE=tests/personal_agent/memory/test_embeddings_managed.py
make test-file FILE=tests/personal_agent/memory/test_embeddings.py
make test
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Documentation updates

- `.env.example` (managed-embedder-adoption comment block, ~line 390-395):
  change `AGENT_EMBEDDING_DIMENSIONS=4096` → the value is already the
  default `1024`; rewrite the rationale — no index-width rebuild is
  triggered anymore (1024 → 1024 is a no-op for `ensure_vector_index()`),
  but the one-time corpus re-embed is still required (different model
  weights produce different vector *values* at the same width).
- `docs/runbooks/embedder-managed-adoption.md`: step 4 — remove
  `AGENT_EMBEDDING_DIMENSIONS=4096`; note the dimension is already correct
  at the default and does not need to be set. Remove the now-false "will
  drop/recreate `entity_embedding` at the new width" claim. Rollback section:
  drop the "revert `AGENT_EMBEDDING_DIMENSIONS` to 1024" step (nothing to
  revert).

## Acceptance-criteria proof this PR offers at the gate

- AC "unit test asserts the managed OVH request payload includes
  `dimensions == settings.embedding_dimensions`, and a returned vector of the
  wrong length fails loud" — `test_request_includes_dimensions_param` +
  `test_raises_on_wrong_dimension`.
- AC "with `AGENT_EMBEDDING_DIMENSIONS=1024`, `generate_embeddings` via the
  managed path returns 1024-dim unit vectors (not 4096)" —
  `test_managed_path_returns_1024_unit_vectors`.
- AC "managed and local-fallback paths return the same-length, same-space
  vectors at the configured dimension" — `test_managed_failure_falls_back_to_local`
  (now exercises a native-4096 fallback truncated to 1024) +
  `test_to_target_dimension`-level coverage via `test_renormalizes_to_unit_length`.

## Out of scope

- No deploy, no live OVH calls, no corpus re-embed — master's job per the
  runbook, once this lands.
- Not touching `config/substrate.yaml`, `config_guard.py`'s identity check,
  or the substrate profile mechanism — all correct already (FRE-816/821).
