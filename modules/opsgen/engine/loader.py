"""Load and validate YAML script templates from the filesystem."""

import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


class TemplateError(ValueError):
    pass


class TemplateLoader:
    QUESTION_TYPES = {"string", "int", "bool", "choice", "multi"}
    DEFAULT_PER_PAGE = 12

    def __init__(self, templates_dir: str | Path, custom_dir: str | Path | None = None):
        self.templates_dir = Path(templates_dir)
        self.custom_dir = Path(custom_dir) if custom_dir else None
        if self.custom_dir:
            self.custom_dir.mkdir(parents=True, exist_ok=True)

    def list_templates(self) -> list[dict[str, Any]]:
        return self.search_templates()["items"]

    def search_templates(
        self,
        query: str | None = None,
        category: str | None = None,
        source: str | None = None,
        page: int = 1,
        per_page: int | None = None,
        sort_by: str = "title",
        favorite_names: list[str] | None = None,
        favorites_only: bool = False,
    ) -> dict[str, Any]:
        per_page = per_page or self.DEFAULT_PER_PAGE
        page = max(1, page)
        per_page = max(1, min(per_page, 100))

        items = self._collect_all_templates()
        items = self._filter_templates(items, query, category, source, favorite_names, favorites_only)
        items = self._sort_templates(items, sort_by)

        total = len(items)
        pages = max(1, math.ceil(total / per_page)) if total else 1
        page = min(page, pages)
        start = (page - 1) * per_page
        end = start + per_page

        return {
            "items": items[start:end],
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
            "query": query or "",
            "category": category or "",
            "source": source or "",
            "sort_by": sort_by,
            "favorites_only": favorites_only,
        }

    def get_categories(self) -> list[str]:
        categories = sorted({item["category"] for item in self._collect_all_templates() if item.get("category")})
        return categories

    def get_template(self, name: str) -> dict[str, Any]:
        path = self._resolve_path(name)
        if not path:
            raise FileNotFoundError(f"Template '{name}' not found")
        data = self._load_file(path)
        data["source"] = "custom" if self._is_custom_path(path) else "builtin"
        return data

    def get_template_raw(self, name: str) -> str:
        path = self._resolve_path(name)
        if not path:
            raise FileNotFoundError(f"Template '{name}' not found")
        return path.read_text(encoding="utf-8")

    def is_custom(self, name: str) -> bool:
        if not self.custom_dir:
            return False
        return (self.custom_dir / f"{name}.yaml").exists()

    def save_template(self, yaml_content: str, overwrite: bool = False) -> dict[str, Any]:
        if not self.custom_dir:
            raise TemplateError("Custom templates directory is not configured")

        try:
            data = yaml.safe_load(yaml_content) or {}
        except yaml.YAMLError as exc:
            raise TemplateError(f"Invalid YAML: {exc}") from exc

        self._validate(data, "custom template")
        name = data.get("name")
        if not name:
            raise TemplateError("Template must include a 'name' field")
        if not NAME_PATTERN.match(name):
            raise TemplateError("Template name may only contain letters, numbers, underscore and hyphen")

        path = self.custom_dir / f"{name}.yaml"
        builtin_path = self.templates_dir / f"{name}.yaml"
        if builtin_path.exists() and not overwrite:
            raise TemplateError(f"Template name '{name}' conflicts with a built-in template")
        if path.exists() and not overwrite:
            raise TemplateError(f"Template '{name}' already exists")

        path.write_text(yaml_content.strip() + "\n", encoding="utf-8")
        return self.get_template(name)

    def update_template(self, name: str, yaml_content: str) -> dict[str, Any]:
        if not self.is_custom(name):
            raise TemplateError("Only custom templates can be edited")
        data = yaml.safe_load(yaml_content) or {}
        self._validate(data, "custom template")
        new_name = data.get("name", name)
        if new_name != name and not NAME_PATTERN.match(new_name):
            raise TemplateError("Template name may only contain letters, numbers, underscore and hyphen")
        if new_name != name and (self.templates_dir / f"{new_name}.yaml").exists():
            raise TemplateError(f"Template name '{new_name}' conflicts with a built-in template")

        target = self.custom_dir / f"{new_name}.yaml"
        target.write_text(yaml_content.strip() + "\n", encoding="utf-8")
        if new_name != name:
            old_path = self.custom_dir / f"{name}.yaml"
            old_path.unlink(missing_ok=True)
        return self.get_template(new_name)

    def delete_template(self, name: str) -> None:
        if not self.is_custom(name):
            raise TemplateError("Only custom templates can be deleted")
        path = self.custom_dir / f"{name}.yaml"
        path.unlink(missing_ok=True)

    def fork_template(self, name: str) -> str:
        if not self.custom_dir:
            raise TemplateError("Custom templates directory is not configured")
        raw = self.get_template_raw(name)
        base = f"{name}_copy"
        new_name = base
        index = 1
        while self._resolve_path(new_name):
            index += 1
            new_name = f"{base}_{index}"

        if re.search(r"^name:\s*.+$", raw, flags=re.MULTILINE):
            raw = re.sub(r"^name:\s*.+$", f"name: {new_name}", raw, count=1, flags=re.MULTILINE)
        else:
            raw = f"name: {new_name}\n{raw}"

        if re.search(r"^title:\s*.+$", raw, flags=re.MULTILINE):
            raw = re.sub(
                r"^title:\s*(.+)$",
                lambda match: f"title: {match.group(1).strip()} (副本)",
                raw,
                count=1,
                flags=re.MULTILINE,
            )

        self.save_template(raw, overwrite=False)
        return new_name

    def validate_yaml(self, yaml_content: str) -> dict[str, Any]:
        try:
            data = yaml.safe_load(yaml_content) or {}
        except yaml.YAMLError as exc:
            raise TemplateError(f"Invalid YAML: {exc}") from exc
        self._validate(data, "custom template")
        return {
            "valid": True,
            "name": data.get("name"),
            "title": data.get("title"),
            "question_count": len(data.get("questions") or []),
        }

    def _collect_all_templates(self) -> list[dict[str, Any]]:
        templates: list[dict[str, Any]] = []
        seen: set[str] = set()

        for path, source in self._iter_template_paths():
            name = path.stem
            if name in seen:
                continue
            seen.add(name)
            data = self._load_file(path)
            stat = path.stat()
            templates.append(
                {
                    "name": data.get("name", name),
                    "title": data.get("title", name),
                    "description": data.get("description", ""),
                    "icon": data.get("icon", "📜"),
                    "category": data.get("category", "general"),
                    "tags": data.get("tags") or [],
                    "source": source,
                    "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                }
            )

        return templates

    def _sort_templates(self, items: list[dict[str, Any]], sort_by: str) -> list[dict[str, Any]]:
        if sort_by == "updated":
            return sorted(items, key=lambda item: item.get("updated_at", ""), reverse=True)
        if sort_by == "category":
            return sorted(items, key=lambda item: (item.get("category", ""), item.get("title", "").lower()))
        if sort_by == "name":
            return sorted(items, key=lambda item: item.get("name", "").lower())
        return sorted(items, key=lambda item: item.get("title", "").lower())

    def _filter_templates(
        self,
        items: list[dict[str, Any]],
        query: str | None,
        category: str | None,
        source: str | None,
        favorite_names: list[str] | None = None,
        favorites_only: bool = False,
    ) -> list[dict[str, Any]]:
        result = items
        if favorites_only and favorite_names is not None:
            favorites = set(favorite_names)
            result = [item for item in result if item["name"] in favorites]
        if category:
            result = [item for item in result if item.get("category") == category]
        if source in {"builtin", "custom"}:
            result = [item for item in result if item.get("source") == source]
        if query:
            q = query.strip().lower()
            result = [
                item
                for item in result
                if q in item["name"].lower()
                or q in item["title"].lower()
                or q in item.get("description", "").lower()
                or q in item.get("category", "").lower()
                or any(q in tag.lower() for tag in item.get("tags", []))
            ]
        return result

    def _iter_template_paths(self) -> list[tuple[Path, str]]:
        paths: list[tuple[Path, str]] = []
        if self.templates_dir.exists():
            for path in sorted(self.templates_dir.glob("*.yaml")):
                paths.append((path, "builtin"))
        if self.custom_dir and self.custom_dir.exists():
            for path in sorted(self.custom_dir.glob("*.yaml")):
                paths.append((path, "custom"))
        return paths

    def _resolve_path(self, name: str) -> Path | None:
        if self.custom_dir:
            custom_path = self.custom_dir / f"{name}.yaml"
            if custom_path.exists():
                return custom_path
        builtin_path = self.templates_dir / f"{name}.yaml"
        if builtin_path.exists():
            return builtin_path
        return None

    def _is_custom_path(self, path: Path) -> bool:
        return self.custom_dir is not None and path.parent.resolve() == self.custom_dir.resolve()

    def _load_file(self, path: Path) -> dict[str, Any]:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}

        self._validate(data, path.name)
        data.setdefault("name", path.stem)
        return data

    def _validate(self, data: dict[str, Any], filename: str) -> None:
        if "script" not in data:
            raise ValueError(f"Template {filename} missing required 'script' field")

        questions = data.get("questions", [])
        if not isinstance(questions, list):
            raise ValueError(f"Template {filename} 'questions' must be a list")

        for question in questions:
            qtype = question.get("type", "string")
            if qtype not in self.QUESTION_TYPES:
                raise ValueError(
                    f"Template {filename} question '{question.get('name')}' "
                    f"has invalid type '{qtype}'"
                )
            if "name" not in question:
                raise ValueError(f"Template {filename} has question without 'name'")
            if qtype in {"choice", "multi"} and not question.get("choices"):
                raise ValueError(
                    f"Template {filename} question '{question['name']}' "
                    "requires 'choices'"
                )
