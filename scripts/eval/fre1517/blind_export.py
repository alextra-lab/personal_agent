"""FRE-1517 blind export — s1_trip turns 1-5 of the named production sessions. Read-only.

Stage 2 (2026-10-09): ``--run LABEL=PATH`` (repeatable) replaces the default run list,
``--last-turn`` sets the turn range, and ``--out-dir`` keeps a new export apart from an old one.

Writes one sheet per session under a random four-letter code, in random order, to
``out/blind/sheets.md``. The mapping from code to run goes to ``out/blind/key.json``, and the
script prints only the key's SHA-256, so the operator can post the hash before any scoring and
the key itself after the last score (rubric.md, Blinding procedure).

The sheet keeps the user message and the full delivered reply, including the grounding note and
any fan-out trailer, because the user saw both. Run labels, model ids, timings and trace data are
removed.
"""

import argparse
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
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", action="append", default=[], help="LABEL=PATH of a run's JSONL")
    parser.add_argument("--last-turn", type=int, default=LAST_TURN)
    parser.add_argument("--out-dir", type=Path, default=OUT / "blind")
    args = parser.parse_args()
    runs = dict(r.split("=", 1) for r in args.run) if args.run else RUNS
    runs = {label: Path(path) for label, path in runs.items()}
    last_turn = args.last_turn
    rng = random.SystemRandom()
    codes: set[str] = set()
    while len(codes) < len(runs):
        codes.add("".join(secrets.choice(string.ascii_uppercase) for _ in range(4)))
    labels = list(runs)
    rng.shuffle(labels)
    key = dict(zip(sorted(codes), labels, strict=True))

    blind = args.out_dir
    blind.mkdir(parents=True, exist_ok=True)
    parts = []
    for code in sorted(key):
        rows = [
            json.loads(line) for line in runs[key[code]].read_text().splitlines() if line.strip()
        ]
        turns = {r["turn"]: r for r in rows if r["turn"] <= last_turn}
        parts.append(f"# Sheet {code}\n")
        for n in range(1, last_turn + 1):
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
