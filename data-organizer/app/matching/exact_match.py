"""Exact / learned matching: the user's saved decisions ("learning from decisions").

* ``map``    decisions - alias (+ city context) -> master entity, applied automatically to every future occurrence
* ``reject`` decisions - "this alias is NOT that entity", so the same wrong suggestion is never made again
"""
from __future__ import annotations

from typing import Iterable


class VerifiedMappings:
    def __init__(self, decisions: Iterable[dict] = ()):
        self.maps: dict[tuple[str, str], str] = {}
        self.rejects: set[tuple[str, str, str]] = set()
        for d in decisions:
            key = (d["alias_normalized"], d.get("context_city") or "")
            if d["decision"] == "map":
                self.maps[key] = d["master_entity_id"]
            elif d["decision"] == "reject":
                self.rejects.add((*key, d["master_entity_id"]))

    def lookup(self, alias_norm: str, city: str) -> str | None:
        return self.maps.get((alias_norm, city or "")) or self.maps.get((alias_norm, ""))

    def is_rejected(self, alias_norm: str, city: str, master_id: str) -> bool:
        return (alias_norm, city or "", master_id) in self.rejects or (alias_norm, "", master_id) in self.rejects

    def add_map(self, alias_norm: str, city: str, master_id: str) -> None:
        self.maps[(alias_norm, city or "")] = master_id

    def add_reject(self, alias_norm: str, city: str, master_id: str) -> None:
        self.rejects.add((alias_norm, city or "", master_id))

    def repoint(self, old: str, new: str) -> None:
        """A master entity was merged into another: keep every saved mapping valid."""
        for k, v in list(self.maps.items()):
            if v == old:
                self.maps[k] = new
