# FRE-821 — Adopt OVH Qwen3-Embedding-8B + same-model local fallback [AC-5/AC-6]

Backing: ADR-0112 §D4/§D6, AC-5, AC-6. Blocked-by FRE-816 (seam, Done) and FRE-817
(corpus A/B, Done — winner: OVH-managed Qwen3-Embedding-8B, nDCG@5 0.9566 vs the
deployed 0.6B's 0.9303, both open-weight so the pre-registered margin gate did not
apply but the 8B model is still the measured winner).

**Revised after codex plan-review (2026-07-06)** — see "Codex findings addressed"
below for what changed from the first draft.

## Scope decision: new `managed_embedder` profile, `private`/storage untouched

The currently-deployed `private`/default embedder (Qwen3-Embedding-0.6B, local
llama.cpp, 1024-dim) stays **exactly as it is today** — no change to
`config/models.yaml` / `config/models.cloud.yaml`'s `embedding` role. Reasons:

1. `MemoryService.ensure_vector_index()` auto-drops/recreates the Neo4j vector index
   whenever `settings.embedding_dimensions` changes — flipping the default *now*
   would empty the live index before any re-embed has run. That is exactly the kind
   of live-serving-stack action the ticket reserves for master ("Deploy touches the
   live serving stack → master authorizes/executes").
2. AC-5 wants the *managed* embedder to become primary and the embedder container
   removed from the host — an opt-in state, not the default one.

**Codex finding (blocking, addressed):** the existing `config/substrate.yaml`
`managed` profile flips **every** component — postgres/neo4j/elasticsearch too —
to managed. ADR-0112 D5 explicitly says keep stores on the VPS. Reusing the
all-managed `managed` profile for embedder adoption would be wrong: it would also
select managed Postgres/Neo4j/ES, which nobody wants and no `managed_database_url`
etc. are even configured for this owner. **Fix:** add a new substrate profile,
`managed_embedder`, identical to `private` for every component except `embedder`
(which resolves managed, same as the `managed` profile's embedder row). This is
the actual "owner-controlled storage + reasonable-terms API endpoint" combination
ADR-0112 D3's `private` prose describes, expressed as its own selectable profile
rather than overloading either existing one.

## What this PR delivers (build-provable, offline)

- `config/substrate.yaml`: new `managed_embedder` profile (all D3 components,
  `embedder` managed / everything else local — mirrors `private` row-for-row
  except `embedder`).
- Runtime code that, under the `managed_embedder` profile, calls the OVH AI
  Endpoints Qwen3-Embedding-8B API (Bearer auth, ≤25-per-request batch chunking —
  the endpoint's confirmed limit per FRE-817) and falls back to a same-model local
  endpoint on failure — mirroring the FRE-817 harness's already-tested
  `_embed_ovh`/`_embed_ovh_batch` pattern, now in production code (using a
  dedicated exception, not `SystemExit` — that would escape the caller's
  `except Exception` and could kill the process; production code must never do
  that).
- Config plumbing: new `AppConfig` fields for the managed token/model and the
  local-fallback endpoint/model. **No new dimensions field** — `embedding_dimensions`
  stays the single shared knob (see "Dimensions" below). All resolved through
  `AppConfig` (never `os.getenv`), consistent with ADR-0112 D3.
- A static, CI-enforced identity guard: the managed model id and the
  local-fallback model id must name the exact same weights revision after
  provider-prefix normalization (AC-6's static half). Documents (does not
  code-check — see below) that normalization/pooling must also match.
- Zero behavior change for `private`/`dev`/`test` profiles (existing
  `tests/personal_agent/memory/test_embeddings.py` suite passes unmodified).
- A live probe script (ephemeral, off-host pattern per D6/FRE-817 precedent,
  credentials from `pass`, never committed) with **two** checks — AC-6 needs both:
  1. pairwise cosine over a ≥50-input fixed set (managed vs local-fallback), and
  2. top-k(10) retrieval overlap over the **existing** Neo4j index (managed vs
     local-fallback used as the query embedder, same downstream retrieval path).
  This PR cannot run either (no local-8B fallback exists yet to compare against),
  so it ships as the tool master runs at actual deploy time.
- A runbook documenting the exact master-side deploy sequence, including that
  `AGENT_EMBEDDING_DIMENSIONS` must be set to the 8B model's dimension in the same
  deploy step as the profile switch (see "Dimensions"), and what to measure for
  AC-5 (host free RAM, `%commit`) and AC-6 (live cosine/top-k overlap).

## Dimensions: reuse `embedding_dimensions`, do not add a second field

The first draft added a separate `managed_embedding_dimensions` field. Codex
correctly flagged this as insufficient: `embeddings.py` and
`service.py::ensure_vector_index` both read `settings.embedding_dimensions`
directly for the zero-vector length, the `dimensions=` request param, and the
Neo4j index width — a second, unconsumed field would prove nothing. A running
deployment only ever runs **one** substrate profile at a time, so there is only
ever one active embedder dimension at a time too. **Fix:** no new field. The
runbook instructs master to set `AGENT_EMBEDDING_DIMENSIONS=4096` in the same
deploy step as `AGENT_SUBSTRATE_PROFILE=managed_embedder` (this is what
intentionally triggers `ensure_vector_index`'s drop/recreate, coordinated with
the one-time re-embed — not an accident to guard against). Both the managed call
and the local-fallback call request `dimensions=settings.embedding_dimensions`,
so "same output dimension" holds by construction, not by a second config value
that could drift from the first.

## Identity guard: exact match, not fuzzy

First draft said "normalizes ... and flags a mismatch" without pinning down what
"normalize" means precisely — codex flagged this as too weak for AC-6's "exact
weights revision" bar. **Fix:** the guard strips an optional `"Qwen/"` (or
declared HF-org) prefix and requires **exact, case-sensitive** equality of what
remains (`Qwen3-Embedding-8B`) — not a fuzzy/substring match. Pooling/normalization
(llama.cpp's `--pooling last` flag on the container command line) is not an
AppConfig field and cannot be guard-checked in Python; it is a manual attestation
in the runbook, cross-checked live by the probe script's rank-order sanity check
(mirrors FRE-817's `_sanity_check_ovh`: relevant text must out-cosine irrelevant
text on both endpoints — a wrong pooling mode would fail this).

## What this PR does NOT do (explicitly out of scope, master's job)

- Does not download/provision the local Qwen3-Embedding-8B weights or container.
- Does not set `AGENT_SUBSTRATE_PROFILE=managed_embedder` in any deployed
  environment, nor `AGENT_EMBEDDING_DIMENSIONS=4096`.
- Does not run the one-time corpus re-embed.
- Does not stop the `cloud-sim-embeddings` container or measure live host RAM.
- Does not claim AC-5 or the dynamic half of AC-6 as proven by `make test` — these
  are handed to master's deploy runbook with exact verification commands.

## Files

1. `config/substrate.yaml` — add `managed_embedder` profile (private row-for-row,
   `embedder` swapped to the managed source/kind).
2. `src/personal_agent/config/settings.py` — add:
   - `managed_embedding_token: str | None` (secret)
   - `managed_embedding_model: str = "Qwen3-Embedding-8B"`
   - `local_fallback_embedding_endpoint: str | None = None`
   - `local_fallback_embedding_model: str = "Qwen/Qwen3-Embedding-8B"`
3. `src/personal_agent/memory/embeddings.py`:
   - `EmbeddingResponseError(Exception)` — replaces FRE-817's `SystemExit` for
     production use.
   - `_embed_managed_batch` / `_embed_managed` helpers (httpx, Bearer auth,
     ≤25-row chunking, order-preserving via response `index`, raise
     `EmbeddingResponseError`/`httpx.HTTPStatusError` on a bad response) —
     adapted from `scripts/eval/fre817_corpus_ab_embedder/corpus_ab.py`'s tested
     `_embed_ovh*`.
   - `generate_embedding` / `generate_embeddings_batch` branch on
     `resolve_substrate(settings.substrate_profile).backends["embedder"].kind`:
     - `"local"` → unchanged existing path.
     - `"managed"` → call `_embed_managed`; on any exception, if
       `local_fallback_embedding_endpoint` is set, retry once via the existing
       OpenAI-compatible path pointed at the fallback endpoint/model (structlog
       warning `embedding_managed_failover`); if that also fails (or fallback
       unset), fall through to the existing zero-vector fail-open path (unchanged
       final behavior, same log event as today).
4. `src/personal_agent/config/config_guard.py`:
   - `check_embedding_fallback_identity(root)` — policy-severity: exact-match
     (post prefix-strip) of `managed_embedding_model` vs
     `local_fallback_embedding_model`. Wired into `run_all_checks`.
5. `.env.example` — document the 4 new `AGENT_*` keys (commented, matching the
   existing `managed_*` block).
6. `docs/reference/CONFIG_INVENTORY.md` — regenerate via
   `uv run python scripts/audit/config_inventory.py generate` (paste into AUTOGEN
   block) then `... verify`.
7. New eval script (off-host, D6 pattern, gitignored output):
   `scripts/eval/fre821_embedder_failover_probe/probe.py` — two subcommands:
   `cosine` (≥50-input pairwise cosine, fail loud if mean < 0.999) and
   `retrieval-overlap` (top-k(10) overlap over the existing Neo4j index, fail
   loud if < 0.95). CLI shape mirrors FRE-817's `corpus_ab.py`. Credentials via
   `pass`.
8. `docs/runbooks/embedder-managed-adoption.md` — new: master's exact deploy
   sequence (provision fallback container → run re-embed → set
   `AGENT_SUBSTRATE_PROFILE=managed_embedder` + `AGENT_EMBEDDING_DIMENSIONS=4096`
   together → verify both probe subcommands → stop old container → measure RAM)
   + rollback (revert both env vars to the `private` defaults; old container
   stays until AC-5/AC-6 confirmed live).

## Tests (TDD — failing first)

- `tests/personal_agent/config/test_substrate_resolve.py` (extend): new
  `managed_embedder` profile resolves `embedder` to the managed target, every
  other component to the same target as `private`.
- `tests/personal_agent/memory/test_embeddings_managed.py` (new):
  - managed-profile call sends Bearer auth + chunks at 25 + preserves order
    (MockTransport, mirrors FRE-817's `test_fre817_embed_ovh.py`).
  - managed call raises + fallback endpoint set → fallback endpoint called, its
    vectors returned.
  - managed call raises + no fallback configured → existing zero-vector fail-open.
  - private/dev/test profiles: behavior byte-identical to today (regression guard).
- `tests/personal_agent/config/test_config_guard.py` (extend or new): identity
  guard catches a deliberate mismatch, passes on the default pairing.
- Existing `tests/personal_agent/memory/test_embeddings.py` must pass unmodified.

## Commands

```bash
make test-file FILE=tests/personal_agent/config/test_substrate_resolve.py
make test-file FILE=tests/personal_agent/memory/test_embeddings_managed.py
make test-file FILE=tests/personal_agent/memory/test_embeddings.py
make test-file FILE=tests/personal_agent/config/test_config_guard.py
make test
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Acceptance-criteria proof this PR can offer at the gate

- **AC-6 static half** (same model identity, config-pinned): `check_embedding_fallback_identity`
  test + CI gate.
- **AC-6 dynamic half** (≥50-input cosine ≥0.999, top-k(10) overlap ≥0.95) and
  **AC-5** (off-host relief, RAM): NOT provable without live deploy — explicitly
  handed to master via the runbook + two-subcommand probe script, with exact
  commands and thresholds documented in the final Linear comment.
