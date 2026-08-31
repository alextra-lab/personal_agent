# Last session — 2026-08-30 → 31

## Doing / discussing  (≤5 sentences)
The day began with the owner reporting `web_search` dead and ended with a live user losing work.
Search was restored end to end — the cause was not the engines but that **production SearXNG had been
reading its config from a build seat's git worktree**, so PR #995's repair never reached it. The
eval stack was made to boot for the first time (four separate blockers, each found by the previous
one failing), which finally let FRE-1337's arm 3 run and settle FRE-1288 with data. Then a deploy of
mine killed a user's turn and destroyed the NFL tool she'd had built; it was recovered from
telemetry. The open thread the owner raised last: **do we need a home for executable artifacts?**

## What was decided and why

**Three claims of mine were wrong and are retracted in writing on their tickets.** All the same
shape: reasoning from an absence, or from configuration, without checking the instrument pointed
where the writer writes. (1) *"The eval stack has never worked on this box"* — inferred history from
a deterministic failure; the owner corrected it. (2) *"A live production-contamination path"* —
the wiring trace was right but `event_bus_enabled` defaults to `False` and the eval compose never
set it, so eval had **never published**. Latent, not live; I escalated FRE-1342 to Urgent on that
overstatement. (3) *"The Captain's Log persists nothing"* — I queried Postgres tables **no code has
ever INSERTed into**; it writes to disk and ES, 2,121 reflection docs sitting there. A build seat
caught that one. Memory updated (`feedback_a_negative_result_is_probably_your_instrument`) with the
sub-pattern the old note missed: **config proves a path EXISTS, never that it RUNS.**

**Search: the engines were a symptom.** Scraped `bing` returns exactly 10 results, always
`success`, always unrelated — Romanian wallpaper tutorials, Arabic Google Translate, Airbnb — and
**different junk each time for an identical query**, so it is a blocked scraper parsing whatever it
lands on. The owner's locale hypothesis was tested at en/fr/en-US/fr-FR and disproved. `braveapi`
then refused to register silently because upstream's defaults ship it `inactive: true` and
`use_default_settings` merges by name — `disabled: false` does not override it. FRE-1310's exa block
had the identical latent defect. **ADR-0034's privacy rationale is stale and unadjudicated**: one
keyed vendor is *fewer* third parties than fanning out to a dozen.

**FRE-1288 is answered.** 3 research fixtures × 3 models × 3 trials = **27/27** say `analysis` where
the cascade says `conversational`; 36/36 agree elsewhere. Behaviour corroborates (4–8 tool calls,
+7.7k–19.8k tokens vs 0/0 for conversational). The classifier is not noisy — it is systematically
wrong on exactly one class. **Do not cite the earlier single run**: every cell was n=1, and its
`tool_use_request` zero was an artifact (control arm has no `bash`), which I initially reported as a
finding.

**The user-data loss, and what it exposed.** `config/governance/tools.yaml` grants `write`
`allowed_paths: /app/**`, but only `/app/agent_workspace` and `/app/telemetry` are mounted.
Everything else under `/app` is image layer. Susan's nine files (38,857 chars) passed governance
**cleanly, because the config says they should** — then died on my rebuild. FRE-1352 filed Urgent.
Recovery worked only because `write` arguments survive in ES telemetry; that is luck, not design.

## Worktrees — anything special
`docker/searxng/` reverts to uid **977** on every container restart — `sudo chown -R debian:977`
before any git operation there or checkouts fail confusingly. It bit repeatedly. The build worktree
also has `git update-index --assume-unchanged` set on `docker/searxng/settings.yml.example` as a
workaround for that ownership — **it will silently ignore real changes to that file** until cleared.

## Sequence position + drift
Heavy drift, owner-directed throughout. The Observability Foundation directive is still untouched.
The owner added a standing directive (console, 2026-08-31): **"Waive unless seriously necessary"** —
master merges escalated diffs without asking. Applied three times since.

## Answers for the fresh start
- **Is the eval stack trustworthy now?** Boots from a fresh substrate and is genuinely isolated
  (prod stream unchanged at 1923 across a turn that demonstrably published). But **neither arm has
  production's tool surface** — control 10 tools, treatment 15, production 22, MCP off in both. Tool
  counts compare between fixtures, never to production.
- **Why is FRE-1348 not Done?** The script is merged and proven on test fixtures; production still
  carries `NULL provenance_state`. The ops run is parked under the owner's hold — dry-run first.
- **Why is FRE-1337 not Done?** AC-3 needs `run_contamination_proof`, which **has no callers
  anywhere**. Arm 3 ran; that specific proof did not.
- **Is `build_fingerprint: unknown` on production a failure?** No — only `make eval-infra-up` passes
  the build-arg. But it is a dead field, and `unknown` is in the checker's own marker set, so
  pointing the freshness check at production would call it permanently stale. Unfiled.
- **Open, owner-led:** where do executable artifacts live? Three mechanisms exist and none fits —
  `agent_workspace` is durable but unscoped and invisible to users; artifacts/R2 are scoped and
  retrievable but single-blob and not runnable; the sandbox is correctly ephemeral. Tensions:
  runnable-by-agent vs exportable-to-user pull opposite ways; durable+executable is a real security
  step; scoping is already broken with >1 user. **FRE-1352's AC-3 names a destination this may
  change** — I recommended narrowing it to enforcement only. Owner had not answered.
