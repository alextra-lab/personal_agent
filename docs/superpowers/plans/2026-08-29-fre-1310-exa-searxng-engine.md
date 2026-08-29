# FRE-1310 — Add Exa as a SearXNG engine

Branch: `fre-1310-exa-searxng-engine`

## Context

[FRE-1310](https://linear.app/frenchforest/issue/FRE-1310) is written as a decision ticket
("needs the owner's yes before anything is built") with acceptance criteria "deliberately not
written yet." The owner has explicitly directed implementation despite that, and separately
confirmed: Exa goes in its **own SearXNG category**, not `general` — opt-in exposure, not a
default-on posture change for every query.

This plan covers what's concretely buildable without a live Exa API key: the engine
configuration scaffold (safely shipped `disabled: true`), the public-repo credential-safety
fix the ticket's own BLOCKER section calls for, and model discoverability. It does **not**
wire Exa spend into `cost_gate`/`budget.yaml` — confirmed via a research pass that `cost_gate`
is strictly `ModelRole`-scoped (LLM completions only) with zero precedent for tracking a
third-party API's non-LLM spend; forcing an `exa` role into that structure would fail
`validate_role_totality` (it validates `budget.yaml`'s roles against the `ModelRole` enum) and
is a separate, ADR-sized piece of work. Flagged in the handoff, not built here.

## Verified facts backing the design

- `docker/searxng/settings.yml` is tracked in the public repo (confirmed via `git ls-files`,
  per the ticket). No engine currently carries a live secret.
- SearXNG's stock Docker image (`searxng/searxng:latest`, `docker-compose.yml:60`) does **not**
  support `${VAR}`-style substitution in `settings.yml` — confirmed against
  `docs.searxng.org/admin/settings/settings.html`; no entrypoint script exists in
  `docker/searxng/` to do it ourselves. This settles the ticket's open question: the
  `budget.yaml`/FRE-1209 gitignore-real-track-example pattern is the only safe route, not an
  invented cleaner one.
- `web_search`'s `categories` param (`tools/web.py:82-97`) is a free-form string passed
  straight through to SearXNG (`web.py:212`) — no Python enum/Literal to update. A new
  category is a `settings.yml` + docs change only.
- `config/governance/tools.yaml`'s `web_search` entry gates the whole tool
  (`risk_level: low`, `tools.yaml:48-54`) — there is no per-SearXNG-engine governance lever.
  Not changing this; noting it as a known gap in the handoff (governance can't yet
  distinguish "this query got routed to Exa" from any other web_search call).
- `fetch_url`'s content-length convention: `_DEFAULT_MAX_CHARS = 10_000`
  (`tools/fetch.py:80`). Exa's `content_max_characters` defaults to 500 upstream (SearXNG
  docs) — far too small for "full page text"; matching `fetch_url`'s own default keeps the
  two full-text-returning tools consistent.
- Exa pricing (fetched 2026-08-29 from `exa.ai/docs/reference/pricing`): standard `/search`
  $7/1k requests (up to 10 results), `deep`/`deep-lite` $12/1k, `deep-reasoning` $15/1k
  (12-40s latency — also excluded on FRE-1303 authorship-independence grounds, not just
  cost). Default rate limit 10 QPS / 600 per minute. New accounts: $20 free credit.
- Existing regression test precedent: `tests/test_tools/test_web_search.py:24-36`
  (`test_chefkoch_not_in_general_category`) reads `docker/searxng/settings.yml` directly via
  `yaml.safe_load(Path(...).read_text())`. Once that file is gitignored, a fresh CI checkout
  only has `settings.yml.example` — this test would break without a fallback. The established
  fallback pattern already exists for `budget.yaml`
  (`tests/_helpers/budget_config.py:27-32`: prefer the real file, fall back to `.example`).
  Same shape applied here, inline (single test file, no need for a shared helper yet).

## Design decisions (this ticket's open questions, resolved for this PR)

| Question | Decision | Why |
|---|---|---|
| Which category | Dedicated `exa` category, not `general` | Owner's explicit answer — opt-in exposure |
| `search_type` | `auto` | Ticket: pin `auto` or `fast`; `auto` is also SearXNG's own upstream default |
| `content_mode` | `text` (not the docs' `highlights` default) | Matches the ticket's own prose rationale — full page text collapses search+fetch for the ADR-0138 citation contract; `highlights` is just SearXNG's doc-page example, not what the ticket argues for |
| `content_max_characters` | `10000` | Matches `fetch_url`'s existing default for consistency between the two full-text tools |
| Secret handling | Gitignore `settings.yml`, track `.example`, `pass show seshat/EXA_API_KEY` on deploy | Confirmed no env-substitution support; mirrors FRE-1209's `budget.yaml` precedent exactly |
| Shipped state | `disabled: true`, placeholder `api_key` | No live Exa key exists; ship the capability, not a live secret. Enabling is an ops step. |
| Cost governance wiring | Not in this PR | `cost_gate` is LLM-role-scoped only; flagged as a follow-up gap, not built |

## Steps

1. **`docker/searxng/settings.yml.example`** (new file) — copy of the current tracked
   `settings.yml` content, plus a new `exa` engine block appended after the `Recipes` section
   (same structural pattern as `chefkoch`'s own scoped-category comment block), and a header
   comment block (mirroring `budget.yaml.example`'s) explaining the gitignore/template split
   and the `pass show seshat/EXA_API_KEY` activation step.

2. **`.gitignore`** — add `docker/searxng/settings.yml` under a new comment block next to the
   existing `budget.yaml` entry, referencing FRE-1310 and the same public-repo rationale.

3. **Untrack + locally apply**: `git rm --cached docker/searxng/settings.yml` (working-tree
   file stays on disk so local/dev SearXNG keeps functioning), then edit the now-untracked
   working file to add the same `exa` engine block (placeholder key, `disabled: true`) so this
   worktree's own SearXNG instance has it too.

4. **`src/personal_agent/tools/web.py`** — update `web_search_tool.description` and the
   `categories` `ToolParameter.description` to mention `exa`: what it's for (semantic,
   long-tail, non-English queries needing full page text), that it's opt-in (not in
   `general`), and how to reach it (`categories="exa"`).

5. **Tests** (`tests/test_tools/test_web_search.py`):
   - Add a local `_load_searxng_config()` helper (real file if present, else `.example` —
     mirrors `tests/_helpers/budget_config.py`'s pattern) and switch
     `test_chefkoch_not_in_general_category` to use it (currently hardcodes the real path,
     which won't exist in CI once gitignored).
   - `test_exa_not_in_general_category` — engine exists, `categories == "exa"`,
     `categories != "general"`.
   - `test_exa_search_type_pinned_to_auto` — asserts `search_type == "auto"` exactly (not
     merely "not deep-reasoning") — matches AC4 precisely (Codex review caught the original
     draft's test being looser than the AC).
   - `test_exa_content_mode_and_length` — asserts `content_mode == "text"` **and**
     `content_max_characters == 10000` in one test — covers AC5 fully (the original draft only
     asserted `content_mode`, silently leaving the char-count half of AC5 untested).
   - `test_exa_shipped_disabled_with_placeholder_key` — asserts `disabled is True` and
     `api_key == "REPLACE_WITH_EXA_API_KEY"` (the exact placeholder literal, not just "looks
     like a placeholder") — guards against ever accidentally shipping a real key in the
     example file.
   - `test_searxng_settings_yml_not_tracked_in_git` — runs `git ls-files
     docker/searxng/settings.yml` and asserts empty output; directly verifies AC1's
     "no longer tracked" half (the placeholder-key test above covers the "no live key in the
     diff" half). Codex review noted AC1 had no automated check at all in the original draft.
   - `test_web_search_description_mentions_exa` — tool description names `exa` and states the
     opt-in framing; `categories` param description lists `exa` among the options.

   **Known test-coverage limit (accepted, not fixed here):** these are static config/string
   assertions, consistent with this test file's existing no-container approach (its own
   module docstring: "no SearXNG container required"). They prove the config *declares*
   Exa opt-in/disabled/pinned correctly — they do not runtime-prove SearXNG actually honors
   `disabled: true` inertly or that `categories` scoping is enforced at the SearXNG level
   (Codex review point #3). No live-container test exists for any other engine in this suite
   either, so this isn't a new gap introduced here.

6. **Quality gates**: `make test`, `make mypy`, `make ruff-check` + `make ruff-format`,
   `pre-commit run --all-files`.

## Acceptance criteria (written now, since the ticket left none)

1. `docker/searxng/settings.yml` is no longer tracked in git; no literal Exa API key exists
   anywhere in the committed diff.
2. `docker/searxng/settings.yml.example` documents the full engine list including `exa`, with
   a placeholder key and activation instructions.
3. The `exa` engine is not reachable via the default `general` category — verified by test.
4. `search_type` is pinned to `auto` (not `deep-reasoning`) — verified by test.
5. `content_mode` is `text`, `content_max_characters` is `10000` — verified by test.
6. `web_search`'s tool/param descriptions surface `exa` so the model can discover it —
   verified by test.
7. `make test` / `mypy` / `ruff` / `pre-commit` all pass.

## Handoff notes (for the PR / Linear comment, not built here)

- **Cost governance gap**: no automated budget lane for Exa spend exists or is added by this
  PR. Interim mitigation is Exa's own default rate limit (10 QPS / 600/min) plus manual
  dashboard monitoring. Wiring this into `cost_gate` needs a new non-LLM spend-tracking
  mechanism — likely ADR-worthy, not folded in here.
- **Governance gap**: `tools.yaml` gates `web_search` as a whole; there's no lever to treat a
  query that got routed to Exa differently from one that stayed self-hosted. Named, not fixed.
- **"Opt-in" is discoverability, not an enforced boundary** (Codex review point #4): the
  dedicated `exa` category keeps Exa out of default `general` searches and out of the model's
  reflexive path, but `web_search`'s `engines` param is free-form and already lets any caller
  reach any engine directly (`engines="exaapi"`), exactly like `engines="chefkoch"` already
  does for the recipes engine today. This is existing `web_search` behavior, not a new gap
  from this ticket — but "opt-in" should be understood as "the model won't reach for it
  without being told to," not "governance prevents it."
- **Git history**: no rewrite needed — the currently-tracked `settings.yml` never held a
  secret, so `git rm --cached` + `.gitignore` is sufficient going forward. Named for the
  record (Codex review point #1): `.gitignore` only stops *accidental* re-adds, not a forced
  `git add -f`; if a real key is ever committed by mistake, that's a revoke-and-rewrite-history
  situation, not something this pattern alone prevents.
- **Enabling it live**: `pass show seshat/EXA_API_KEY` (owner must first create an Exa account
  and API key — no existing credential to reuse), paste into the VPS's deployed (untracked)
  `docker/searxng/settings.yml`, flip `disabled: false`, restart the `searxng` container.
