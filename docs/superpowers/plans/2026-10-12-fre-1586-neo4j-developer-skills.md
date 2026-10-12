# FRE-1586 — Three Neo4j skills as developer references, pinned to 5.26 Community

**Ticket:** FRE-1586. **Design intent:** the Neo4j Agent Skills study, proposal 2
(`docs/research/2026-10-11-neo4j-agent-skills-for-recall.md`, findings N1, N3, N5).
**Diff class:** docs and one test. No `src/` logic. Trivial tier, so no codex plan review.
**Deploy class:** none. The folders sit under `.claude/skills/` and no image copies them.

## Facts that the plan depends on

- Upstream is `neo4j-contrib/neo4j-skills` at `bb30e1f031af84a42fd2fdfd502ad52570e427cf` (MIT).
- The three skills hold 130 fenced Cypher blocks. The upstream default is the 2025 and 2026 dialect.
- Production is Neo4j 5.26.24 Community with APOC only. It refuses the version-25 dialect (study N3).
- `build-neo4j-test-1` runs the same version and plugin set. Iteration runs there. The AC-3 evidence
  run is the only production read.

## Decisions

1. **What is copied.** `SKILL.md` and `references/*.md` for each skill, except three things that stay out:
   - `README.md` — an install note for the upstream repository.
   - `scripts/*.py` of the cypher skill — third-party code that needs the `neo4j` driver and a password
     in the environment. The skill's own four-query schema step does the same job through `cypher-shell`.
   - `references/graph-type.md` — GRAPH TYPE DDL needs 2026.02 or later and is refused on Community.
2. **Frontmatter.** Keep `name` and `description`, and rewrite the description for the pin. Drop
   `version`, `compatibility` and `allowed-tools`. The last one pre-approves `Bash` and `WebFetch` for any
   seat that loads the skill, which a copied file must not do.
3. **Pin line.** The first body line of each `SKILL.md` names Neo4j 5.26 Community, Cypher 5, APOC only,
   no GDS, no GenAI procedures.
4. **Provenance.** Each folder holds `LICENSE` (the upstream MIT text, verbatim) and `UPSTREAM.md`
   (source URL, commit SHA, upstream skill version, the list of changes).
5. **Examples.** The server decides. Each fenced `cypher` block is a read example and runs in read mode,
   or its first line is `// WRITE - not run`. A block that the server rejects is rewritten to the
   Cypher 5 form or removed. Version tags are triaged the same way: a feature that 5.26 refuses leaves
   the file, and a feature that 5.26 accepts loses its 2025 or 2026 tag.
6. **Deliberately invalid examples.** A trap table row stays as inline text. A fenced block that shows
   code that must not run moves to a `text` fence with a caption. Reported in the handoff.

## Steps

1. Test first: `tests/docs/test_neo4j_developer_skills.py` covers AC-1, AC-2 and AC-4.
   `make test-file FILE=tests/docs/test_neo4j_developer_skills.py` → fails (folders absent).
2. Copy the files. Add `LICENSE` and `UPSTREAM.md`.
3. Edit: pin line, frontmatter, then mechanical removal of the version prefix and the 2026-only sections.
4. AC-3 loop on `build-neo4j-test-1`: extract every fenced `cypher` block, run each statement with
   `cypher-shell --access-mode read`, fix every failure, repeat until none remain.
5. Final AC-3 run on the production container, read mode, row counts only (the graph holds personal data;
   the output keeps no row values). The output goes on the ticket.
6. Gates: `make test-file` for the new test, `make test`, `make mypy`, `make ruff-check`,
   `make ruff-format`, `pre-commit run --all-files`. Commit, then self-review the committed diff.
7. Rebase on `origin/main`, push, open the PR, post the handoff comment.

## Acceptance criteria → proof

| AC | Proof |
|----|-------|
| AC-1 | `test_each_skill_has_the_pin_line_and_the_mit_notice` |
| AC-2 | `test_no_forbidden_token_in_any_skill_file` and a grep over the three folders |
| AC-3 | The run output of step 5, on the ticket |
| AC-4 | `test_each_skill_records_the_same_upstream_commit` |
