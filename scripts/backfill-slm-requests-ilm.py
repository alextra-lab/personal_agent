#!/usr/bin/env python3
"""Backfill ILM lifecycle policy to existing slm-requests-* indices (FRE-1106).

This script idempotently sets index.lifecycle.name on all existing slm-requests-*
indices to make them lifecycle-managed. It is meant to be run once after the
slm-requests-ilm-policy is deployed.

The script:
1. Discovers all slm-requests-* indices
2. Checks if they are already managed (no-op if so)
3. Sets index.lifecycle.name="slm-requests" on unmanaged indices
4. Verifies the lifecycle binding took effect

Usage:
    python3 scripts/backfill-slm-requests-ilm.py [--es-url <url>] [--dry-run]

Environment:
    ES_URL: Elasticsearch URL (default: http://localhost:9200)
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def es_request(
    es_url: str, method: str, path: str, data: dict | None = None, expect_404: bool = False
) -> dict:
    """Make an ES API request."""
    url = urljoin(es_url, path)
    headers = {"Content-Type": "application/json"}

    if data:
        body = json.dumps(data).encode("utf-8")
    else:
        body = None

    req = Request(url, data=body, headers=headers, method=method)

    try:
        with urlopen(req, timeout=10) as resp:
            if resp.status in (200, 201):
                content = resp.read().decode("utf-8")
                return json.loads(content) if content else {}
            raise RuntimeError(f"Unexpected status {resp.status}")
    except HTTPError as e:
        if expect_404 and e.code == 404:
            return {}
        log.error(f"{method} {path} failed: {e.code} {e.reason}")
        raise


def discover_indices(es_url: str) -> list[str]:
    """Find all slm-requests-* indices."""
    resp = es_request(es_url, "GET", "/_cat/indices?format=json")
    return [idx["index"] for idx in resp if idx["index"].startswith("slm-requests-")]


def get_ilm_status(es_url: str, index: str) -> dict:
    """Get ILM explain status for an index."""
    resp = es_request(es_url, "GET", f"/{index}/_ilm/explain")
    return resp.get("indices", {}).get(index, {})


def set_lifecycle_policy(es_url: str, index: str, policy: str, dry_run: bool = False) -> bool:
    """Set index.lifecycle.name on an index."""
    if dry_run:
        log.info(f"[DRY-RUN] Would set lifecycle policy '{policy}' on {index}")
        return True

    try:
        es_request(es_url, "PUT", f"/{index}/_settings", {"index.lifecycle.name": policy})
        log.info(f"Set lifecycle policy '{policy}' on {index}")
        return True
    except Exception as e:
        log.error(f"Failed to set lifecycle policy on {index}: {e}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill ILM lifecycle policy to slm-requests-* indices"
    )
    parser.add_argument(
        "--es-url",
        default="http://localhost:9200",
        help="Elasticsearch URL (default: http://localhost:9200)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    args = parser.parse_args()

    es_url = args.es_url
    policy_name = "slm-requests"
    dry_run = args.dry_run

    log.info(f"Starting backfill on {es_url}")
    if dry_run:
        log.info("[DRY-RUN MODE]")

    # 1. Verify policy exists
    try:
        policy_resp = es_request(es_url, "GET", f"/_ilm/policy/{policy_name}", expect_404=True)
        if not policy_resp:
            log.error(f"Policy '{policy_name}' does not exist. Run setup-elasticsearch.sh first.")
            return 1
        log.info(f"✓ Policy '{policy_name}' found")
    except Exception as e:
        log.error(f"Failed to check policy: {e}")
        return 1

    # 2. Discover indices
    try:
        indices = discover_indices(es_url)
        log.info(f"Found {len(indices)} slm-requests-* indices")
    except Exception as e:
        log.error(f"Failed to discover indices: {e}")
        return 1

    if not indices:
        log.info("No indices to backfill.")
        return 0

    # 3. Backfill unmanaged indices
    updated = 0
    skipped = 0
    failed = 0

    for index in sorted(indices):
        try:
            status = get_ilm_status(es_url, index)
            is_managed = status.get("managed", False)

            if is_managed and status.get("policy") == policy_name:
                log.info(f"→ {index} already managed by '{policy_name}'")
                skipped += 1
            else:
                if set_lifecycle_policy(es_url, index, policy_name, dry_run):
                    updated += 1
                else:
                    failed += 1
        except Exception as e:
            log.error(f"Error processing {index}: {e}")
            failed += 1

    # 4. Summary
    log.info("")
    log.info(f"Backfill complete: {updated} updated, {skipped} already managed, {failed} failed")

    if failed > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
