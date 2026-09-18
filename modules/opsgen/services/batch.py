"""Batch script generation results."""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class BatchService:
    def __init__(self, batches_dir: str | Path):
        self.batches_dir = Path(batches_dir)
        self.batches_dir.mkdir(parents=True, exist_ok=True)

    def create_batch(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        batch_id = uuid.uuid4().hex[:12]
        payload = {
            "id": batch_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "count": len(items),
            "success": sum(1 for item in items if item.get("ok")),
            "results": items,
        }
        path = self.batches_dir / f"{batch_id}.json"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        return payload

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        path = self.batches_dir / f"{batch_id}.json"
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
