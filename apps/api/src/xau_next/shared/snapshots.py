"""Immutable detached domain snapshots; transport maps explicit dict copies."""

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Snapshot:
    canonical_json: str

    @classmethod
    def of(cls, value: dict):
        return cls(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    def to_dict(self) -> dict:
        return json.loads(self.canonical_json)
