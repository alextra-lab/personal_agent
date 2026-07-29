"""Regenerate the ADR-0128 telemetry naming census (FRE-1038).

Every figure quoted in ADR-0128 is produced by this script. Run it and diff the
output against ``docs/research/2026-07-29-fre-1038-telemetry-naming-census.md``
rather than trusting the committed numbers.

Usage:
    python3 scripts/audit/fre1038_naming_census.py > docs/research/<dated>.md

Static sections (1, 2) need only the repository. Live sections (3, 4, 5) need a
reachable Elasticsearch; they are skipped with a stated reason when it is not,
so an unreachable cluster can never be mistaken for a zero result.
"""

from __future__ import annotations

import collections
import glob
import json
import os
import urllib.error
import urllib.request

TEMPLATE_DIR = "docker/elasticsearch"
ES_URL = os.environ.get("CENSUS_ES_URL", "http://localhost:9200")

# (family pattern, the record-timestamp spelling that family stores)
FAMILIES: list[tuple[str, str]] = [
    ("agent-logs-*", "@timestamp"),
    ("agent-topology-*", "@timestamp"),
    ("agent-monitors-projector-health-*", "@timestamp"),
    ("agent-captains-funnel-events-*", "@timestamp"),
    ("agent-monitors-cache-reset-cadence-*", "@timestamp"),
    ("slm-requests-*", "ts"),
    ("agent-captains-captures-2*", "timestamp"),
    ("agent-captains-captures-subagents*", "timestamp"),
    ("agent-captains-reflections-*", "timestamp"),
    ("agent-insights-*", "timestamp"),
    ("agent-monitors-joinability-2*", "started_at"),
    ("agent-monitors-joinability-substrate-*", "started_at"),
    ("agent-monitors-slm-health-*", "probed_at"),
    ("user-turn-ratings-*", "rated_at"),
]

IDENTITY_FIELDS = ["@timestamp", "event_type", "trace_id", "session_id", "user_id", "span_id"]


