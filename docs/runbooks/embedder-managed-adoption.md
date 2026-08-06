# Runbook: adopt the OVH-managed Qwen3-Embedding-8B embedder

**Backing:** ADR-0112 §D4/§D6, AC-5, AC-6. **Owner:** master (this touches the
live serving stack — the build session that shipped the mechanism, FRE-821,
cannot execute any of this).

**Precondition:** FRE-817's corpus A/B decided the embedder (OVH-managed
Qwen3-Embedding-8B, nDCG@5 0.9566 vs the deployed 0.6B's 0.9303). FRE-821 shipped
the `managed_embedder` substrate profile (`config/substrate.yaml`), the
managed-call + same-model local-fallback runtime path
(`src/personal_agent/memory/embeddings.py`), and the static identity guard
(`config_guard.check_embedding_fallback_identity`).

**Stale as of FRE-1166 (2026-08-06):** `config/models.yaml`'s `embedding:` entry
was pointed directly at the OVH endpoint on 2026-07-19 (`ba81b8985`), bypassing
this runbook's `managed_embedder` profile flip — every substrate profile now
resolves `model_endpoint:embedding` to OVH regardless of `AGENT_SUBSTRATE_PROFILE`.
The steps below (re-embed, AC-5/AC-6 live verification, same-model local-8B
fallback) were never run against that switch — there is currently no verified
fallback for the managed embedder. Flagged to master/owner as a discovered gap,
not resolved here.

## What this adoption changes

- The embedder becomes the OVH AI Endpoints Qwen3-Embedding-8B (managed).
- A same-model local instance becomes the failover (D4's "seamless local
  fallback" — same weights revision, so no re-embed on failover).
- Storage (Postgres/Neo4j/Elasticsearch) is **untouched** — ADR-0112 D5 keeps it
  on the VPS. Do not select the plain `managed` substrate profile for this; use
  `managed_embedder`.

## Sequence

1. **Provision the local-8B fallback.** Download/mount the Qwen3-Embedding-8B
   GGUF weights and bring up a new llama.cpp service (`--pooling last` — pooling
   must match the OVH endpoint's, per AC-6). FRE-1166 retired the old 0.6B
   `cloud-sim-embeddings` container this step used to mirror; write the new
   service definition fresh rather than adapting the deleted one. Confirm it
   answers `/v1/embeddings`.

2. **One-time corpus re-embed.** Per ADR-0112 D6: spin up an owner-account
   ephemeral GPU (OVH/Scaleway L4, ~€0.75/hr) OR let the OVH-managed endpoint do
   the embedding pass. Re-embed every `Entity`/`Turn` node at the new dimension.
   This is the one-way door — do not skip it before flipping the env vars below
   (the vector index will otherwise silently mismatch until repopulated).

3. **Set the managed-embedder secrets** (via the existing `pass`-backed secrets
   flow, not committed anywhere):
   ```
   AGENT_MANAGED_EMBEDDING_ENDPOINT=<OVH AI Endpoints base URL>
   AGENT_MANAGED_EMBEDDING_TOKEN=<OVH bearer token>
   AGENT_MANAGED_EMBEDDING_MODEL=Qwen3-Embedding-8B
   AGENT_LOCAL_FALLBACK_EMBEDDING_ENDPOINT=<step-1 local-8B endpoint>
   AGENT_LOCAL_FALLBACK_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-8B
   ```

4. **Flip the profile:**
   ```
   AGENT_SUBSTRATE_PROFILE=managed_embedder
   ```
   `AGENT_EMBEDDING_DIMENSIONS` stays at its default `1024` — that is the
   measured MRL sweet spot for the 8B model (nDCG@5 peaks at 1024, beating
   native 4096 — FRE-826), not a value you set to the 8B's native width. Since
   the width is unchanged from what's already indexed,
   `MemoryService.ensure_vector_index()` will **not** drop/recreate
   `entity_embedding` on boot — the index shape stays put; only its *contents*
   change, via step 2's re-embed.

5. **Verify AC-6 live**, before removing the old container:
   ```bash
   uv run python -m scripts.eval.fre821_embedder_failover_probe.probe cosine \
     --fallback-endpoint <step-1 local-8B endpoint>
   uv run python -m scripts.eval.fre821_embedder_failover_probe.probe retrieval-overlap \
     --fallback-endpoint <step-1 local-8B endpoint>
   ```
   Both must print `[PASS]` (cosine ≥ 0.999 min pairwise; retrieval overlap ≥
   0.95 mean top-10). If either fails, **do not proceed to step 6** — investigate
   pooling/normalization/revision drift between the two endpoints first.

6. The old 0.6B container is already gone (FRE-1166) — nothing to stop here.

7. **Verify AC-5 live:**
   - `docker ps` (or `make ps`) — confirm no embedder container runs on the host.
   - `free -h` before/after — host free RAM should rise by roughly the old
     container's resident footprint (~2.8 GiB).
   - `sar -r` / `%commit` — confirm `%commit` stays below 100% under standard
     load. Check this **separately** from swap-present / test-stack-reclaimed
     (ADR-0112 AC-5 requires these as distinct sub-checks, not a single
     RAM-went-up observation).

## Rollback

Revert `AGENT_SUBSTRATE_PROFILE` to `private`, redeploy (`AGENT_EMBEDDING_DIMENSIONS`
needs no change — it never left its default `1024`). The old 0.6B container is
retired (FRE-1166); rollback falls back to whatever `model_endpoint:embedding`
resolves to in `config/models.yaml` (currently OVH — see the stale-precondition
note above), not to a local container.
