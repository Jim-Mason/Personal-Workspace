"""Persist script generation history and statistics on the filesystem."""

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


class HistoryService:
    MAX_ENTRIES = 100

    def __init__(self, history_file: str | Path):
        self.history_file = Path(history_file)
        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.history_file.exists():
            self._write([])

    def list_entries(self, query: str | None = None, page: int = 1, per_page: int = 20) -> dict[str, Any]:
        items = self._read()
        if query:
            q = query.lower()
            items = [
                entry
                for entry in items
                if q in entry.get("title", "").lower()
                or q in entry.get("template", "").lower()
            ]
        total = len(items)
        page = max(1, page)
        per_page = max(1, min(per_page, 100))
        pages = max(1, (total + per_page - 1) // per_page) if total else 1
        page = min(page, pages)
        start = (page - 1) * per_page
        return {
            "items": items[start : start + per_page],
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
        }

    def get_stats(self) -> dict[str, Any]:
        entries = self._read()
        template_counts = Counter(entry["template"] for entry in entries)
        top_templates = [
            {"template": name, "count": count, "title": self._latest_title(entries, name)}
            for name, count in template_counts.most_common(8)
        ]
        return {
            "total_generations": len(entries),
            "unique_templates": len(template_counts),
            "top_templates": top_templates,
            "recent": entries[:5],
        }

    def add_entry(
        self,
        template: str,
        title: str,
        params: dict[str, Any],
        outputs: dict[str, str],
        share_id: str | None = None,
    ) -> dict[str, Any]:
        entry = {
            "id": uuid4().hex[:12],
            "template": template,
            "title": title,
            "params": params,
            "outputs": outputs,
            "share_id": share_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        history = self._read()
        history.insert(0, entry)
        self._write(history[: self.MAX_ENTRIES])
        return entry

    def get_entry(self, entry_id: str) -> dict[str, Any] | None:
        for entry in self._read():
            if entry["id"] == entry_id:
                return entry
        return None

    def delete_entry(self, entry_id: str) -> bool:
        history = self._read()
        new_history = [entry for entry in history if entry["id"] != entry_id]
        if len(new_history) == len(history):
            return False
        self._write(new_history)
        return True

    def clear_all(self) -> int:
        count = len(self._read())
        self._write([])
        return count

    def _latest_title(self, entries: list[dict[str, Any]], template: str) -> str:
        for entry in entries:
            if entry["template"] == template:
                return entry.get("title", template)
        return template

    def _read(self) -> list[dict[str, Any]]:
        with open(self.history_file, encoding="utf-8") as handle:
            return json.load(handle)

    def _write(self, data: list[dict[str, Any]]) -> None:
        with open(self.history_file, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
