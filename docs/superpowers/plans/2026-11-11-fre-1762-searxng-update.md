# FRE-1762 — Move SearXNG from 2026.8.29 to 2026.11.11

> Date: 2026-11-11 · Ticket: FRE-1762 · Tier: trivial (config, tests, docs; no `src/` change)

## Scope

Move the SearXNG image to `2026.11.11-248b8ffc0` in every compose file that runs SearXNG. In
`docker-compose.cloud.yml` the pin moves from `2026.8.29-d226b78bc`. In `docker-compose.yml`
(dev) the image moves from `latest`, so dev gets a pin for the first time. Make no change to
our search code. The change goes before FRE-1760.

Upstream publishes no GitHub release or tag. It publishes one Docker tag for each commit on
`master`. So "the current upstream release" means the newest tag on Docker Hub. At the time of
the work that tag was `2026.11.11-248b8ffc0`, pushed about two hours earlier. Its digest is
`sha256:71a410e383ea…`, the same digest as `latest`. The range has 192 upstream commits.

## AC-1 — Upstream changes and their effect on `web_search`

Our tool reads only `title`, `url`, `content`, `engine` and `score` from each result
(`shape_search_response`, `tools/web.py`). It sends `pageno=1` always.

| # | Upstream change | Changes what `web_search` returns? | Settings edit? |
|---|---|---|---|
| 1 | `42829cecd` (2026-09-13): title is cut to 200 characters and content to 1200, at a word boundary, with ` …` added | **Yes.** Our `exa` block asks for 2000 characters. SearXNG now drops everything above 1200. Live check (AC-3): 4 of 20 `braveapi` results and 2 of 5 `exa` results sit at the cap. | No. The value 2000 now has no effect above 1200. |
| 2 | `a9d990033` (2026-09-30): `braveapi` maps SafeSearch to `off`, `moderate` or `strict` | **Possibly yes.** Our `search.safe_search` is 0. The old code sent no `safesearch` value for 0. The new code sends `safesearch=off`. I did not verify what Brave does when the value is absent. | Optional. See "Owner decision". |
| 3 | `745d5b6fc` (2026-09-03): `braveapi` offset is the page index, not a result count | No. We always send page 1, and the offset is 0 in both versions. | No |
| 4 | `3e454637f`, `cccbc9e15`: `braveapi` sends `Accept: application/json` and `Cache-Control: no-cache` | None expected. The live response had the same shape. | No |
| 5 | `4d9c982cd`, `c0014f015`: `braveapi` reuses the shared Brave parser. Title no longer passes through `html_to_text`. `publishedDate` now comes from `page_age`. | No. We do not read `publishedDate`. The live response had no markup or entity in 25 titles. | No |
| 6 | `adbf160ff`: `braveapi` no longer parses web results as news results | None. The live query returned 20 web results. | No |
| 7 | `b4f616fed`: engines use `setup()` instead of `init()`. This is the only change to `exaapi.py` in the range. | No. | No |
| 8 | `html_to_text` in `searx/utils.py` | No. Same output in both versions (AC-5). | No |
| 9 | `_normalize_text_fields` | Only the cap in row 1. Whitespace handling is the same (AC-5). | No |
| 10 | `be836e614` (2026-09-04): network layer moves from `httpx` to `curl_cffi`. Settings `outgoing.pool_maxsize`, `outgoing.keepalive_expiry` and the engine keys `max_keepalive_connections` and `keepalive_expiry` are removed. `enable_http3` is added. | No. Our settings set none of the removed keys (checked by key name, no value printed). The scratch container loaded 22 of 22 declared engines, including the `lemmy` network (`outgoing.networks`, unchanged in `network.py`). | No |
| 11 | `ef05645f0`, `3afd94926`, `1c10abe2d`: container build rework | No. The only change in `entrypoint.sh` is two exit codes (127 to 1). | No |
| 12 | Engine additions, removals and defaults in `searx/settings.yml` (for example `e75fd22d0`) | No. `keep_only` in our file names the engines that run. The parity monitor reports 22 declared and 22 registered. | No |

