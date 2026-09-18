"""Persist user favorite templates on the filesystem."""

import json
from pathlib import Path
from typing import Any


class FavoritesService:
    def __init__(self, favorites_file: str | Path):
        self.favorites_file = Path(favorites_file)
        self.favorites_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.favorites_file.exists():
            self._write([])

    def list_favorites(self) -> list[str]:
        return self._read()

    def is_favorite(self, name: str) -> bool:
        return name in self._read()

    def toggle(self, name: str) -> dict[str, Any]:
        favorites = self._read()
        if name in favorites:
            favorites.remove(name)
            active = False
        else:
            favorites.append(name)
            active = True
        self._write(favorites)
        return {"name": name, "favorite": active, "favorites": favorites}

    def _read(self) -> list[str]:
        with open(self.favorites_file, encoding="utf-8") as handle:
            return json.load(handle)

    def _write(self, data: list[str]) -> None:
        with open(self.favorites_file, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
