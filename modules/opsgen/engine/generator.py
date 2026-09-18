"""Generate executable scripts from YAML templates and user parameters."""

from typing import Any

from jinja2 import Environment, StrictUndefined

from .expressions import ExpressionError, resolve_computed
from .loader import TemplateLoader


class ValidationError(ValueError):
    pass


class ScriptGenerator:
    def __init__(self, loader: TemplateLoader):
        self.loader = loader
        self.jinja = Environment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)

    def generate(self, template_name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        template = self.loader.get_template(template_name)
        normalized = self._normalize_params(template, params or {})
        self._validate_params(template, normalized)

        context = dict(normalized)
        computed = template.get("computed") or {}
        if computed:
            try:
                computed_values = resolve_computed(computed, context)
                context.update(computed_values)
            except ExpressionError as exc:
                raise ValidationError(str(exc)) from exc

        script_template = self.jinja.from_string(template["script"])
        script = script_template.render(**context)

        outputs = {"script": script.strip() + "\n"}
        for extra in template.get("extra_outputs") or []:
            name_template = self.jinja.from_string(extra["name"])
            name = name_template.render(**context)
            content_template = self.jinja.from_string(extra["content"])
            outputs[name] = content_template.render(**context).strip() + "\n"

        primary_key = template.get("primary_output", "script")
        if primary_key != "script" and "{{" in primary_key:
            primary_key = self.jinja.from_string(primary_key).render(**context)

        return {
            "template": template_name,
            "title": template.get("title", template_name),
            "params": normalized,
            "outputs": outputs,
            "primary_output": primary_key,
        }

    def _normalize_params(self, template: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for question in template.get("questions", []):
            name = question["name"]
            qtype = question.get("type", "string")
            raw = params.get(name, question.get("default"))

            if raw is None or raw == "":
                if question.get("required", False) and self._is_visible(question, result):
                    continue
                if qtype == "bool":
                    result[name] = bool(question.get("default", False))
                elif qtype == "multi":
                    result[name] = list(question.get("default") or [])
                elif qtype == "int":
                    result[name] = question.get("default")
                else:
                    result[name] = question.get("default", "")
                continue

            if qtype == "bool":
                if isinstance(raw, bool):
                    result[name] = raw
                else:
                    result[name] = str(raw).lower() in {"1", "true", "yes", "on"}
            elif qtype == "int":
                result[name] = int(raw)
            elif qtype == "multi":
                if isinstance(raw, list):
                    result[name] = [str(item) for item in raw]
                elif isinstance(raw, str):
                    result[name] = [item.strip() for item in raw.split(",") if item.strip()]
                else:
                    result[name] = [str(raw)]
            elif qtype == "choice":
                result[name] = str(raw)
            else:
                result[name] = str(raw)

        return result

    def _validate_params(self, template: dict[str, Any], params: dict[str, Any]) -> None:
        errors: list[str] = []

        for question in template.get("questions", []):
            if not self._is_visible(question, params):
                continue

            name = question["name"]
            qtype = question.get("type", "string")
            value = params.get(name)

            if question.get("required", False) and (value is None or value == "" or value == []):
                errors.append(f"'{question.get('label', name)}' is required")
                continue

            if value is None:
                continue

            if qtype == "int":
                if not isinstance(value, int):
                    errors.append(f"'{name}' must be an integer")
                else:
                    min_val = question.get("min")
                    max_val = question.get("max")
                    if min_val is not None and value < min_val:
                        errors.append(f"'{name}' must be >= {min_val}")
                    if max_val is not None and value > max_val:
                        errors.append(f"'{name}' must be <= {max_val}")

            if qtype == "choice" and value not in (question.get("choices") or []):
                errors.append(f"'{name}' must be one of {question.get('choices')}")

            if qtype == "multi":
                choices = set(question.get("choices") or [])
                invalid = [item for item in value if item not in choices]
                if invalid:
                    errors.append(f"'{name}' has invalid choices: {', '.join(invalid)}")

            pattern = question.get("pattern")
            if pattern and isinstance(value, str):
                import re

                if not re.match(pattern, value):
                    errors.append(f"'{name}' format is invalid")

        if errors:
            raise ValidationError("; ".join(errors))

    def _is_visible(self, question: dict[str, Any], params: dict[str, Any]) -> bool:
        show_when = question.get("show_when")
        if not show_when:
            return True

        for field, expected in show_when.items():
            actual = params.get(field)
            if isinstance(expected, list):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True
