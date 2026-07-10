# ADR-0114 study substrate (FRE-838)

Isolated Neo4j+GDS sandbox for the decoupled heterarchical associative-memory
research study (`docs/architecture_decisions/ADR-0114-heterarchical-associative-memory-study.md`,
decision D1). Never wired to prod; see `docker-compose.study.yml` and
`scripts/study/verify_isolation.py`.

## Stand up the substrate

```
make study-infra-up      # Neo4j+GDS on bolt://localhost:7691 (http :7478)
make study-infra-ps       # status
make study-infra-down     # stop
make study-infra-reset    # stop + wipe volume (full reset)
```

Requires `STUDY_NEO4J_PASSWORD` in `.env` — distinct from `NEO4J_PASSWORD`
(prod) by design.

**Resources (fix-forward, 2026-07-10):** master's first live corpus-load
attempt OOM-killed `neo4j-study` at the original 1.5g mem_limit / 1g heap —
the real prod corpus (10,290 nodes / 34,301 relationships) plus the GDS
plugin needs real headroom. Bumped to `mem_limit: 4g` / heap `2g` /
pagecache `512m` in `docker-compose.study.yml`; re-verified end-to-end
against a prod-scale synthetic corpus (see `tests/scripts/study/test_export_snapshot_scale.py`).

## Verify isolation (AC-5(1))

```
uv run python scripts/study/verify_isolation.py
```

Confirms: the compose file has no network leak paths (`network_mode`,
`extra_hosts`, `cloud-sim` membership), and a container on
`seshat-study-net` cannot resolve or reach prod's Neo4j/Postgres/
Elasticsearch (DNS + TCP probes). Also exercised as
`tests/scripts/study/test_verify_isolation.py` (static checks run in
`make test`; the runtime DNS/TCP probes are marked `integration` and need
`make study-infra-up` first).

## Export the frozen corpus (AC-5(2) + the corpus itself)

```
uv run python scripts/study/export_snapshot.py                # dry run — counts only
uv run python scripts/study/export_snapshot.py --execute       # real prod read + sandbox write
```

Both the direct-path form above and `uv run python -m scripts.study.export_snapshot [--execute]`
work (fix-forward, 2026-07-10: the direct-path form originally crashed with
`ModuleNotFoundError` on the deferred `scripts.study.config` import — direct
execution doesn't put the repo root on `sys.path` the way `-m` invocation
does; the script now bootstraps `sys.path` itself when run directly). Run
from the repo root either way.

Reads prod Neo4j (`Entity`/`Turn`/`Session`/`Person`/`Agent`/`Claim`/
`Location`/`EntityDescriptionVersion` + their relationships) and prod
Postgres `sessions.messages` (conversation traces) **read-only**, writes a
1:1 copy into the study sandbox, and writes `scripts/study/snapshots/snapshot_manifest.json`
(snapshot date, content hash over the full corpus including traces, node/
relationship/session counts — gitignored, no raw content).

**Safety:** refuses to write unless `--execute` is passed, and refuses to
run (even with `--execute`) unless `STUDY_NEO4J_URI` positively resolves to
the study substrate (`localhost:7691`) — never prod. A relationship whose
endpoint node wasn't resolvable (e.g. prod wasn't perfectly quiesced between
the node-read and relationship-read passes) is skipped, logged, and counted
in the manifest's `skipped_relationships` field, rather than aborting the
whole run — check that field is `0` after a real run; a non-zero count means
the frozen corpus is missing some edges and the run should be investigated
(likely re-run during a quieter window).

**AC-5(2) proof (zero prod deltas):** capture prod node/relationship totals
immediately before and after the `--execute` run (`count_nodes_and_relationships`
in `export_snapshot.py`, or `MATCH (n) RETURN count(n)` / `MATCH ()-[r]->()
RETURN count(r)` in `cypher-shell`) and confirm they're identical — the read
performs no writes, so a quiet window (no concurrent live turns) makes the
before/after counts a clean zero-delta proof.

**If the live export has not been run yet** (e.g. the go-ahead was
deferred): the study sandbox stands but has no corpus, and FRE-839/840/841
remain blocked until this command runs against real production data.