No change needs a settings edit. No change breaks an engine. Rows 1 and 2 change the output of
`web_search` and go to the owner as information.

### Answer to the ticket's agent note on `settings.yml.new`

The entrypoint of the new tag creates `settings.yml` only when the file is missing. It does not
write `settings.yml.new` at either tag. The `.new` file in `docker/searxng/` is dated
2026-04-17 and comes from an older image. The deploy does not touch it.

## Owner decision (does not block the PR)

1. **SafeSearch.** `search.safe_search: 0` now sends `safesearch=off` to Brave. Before, no value
   was sent. To keep the old behavior, the owner can set `search.safe_search: 1` in the real
   `docker/searxng/settings.yml`. That value also reaches other engines. The seat did not
   change the owner's file.
2. **ADR-0158 grid.** The grid for `content_max_characters` is 500, 800, 1200 and 1600. After
   this update, SearXNG cuts every `content` to 1200 characters. So the 1600 arm gives the same
   result as the 1200 arm, and the production value 2000 gives about the same result as 1200.
   Any ADR-0158 measurement on the new image must read this.

## AC-2 — One pin everywhere

`docker-compose.cloud.yml` and `docker-compose.yml` both pin `2026.11.11-248b8ffc0`. Before this
change, cloud pinned `2026.8.29-d226b78bc` and dev ran `latest`, so dev ran whatever image the
last pull gave. No other
root compose file runs SearXNG. The eval compose files only reference the production service
through `depends_on`. `tests/scripts/test_searxng_image_pin.py` fails on `latest`, on an
undated tag, and on two different tags.

## AC-3 — Scratch container, one `general` query

Cost: one Brave call and one Exa call, two paid calls in total (owner go on the ticket,
2026-11-11).

Environment: container on the new tag, port 18888 on 127.0.0.1, limits 256 MB and 0.5 CPU,
a copy of our `settings.yml`. No production container, directory or network was used.

Observed result of the query `python dataclass frozen`:

- HTTP 200 in 1.4 s. 25 results: 20 from `braveapi`, 5 from `exa`. `unresponsive_engines` is empty.
- Parity monitor: `declared: 22 engines`, `registered: 22 engines`, exit 0.
- Start-up log: one `ERROR` line, `X-Forwarded-For nor X-Real-IP header is set!`. The production
  container on the old tag logs the same line.
- Memory after the query: 86 MiB of 256 MiB. The production container shows 212 MiB after four
  weeks of use. The two values are not comparable, so the steady-state memory of the new tag is
  not known.

## AC-5 — Line breaks, new version against old version

Probe: `html_to_text` and `_normalize_text_fields` run inside both images on a synthetic table.
No network. No paid call.

| Function | Input | Old tag | New tag |
|---|---|---|---|
| `html_to_text` | table, list and two paragraphs | `NameScorealpha1beta2firstsecondline oneline two` | the same string |
| `_normalize_text_fields` | `row one\nrow two\n\nrow three\twith tab` | `row one row two row three with tab` | the same string |

**The new version still removes line breaks.** `html_to_text` also joins table cells without a
space. The FRE-1760 design must handle both, on either version. The new version adds the cap
from row 1: a 1999-character input gave 1201 characters ending in ` …`.

## Deploy runbook (master)

1. Merge. Run `docker compose -f docker-compose.cloud.yml pull searxng` on the VPS if the image
   is not present. The new tag is already pulled on the VPS.
2. Recreate only SearXNG: `docker compose -f docker-compose.cloud.yml up -d --no-deps searxng`.
   Never recreate Caddy with a single-service rebuild.
3. Verify (AC-4): the container reports `healthy`, `/healthz` answers 200, one `web_search`
   through the gateway returns results from `braveapi` and `exa`, and the gateway `/health` is
   unchanged.
4. Run `python -m scripts.monitors.searxng_engine_parity`. Expect 22 declared and 22 registered.

Rollback: set the tag in `docker-compose.cloud.yml` back to `2026.8.29-d226b78bc` and run step 2.
The old image is still on the VPS. The pin test then fails until `docker-compose.yml` carries the
same tag, so move both files together.
