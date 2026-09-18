"""Store and compare template version snapshots."""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class VersionService:
    MAX_VERSIONS = 20

    def __init__(self, versions_dir: str | Path):
        self.versions_dir = Path(versions_dir)
        self.versions_dir.mkdir(parents=True, exist_ok=True)

    def save_version(self, template_name: str, content: str, note: str = "") -> dict[str, Any]:
        template_dir = self.versions_dir / template_name
        template_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = template_dir / "manifest.json"
        manifest = self._read_manifest(manifest_path)

        version_id = uuid.uuid4().hex[:10]
        entry = {
            "id": version_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "note": note or "自动保存",
        }
        manifest.insert(0, entry)
        for old in manifest[self.MAX_VERSIONS :]:
            (template_dir / f"{old['id']}.yaml").unlink(missing_ok=True)
        manifest = manifest[: self.MAX_VERSIONS]
        (template_dir / f"{version_id}.yaml").write_text(content, encoding="utf-8")
        self._write_manifest(manifest_path, manifest)
        return entry

    def list_versions(self, template_name: str) -> list[dict[str, Any]]:
        manifest_path = self.versions_dir / template_name / "manifest.json"
        if not manifest_path.exists():
            return []
        return self._read_manifest(manifest_path)

    def get_version_content(self, template_name: str, version_id: str) -> str | None:
        path = self.versions_dir / template_name / f"{version_id}.yaml"
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def delete_versions(self, template_name: str) -> None:
        import shutil

        path = self.versions_dir / template_name
        if path.exists():
            shutil.rmtree(path)

    def _read_manifest(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def _write_manifest(self, path: Path, data: list[dict[str, Any]]) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
