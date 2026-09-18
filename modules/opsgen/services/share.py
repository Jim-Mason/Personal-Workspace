"""Share generated scripts via short-lived filesystem links."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


class ShareService:
    def __init__(self, shares_dir: str | Path):
        self.shares_dir = Path(shares_dir)
        self.shares_dir.mkdir(parents=True, exist_ok=True)

    def create_share(
        self,
        template: str,
        title: str,
        params: dict[str, Any],
        outputs: dict[str, str],
    ) -> str:
        share_id = uuid4().hex[:10]
        payload = {
            "id": share_id,
            "template": template,
            "title": title,
            "params": params,
            "outputs": outputs,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        path = self.shares_dir / f"{share_id}.json"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        return share_id

    def get_share(self, share_id: str) -> dict[str, Any] | None:
        path = self.shares_dir / f"{share_id}.json"
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
