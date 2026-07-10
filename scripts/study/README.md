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

Reads prod Neo4j (`Entity`/`Turn`/`Session`/`Person`/`Agent`/`Claim`/
`Location`/`EntityDescriptionVersion` + their relationships) and prod
Postgres `sessions.messages` (conversation traces) **read-only**, writes a
1:1 copy into the study sandbox, and writes `scripts/study/snapshots/snapshot_manifest.json`
(snapshot date, content hash over the full corpus including traces, node/
relationship/session counts — gitignored, no raw content).

**Safety:** refuses to write unless `--execute` is passed, and refuses to
run (even with `--execute`) unless `STUDY_NEO4J_URI` positively resolves to
the study substrate (`localhost:7691`) — never prod.

**AC-5(2) proof (zero prod deltas):** capture prod node/relationship totals
immediately before and after the `--execute` run (`count_nodes_and_relationships`
in `export_snapshot.py`, or `MATCH (n) RETURN count(n)` / `MATCH ()-[r]->()
RETURN count(r)` in `cypher-shell`) and confirm they're identical — the read
performs no writes, so a quiet window (no concurrent live turns) makes the
before/after counts a clean zero-delta proof.

**If the live export has not been run yet** (e.g. the go-ahead was
deferred): the study sandbox stands but has no corpus, and FRE-839/840/841
remain blocked until this command runs against real production data.
