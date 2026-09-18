"""Text diff utilities for version comparison."""

import difflib
import html
from typing import Any


def unified_diff(text_a: str, text_b: str, label_a: str = "A", label_b: str = "B") -> str:
    lines_a = text_a.splitlines(keepends=True)
    lines_b = text_b.splitlines(keepends=True)
    if not lines_a:
        lines_a = ["\n"]
    if not lines_b:
        lines_b = ["\n"]
    return "".join(difflib.unified_diff(lines_a, lines_b, fromfile=label_a, tofile=label_b))


def diff_stats(text_a: str, text_b: str) -> dict[str, int]:
    diff = list(
        difflib.unified_diff(
            text_a.splitlines(),
            text_b.splitlines(),
            lineterm="",
        )
    )
    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    return {"added": added, "removed": removed, "changed_lines": added + removed}


def diff_to_html(diff_text: str) -> str:
    rows: list[str] = []
    for line in diff_text.splitlines():
        escaped = html.escape(line)
        if line.startswith("+++") or line.startswith("---"):
            rows.append(f'<div class="diff-line diff-meta">{escaped}</div>')
        elif line.startswith("@@"):
            rows.append(f'<div class="diff-line diff-hunk">{escaped}</div>')
        elif line.startswith("+"):
            rows.append(f'<div class="diff-line diff-add">{escaped}</div>')
        elif line.startswith("-"):
            rows.append(f'<div class="diff-line diff-del">{escaped}</div>')
        else:
            rows.append(f'<div class="diff-line diff-context">{escaped}</div>')
    return "\n".join(rows) if rows else '<div class="diff-line diff-context">无差异</div>'


def compare_payload(label_a: str, text_a: str, label_b: str, text_b: str) -> dict[str, Any]:
    diff = unified_diff(text_a, text_b, label_a, label_b)
    return {
        "label_a": label_a,
        "label_b": label_b,
        "stats": diff_stats(text_a, text_b),
        "identical": text_a == text_b,
        "diff": diff,
        "diff_html": diff_to_html(diff),
    }
