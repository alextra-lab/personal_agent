"""FRE-1517 blind export — s1_trip turns 1-5 of the named production sessions. Read-only.

Writes one sheet per session under a random four-letter code, in random order, to
``out/blind/sheets.md``. The mapping from code to run goes to ``out/blind/key.json``, and the
script prints only the key's SHA-256, so the operator can post the hash before any scoring and
the key itself after the last score (rubric.md, Blinding procedure).

The sheet keeps the user message and the full delivered reply, including the grounding note and
any fan-out trailer, because the user saw both. Run labels, model ids, timings and trace data are
removed.
"""

import hashlib
import json
import random
import secrets
import string
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "out"
RUNS = {
    "MTPLX workers (ef9473fe)": OUT / "prod-arm3a" / "mtplx_flash_next.s1_trip.r1.jsonl",
    "MTPLX no workers (05495846)": OUT / "prod-flash-po" / "mtplx_flash_next.s1_trip.r1.jsonl",
    "MTPLX briefing (26deb270)": OUT / "prod-flash-brief" / "mtplx_flash_next.s1_trip.r1.jsonl",
    "MTPLX briefing + reserve (5592b9c8)": OUT
    / "prod-flash-reserve"
    / "mtplx_flash_next.s1_trip.r1.jsonl",
    "llama.cpp briefing + reserve (de561b78)": OUT
    / "prod-llama-brief-reserve"
    / "llamacpp_flash_next.s1_trip.r1.jsonl",
    "Sonnet no workers (a0bfd47f)": OUT / "prod-sonnet-po" / "sonnet.s1_trip.r1.jsonl",
    "Sonnet workers (3a0954b9)": OUT / "prod-sonnet-a" / "sonnet.s1_trip.r1.jsonl",
}
LAST_TURN = 5


def main() -> int:
    """Write the coded sheets and the key, and print the codes and the key's SHA-256."""
    rng = random.SystemRandom()
    codes: set[str] = set()
    while len(codes) < len(RUNS):
        codes.add("".join(secrets.choice(string.ascii_uppercase) for _ in range(4)))
    labels = list(RUNS)
    rng.shuffle(labels)
    key = dict(zip(sorted(codes), labels, strict=True))

    blind = OUT / "blind"
    blind.mkdir(exist_ok=True)
    parts = []
    for code in sorted(key):
        rows = [
            json.loads(line) for line in RUNS[key[code]].read_text().splitlines() if line.strip()
        ]
        turns = {r["turn"]: r for r in rows if r["turn"] <= LAST_TURN}
        parts.append(f"# Sheet {code}\n")
        for n in range(1, LAST_TURN + 1):
            r = turns[n]
            parts.append(
                f"## {code} — turn {n}\n\n**User:** {r['message']}\n\n**Reply:**\n\n{r.get('reply') or '(empty)'}\n"
            )
    (blind / "sheets.md").write_text("\n".join(parts))
    raw = json.dumps(key, indent=2, sort_keys=True).encode()
    (blind / "key.json").write_bytes(raw)
    print("codes:", " ".join(sorted(key)))
    print("key sha256:", hashlib.sha256(raw).hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main())
