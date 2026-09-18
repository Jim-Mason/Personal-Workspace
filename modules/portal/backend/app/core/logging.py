"""结构化日志：统一 JSON 输出 + 请求 ID 贯穿全链路。"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
import uuid
from typing import Any

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")

# LogRecord 的内置属性，用于识别业务自定义 extra
_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
    "message", "asctime",
}

# 明确不落盘的字段，避免令牌/密码进入日志
_REDACT_KEYS = {"password", "passwd", "token", "access_token", "refresh_token", "secret", "authorization"}


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def set_request_id(value: str) -> contextvars.Token:
    return _request_id.set(value)


def reset_request_id(token: contextvars.Token) -> None:
    _request_id.reset(token)


def get_request_id() -> str:
    return _request_id.get()


class JsonFormatter(logging.Formatter):
    def __init__(self, *, with_extras: bool = True) -> None:
        super().__init__()
        self.with_extras = with_extras

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        rid = getattr(record, "request_id", None) or get_request_id()
        if rid:
            payload["request_id"] = rid

        if self.with_extras:
            for key, value in record.__dict__.items():
                if key in _RESERVED or key == "request_id" or key.startswith("_"):
                    continue
                if key.lower() in _REDACT_KEYS:
                    payload[key] = "***"
                else:
                    payload[key] = _safe(value)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rid = getattr(record, "request_id", None) or get_request_id()
        prefix = f"[{rid}] " if rid else ""
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<8} {prefix}{record.getMessage()}"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def _safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value][:50]
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in list(value.items())[:50]}
    return str(value)


def setup_logging(*, level: str = "INFO", as_json: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if as_json else PlainFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # 降低第三方噪音
    for noisy in ("uvicorn.access", "httpx", "httpcore", "websockets.client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(level.upper())

    # uvicorn 自带 handler 会绕过我们的 JSON 格式，统一清掉
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
