# Runbook: adopt the OVH-managed Qwen3-Embedding-8B embedder

**Backing:** ADR-0112 §D4/§D6, AC-5, AC-6. **Owner:** master (this touches the
live serving stack — the build session that shipped the mechanism, FRE-821,
cannot execute any of this).

**Precondition:** FRE-817's corpus A/B decided the embedder (OVH-managed
Qwen3-Embedding-8B, nDCG@5 0.9566 vs the deployed 0.6B's 0.9303). FRE-821 shipped
the `managed_embedder` substrate profile (`config/substrate.yaml`), the
managed-call + same-model local-fallback runtime path
(`src/personal_agent/memory/embeddings.py`), and the static identity guard
(`config_guard.check_embedding_fallback_identity`). None of the steps below have
run yet — the default deployed profile is still `private` (local 0.6B).

## What this adoption changes

- The embedder becomes the OVH AI Endpoints Qwen3-Embedding-8B (managed).
- A same-model local instance becomes the failover (D4's "seamless local
  fallback" — same weights revision, so no re-embed on failover).
- Storage (Postgres/Neo4j/Elasticsearch) is **untouched** — ADR-0112 D5 keeps it
  on the VPS. Do not select the plain `managed` substrate profile for this; use
  `managed_embedder`.

## Sequence

1. **Provision the local-8B fallback.** Download/mount the Qwen3-Embedding-8B
   GGUF weights (mirrors the existing `cloud-sim-embeddings` container's 0.6B
   setup in `docker-compose.cloud.yml`, same `--pooling last` flag — pooling
   must match the OVH endpoint's, per AC-6). Bring it up on a **new** port/service
   (do not replace the running 0.6B container yet — it still serves live traffic
   until step 6). Confirm it answers `/v1/embeddings`.

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

4. **Flip the profile and dimension together, in the same deploy** (splitting
   these across two deploys would boot with a dimension/index mismatch):
   ```
   AGENT_SUBSTRATE_PROFILE=managed_embedder
   AGENT_EMBEDDING_DIMENSIONS=4096
   ```
   `MemoryService.ensure_vector_index()` will drop/recreate `entity_embedding` at
   the new width on boot — this is expected and coordinated with step 2's
   re-embed, not an accident to guard against.

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

6. **Stop the old 0.6B container** (`cloud-sim-embeddings` in
   `docker-compose.cloud.yml`) once step 5 passes.

7. **Verify AC-5 live:**
   - `docker ps` (or `make ps`) — confirm no embedder container runs on the host.
   - `free -h` before/after — host free RAM should rise by roughly the old
     container's resident footprint (~2.8 GiB).
   - `sar -r` / `%commit` — confirm `%commit` stays below 100% under standard
     load. Check this **separately** from swap-present / test-stack-reclaimed
     (ADR-0112 AC-5 requires these as distinct sub-checks, not a single
     RAM-went-up observation).

## Rollback

Revert `AGENT_SUBSTRATE_PROFILE` to `private` and `AGENT_EMBEDDING_DIMENSIONS`
to `1024`, redeploy. Keep the old 0.6B container/image available until AC-5/AC-6
are confirmed live — do not delete it in the same change that flips the profile.
