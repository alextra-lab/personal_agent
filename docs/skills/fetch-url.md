# fetch-url — Fetch a URL and return its content as plain text

**Category:** `network_read` · **Risk:** low · **Approval:** `curl` auto-approved (NORMAL/ALERT/DEGRADED); not available in LOCKDOWN

## Plain JSON / API response

For REST APIs or endpoints that return JSON, fetch directly:

```bash
curl -s -L -A 'personal-agent/0.1 (research bot)' --max-time 20 <url>
```

Pipe through `jq` for structured inspection:

```bash
curl -s -L -A 'personal-agent/0.1 (research bot)' --max-time 20 <url> | jq .
```

## HTML pages — fetch with text stripping

HTML responses need script/style removal and block-tag newline injection to be readable:

```bash
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
curl -s -L -A 'personal-agent/0.1 (research bot)' --max-time 20 <url> | head -c 50000
```

## Common patterns

```bash
# Fetch a GitHub raw file
curl -s -L -A 'personal-agent/0.1 (research bot)' --max-time 20 \
  'https://raw.githubusercontent.com/org/repo/main/README.md'

# Fetch JSON API with specific header
curl -s -L -A 'personal-agent/0.1 (research bot)' --max-time 20 \
  -H 'Accept: application/json' \
  'https://api.example.com/v1/status' | jq .

# Follow redirects (included by default with -L)
curl -s -L -A 'personal-agent/0.1 (research bot)' --max-time 20 \
  'https://t.co/shortened-link'
```

## Governance

- `curl` is auto-approved in NORMAL, ALERT, and DEGRADED modes — no PWA prompt.
- Not available in LOCKDOWN or RECOVERY.
- Max content to surface to the model: 10,000–50,000 chars. The HTML stripper above caps at 10,000; increase the slice (`:10000`) if more is needed, up to 50,000.
- Always use `--max-time 20` to prevent hanging on slow hosts.
- Hard-denied: `wget` is blocked by the bash governance layer — always use `curl`.

**ALERT-mode note:** `bash curl` is auto-approved in ALERT mode — unlike the legacy `fetch_url` tool (which was disabled in ALERT mode), primitive `curl` has no ALERT-mode restriction. Be aware outbound network calls continue in degraded states.

See also: [bash — Shell Command Executor](bash.md)
