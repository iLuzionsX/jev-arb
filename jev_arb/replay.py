from __future__ import annotations

import json
from pathlib import Path


def load_jsonl(path: str) -> list[dict]:
    events: list[dict] = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        item = json.loads(line)
        if item.get("type", "book") != "book":
            continue
        events.append(item)
    return sorted(events, key=lambda item: int(item["ts_ms"]))


