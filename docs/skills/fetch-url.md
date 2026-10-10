---
name: fetch-url
description: Fetch a URL and return its content as plain text using bash curl.
when_to_use: When you need to fetch web pages, REST APIs, or check HTTP endpoints.
tools: [bash]
keywords:
  - "fetch "
  - "https://"
  - "http://"
  - readme on
  - github.com
  - anthropic.com
  - current pricing
  - "what's on the page"
  - check the url
---

# fetch-url — Fetch a URL and return its content as plain text

**Status:** Fallback path (FRE-1297, 2026-08-25). A native `fetch_url` tool is registered
again — prefer it: its fetched page content is an admissible citation source under the
ADR-0138 grounding contract, while a `bash`/curl fetch of the same page is never citable
(D2's independence rule — `bash` takes arbitrary model-authored input). Reach for `bash`
curl here only when `fetch_url` cannot do the job — a non-GET request, a request needing
custom headers/auth, or a response `fetch_url`'s extraction mangles — and note in your
answer that the result is not citable.

**Category:** `network_read` · **Risk:** low · **Approval:** every `curl` in this skill asks the user: `curl` runs without asking only for a read of internal telemetry (FRE-1583). `fetch_url` runs without asking.

## Default recipe — status + body together

Always get the HTTP status alongside the body so you can confirm the request succeeded:

```bash
# ask: curl to an outside host asks for approval (FRE-1583, ADR-0156 D8.2).
# Status code on first line, then body — works for JSON and text
curl -s -o /dev/stdout -w '\n--- HTTP %{http_code} ---\n' -L -A 'personal-agent/0.1 (research bot)' --max-time 20 <url>
```

Or: pipe to `jq` for JSON APIs and the exit code tells you if it failed:

```bash
# ask: curl to an outside host asks for approval (FRE-1583, ADR-0156 D8.2).
curl -s -L -A 'personal-agent/0.1 (research bot)' --max-time 20 <url> | jq .
```

## HTML pages — fetch with text stripping

HTML responses need script/style removal and block-tag newline injection to be readable:

```bash
# ask: curl to an outside host and python3 ask for approval (FRE-1583, ADR-0156 D8.2).
curl -s -L -A 'personal-agent/0.1 (research bot)' --max-time 20 <url> | python3 -c "
import sys, re
from html.parser import HTMLParser

class TextExtractor(HTMLParser):
    _SKIP = {'script','style','head','noscript','meta','link','svg','iframe'}
    _BLOCK = {'p','div','h1','h2','h3','h4','h5','h6','li','tr','br'}
    def __init__(self):
        super().__init__()
        self.text = []
        self._skip = 0
    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP: self._skip += 1
        if tag in self._BLOCK: self.text.append('\n')
    def handle_endtag(self, tag):
        if tag in self._SKIP: self._skip -= 1
    def handle_data(self, data):
        if not self._skip: self.text.append(data)

p = TextExtractor()
p.feed(sys.stdin.read())
print(re.sub(r'\n{3,}', '\n\n', ''.join(p.text)).strip()[:10000])
"
```

## Large responses

Cap raw output at 50 KB to avoid truncation:

```bash
# ask: curl to an outside host asks for approval (FRE-1583, ADR-0156 D8.2).
curl -s -L -A 'personal-agent/0.1 (research bot)' --max-time 20 <url> | head -c 50000
```

## Common patterns

```bash
# ask: curl to an outside host asks for approval (FRE-1583, ADR-0156 D8.2).
# Fetch a GitHub raw file
curl -s -L -A 'personal-agent/0.1 (research bot)' --max-time 20 \
  'https://raw.githubusercontent.com/org/repo/main/README.md'

# ask: curl to an outside host asks for approval (FRE-1583, ADR-0156 D8.2).
# Fetch JSON API with specific header
curl -s -L -A 'personal-agent/0.1 (research bot)' --max-time 20 \
  -H 'Accept: application/json' \
  'https://api.example.com/v1/status' | jq .

# ask: curl to an outside host asks for approval (FRE-1583, ADR-0156 D8.2).
# Follow redirects (included by default with -L)
curl -s -L -A 'personal-agent/0.1 (research bot)' --max-time 20 \
  'https://t.co/shortened-link'
```

## Governance

- `curl` to an outside host asks the user every time, in every mode (owner, 2026-10-10, FRE-1583). Use `fetch_url` when it can do the job: it runs without asking.
- Without an approval surface (no PWA client), an ask is refused.
- Max content to surface to the model: 10,000–50,000 chars. The HTML stripper above caps at 10,000; increase the slice (`:10000`) if more is needed, up to 50,000.
- Always use `--max-time 20` to prevent hanging on slow hosts.
- Hard-denied: `wget` is blocked by the bash governance layer — always use `curl`.

See also: [bash — Shell Command Executor](bash.md)
