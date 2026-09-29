"""JSON (list of objects / object wrapping a list) and JSON-Lines."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from .base import BaseReader, Block, FileReadError, TableBlock


def _flatten(obj: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in obj.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        elif isinstance(v, list):
            out[key] = "; ".join(json.dumps(x, ensure_ascii=False) if isinstance(x, (dict, list)) else str(x) for x in v)
        else:
            out[key] = v
    return out


class JsonReader(BaseReader):
    extensions = (".json", ".jsonl", ".ndjson")

    def read(self, path: Path) -> Iterator[Block]:
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            raise FileReadError(f"Cannot read JSON file: {exc}") from exc
        records: list[dict] = []
        try:
            if path.suffix.lower() in (".jsonl", ".ndjson"):
                for n, line in enumerate(text.splitlines(), start=1):
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError as exc:
                        self.issue(f"line {n}", f"invalid JSON line: {exc}")
                        continue
                    if isinstance(obj, dict):
                        records.append(_flatten(obj))
            else:
                data = json.loads(text)
                if isinstance(data, dict):
                    lists = [v for v in data.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
                    data = lists[0] if lists else [data]
                records = [_flatten(o) for o in data if isinstance(o, dict)]
        except json.JSONDecodeError as exc:
            raise FileReadError(f"Invalid JSON: {exc}") from exc
        self.meta.update(sheets=1)
        headers: list[str] = []
        for rec in records:
            for k in rec:
                if k not in headers:
                    headers.append(k)
        rows = [[rec.get(h) for h in headers] for rec in records]
        yield TableBlock(rows=[(1, headers)] + [(i, r) for i, r in enumerate(rows, start=2)], method="json")
