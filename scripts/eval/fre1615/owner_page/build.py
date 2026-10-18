"""FRE-1615: build the owner's blind-scoring page (no arm names in it).

    uv run python scripts/eval/fre1615/owner_page/build.py <out.html> scripts/eval/fre1615/owner_page/template.html

Reads the run's owner_key.json and owner_p2_key.json (``judge owner``), the rows, the suite and the expect lines.
"""

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
B = ROOT / "telemetry/evaluation/fre1615-harness-spike"
OUT = Path(sys.argv[1])

sets = [
    ("Set 1", "", json.loads((B / "block1/owner_key.json").read_text())),
    ("Set 2", "p2-", json.loads((B / "block1/owner_p2_key.json").read_text())),
]
expects = yaml.safe_load((ROOT / "scripts/eval/fre1615/expects.yaml").read_text())["expects"]
suite = {
    t["id"]: t
    for t in yaml.safe_load((ROOT / "scripts/eval/fre1615/suite.yaml").read_text())["tasks"]
}
rows = {}
for line in (B / "block1-a/rows.jsonl").read_text().splitlines() + (
    B / "block1-b/rows.jsonl"
).read_text().splitlines():
    r = json.loads(line)
    if "error" not in r:
        rows[(r["id"], r["arm"])] = r

CATEGORY = {
    "direct_question": "Direct question",
    "research": "Research",
    "memory_recall": "Memory",
    "follow_up": "Follow-up",
    "system_investigation": "System check",
    "coding_file": "Code and files",
    "artifact": "Artifact",
    "authorization": "Needs permission",
}

pairs = []
for set_name, prefix, key in sets:
    for task, slots in key.items():
        t = suite[task]
        side = {}
        for slot in ("A", "B"):
            r = rows[(task, slots[slot])]
            first = r.get("first_turn") or {}
            side[slot] = {"reply": r["reply"], "earlier": first.get("reply", "")}
        pairs.append(
            {
                "id": prefix + task,
                "set": set_name,
                "category": CATEGORY.get(t["category"], t["category"]),
                "earlier_user": t.get("history_user", ""),
                "message": t["message"],
                "expect": expects[task],
                "A": side["A"],
                "B": side["B"],
            }
        )

data = json.dumps(pairs, ensure_ascii=False).replace("</", "<\\/")
template = Path(sys.argv[2]).read_text()
OUT.write_text(template.replace("/*__PAIRS__*/[]", data))
text = OUT.read_text()
assert all(f'"{a}"' not in data for a in ("S", "PL", "P2")), "an arm name leaked"
print(len(pairs), "pairs", len(text), "bytes")