def es(path: str, body: dict | None = None) -> dict:
    """GET/POST against Elasticsearch, returning the decoded body."""
    req = urllib.request.Request(
        f"{ES_URL}{path}",
        data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def es_reachable() -> bool:
    try:
        es("/_cluster/health")
        return True
    except (urllib.error.URLError, OSError):
        return False


def template_properties() -> dict[str, dict]:
    """Map template filename -> its mapping ``properties`` block."""
    out = {}
    for path in sorted(glob.glob(os.path.join(TEMPLATE_DIR, "*index-template.json"))):
        doc = json.load(open(path))
        out[os.path.basename(path)] = doc.get("template", {}).get("mappings", {}).get("properties", {})
    return out


def section_date_census(props: dict[str, dict]) -> None:
    print("## 1. Date-field census — committed templates\n")
    print("Selection is by declared ``type == 'date'``. **Never by field name** — a")
    print("name-substring filter misses `ts` and returns a short, confident, wrong answer.\n")
    print("| Template | date-typed properties |")
    print("|---|---|")
    spellings: collections.Counter = collections.Counter()
    for name, p in props.items():
        dates = [k for k, v in p.items() if v.get("type") == "date"]
        if dates:
            spellings[dates[0]] += 1
        print(f"| `{name}` | {', '.join('`' + d + '`' for d in dates) or '—'} |")
    print(f"\n**{len(props)} templates. Record-timestamp spellings: {len(spellings)}** — " +
          ", ".join(f"`{k}` ({v})" for k, v in spellings.most_common()) + ".\n")
    print("`agent-logs` declares a second date property, `window_start` — a payload")
    print("field, not the record timestamp.\n")


def section_sharing(props: dict[str, dict]) -> None:
    print("## 2. Field-sharing distribution — committed templates\n")
    counts: collections.Counter = collections.Counter()
    for p in props.values():
        for k in p:
            counts[k] += 1
    hist = collections.Counter(counts.values())
    print(f"- total property declarations: **{sum(counts.values())}**")
    print(f"- distinct field names: **{len(counts)}**")
    print(f"- appearing in exactly one family: **{hist[1]}**")
    print(f"- crossing families (>=2): **{sum(v for k, v in hist.items() if k >= 2)}**")
    print(f"- appearing in >=3 families: **{sum(v for k, v in hist.items() if k >= 3)}**\n")
    print("| families containing the name | count of such names |")
    print("|---|---|")
    for n in sorted(hist):
        print(f"| {n} | {hist[n]} |")
    print("\nMost widely shared: " +
          ", ".join(f"`{k}` ({v} families)" for k, v in counts.most_common(4)) + ".\n")


def section_corpus(live: bool) -> None:
    print("## 3. Live corpus — per-family document counts\n")
    if not live:
        print(f"**SKIPPED — Elasticsearch unreachable at `{ES_URL}`.** Not zero; unmeasured.\n")
        return
    print("Method: `_count` per family pattern. **Not `_cat/indices`**, whose `docs.count`")
    print("inflates via nested sub-documents (up to 4.5x on this cluster).\n")
    print("| Family pattern | date field | documents |")
    print("|---|---|---|")
    total = divergent = 0
    for pattern, spelling in FAMILIES:
        n = es(f"/{pattern}/_count")["count"]
        total += n
        if spelling != "@timestamp":
            divergent += n
        print(f"| `{pattern}` | `{spelling}` | {n:,} |")
    logs = es("/agent-logs-*/_count")["count"]
    print(f"\n- **total corpus: {total:,} documents**")
    print(f"- **on a non-`@timestamp` spelling: {divergent:,} ({divergent / total * 100:.2f}%)**")
    print(f"- **`agent-logs` share of corpus: {logs / total * 100:.2f}%**\n")


def section_identity(live: bool) -> None:
    print("## 4. Identity-field presence on the highest-volume family\n")
    if not live:
        print(f"**SKIPPED — Elasticsearch unreachable at `{ES_URL}`.** Not zero; unmeasured.\n")
        return
    total = es("/agent-logs-*/_count")["count"]
    print(f"Method: `exists` query per field over all `agent-logs-*`. Total: **{total:,}** (all-time).\n")
    print("| Field | present | share |")
    print("|---|---|---|")
    for field in IDENTITY_FIELDS:
        n = es("/agent-logs-*/_count", {"query": {"exists": {"field": field}}})["count"]
        print(f"| `{field}` | {n:,} | {n / total * 100:.1f}% |")
    print()


def section_daily_baseline(live: bool) -> None:
    """The pre-change daily volume baseline ADR-0128 AC-3 compares against.

    AC-3 must not use the all-time total as a daily figure; this section exists
    so the daily mean is recorded rather than improvised at verification time.
    """
    print("## 5. Pre-change daily volume baseline (ADR-0128 AC-3)\n")
    if not live:
        print(f"**SKIPPED — Elasticsearch unreachable at `{ES_URL}`.** Not zero; unmeasured.\n")
        return
    body = {
        "size": 0,
        "query": {"range": {"@timestamp": {"gte": "now-7d"}}},
        "aggs": {"per_day": {"date_histogram": {"field": "@timestamp", "calendar_interval": "day"}}},
    }
    buckets = es("/agent-logs-*/_search", body)["aggregations"]["per_day"]["buckets"]
    print("| day | documents |")
    print("|---|---|")
    for b in buckets:
        print(f"| {b['key_as_string'][:10]} | {b['doc_count']:,} |")
    if buckets:
        mean = sum(b["doc_count"] for b in buckets) / len(buckets)
        print(f"\n- **trailing 7-day daily mean: {mean:,.0f} documents/day** — this, not the")
        print("  all-time total, is AC-3's volume baseline.\n")


def section_cluster(live: bool) -> None:
    print("## 6. Cluster shape\n")
    if not live:
        print(f"**SKIPPED — Elasticsearch unreachable at `{ES_URL}`.** Not zero; unmeasured.\n")
        return
    health = es("/_cluster/health")
    print(f"- active primary shards: **{health['active_primary_shards']}** (single node; 1,000-per-node ceiling)")
    print(f"- cluster status: {health['status']}\n")


def main() -> None:
    live = es_reachable()
    props = template_properties()
    print("# FRE-1038 — Telemetry naming census (reproducible measurements)\n")
    print("**Date:** 2026-07-29 · **Backs:** ADR-0128 · **Regenerate:** "
          "`python3 scripts/audit/fre1038_naming_census.py`\n")
    print("Every number quoted in ADR-0128 is produced by the script above, which is")
    print("committed alongside this output. Re-run it and diff; do not copy these")
    print("figures forward on trust. Live sections state explicitly when Elasticsearch")
    print("was unreachable, so an unmeasured section can never read as a zero.\n")
    print("---\n")
    section_date_census(props)
    section_sharing(props)
    section_corpus(live)
    section_identity(live)
    section_daily_baseline(live)
    section_cluster(live)


if __name__ == "__main__":
    main()
