"""Safe expression evaluator for template computed variables."""

import ast
import re
from typing import Any


class ExpressionError(ValueError):
    pass


_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.BoolOp,
    ast.IfExp,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Attribute,
    ast.Subscript,
    ast.Slice,
    ast.Index,
    ast.Call,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.ListComp,
    ast.comprehension,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.And,
    ast.Or,
    ast.Not,
    ast.USub,
    ast.UAdd,
)

_SAFE_BUILTINS = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "len": len,
    "min": min,
    "max": max,
    "abs": abs,
    "round": round,
    "join": str.join,
}


def _validate_node(node: ast.AST) -> None:
    if not isinstance(node, _ALLOWED_NODES):
        raise ExpressionError(f"Unsupported expression construct: {type(node).__name__}")

    if isinstance(node, ast.Call):
        if not isinstance(node.func, (ast.Attribute, ast.Name)):
            raise ExpressionError("Only attribute and name calls are allowed")
        if isinstance(node.func, ast.Name) and node.func.id not in _SAFE_BUILTINS:
            raise ExpressionError(f"Function '{node.func.id}' is not allowed")

    for child in ast.iter_child_nodes(node):
        _validate_node(child)


def evaluate_expression(expr: str, context: dict[str, Any]) -> Any:
    """Evaluate a Python-like expression against a variable context."""
    expr = expr.strip()
    if expr.startswith("{{") and expr.endswith("}}"):
        expr = expr[2:-2].strip()

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"Invalid expression syntax: {expr}") from exc

    _validate_node(tree.body)
    safe_context = {**_SAFE_BUILTINS, **context}
    try:
        return eval(compile(tree, "<expression>", "eval"), {"__builtins__": {}}, safe_context)
    except Exception as exc:
        raise ExpressionError(f"Failed to evaluate '{expr}': {exc}") from exc


def resolve_computed(computed: dict[str, str], context: dict[str, Any]) -> dict[str, Any]:
    """Resolve computed variables, supporting references to other computed values."""
    result = dict(context)
    pending = dict(computed or {})
    max_passes = len(pending) + 1

    for _ in range(max_passes):
        if not pending:
            break
        resolved_keys = []
        for key, expr in pending.items():
            try:
                result[key] = evaluate_expression(expr, result)
                resolved_keys.append(key)
            except ExpressionError:
                continue
        for key in resolved_keys:
            del pending[key]

    if pending:
        unresolved = ", ".join(pending.keys())
        raise ExpressionError(f"Could not resolve computed variables: {unresolved}")

    return {k: result[k] for k in (computed or {})}


def interpolate_string(value: str, context: dict[str, Any]) -> str:
    """Replace {{ expr }} placeholders inside a string."""

    def replacer(match: re.Match[str]) -> str:
        result = evaluate_expression(match.group(1), context)
        return "" if result is None else str(result)

    return re.sub(r"\{\{\s*(.+?)\s*\}\}", replacer, value)
