"""类型化错误体系 + 全局错误处理。

对外只暴露规范化的错误结构：
    {"error": {"code": "...", "message": "...", "request_id": "...", "details": [...]}}
绝不把堆栈或内部实现细节返回给客户端。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_request_id

logger = logging.getLogger("app.error")


class AppError(Exception):
    """业务错误基类。"""

    code: str = "internal_error"
    http_status: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    message: str = "服务内部错误"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        http_status: int | None = None,
        details: Any = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.http_status = http_status or self.http_status
        self.details = details
        super().__init__(self.message)


class BadRequestError(AppError):
    code = "bad_request"
    http_status = status.HTTP_400_BAD_REQUEST
    message = "请求参数不合法"


class UnauthorizedError(AppError):
    code = "unauthorized"
    http_status = status.HTTP_401_UNAUTHORIZED
    message = "未登录或登录已过期"


class ForbiddenError(AppError):
    code = "forbidden"
    http_status = status.HTTP_403_FORBIDDEN
    message = "没有权限执行该操作"


class NotFoundError(AppError):
    code = "not_found"
    http_status = status.HTTP_404_NOT_FOUND
    message = "资源不存在"


class ConflictError(AppError):
    code = "conflict"
    http_status = status.HTTP_409_CONFLICT
    message = "资源冲突"


class UpstreamError(AppError):
    """上游（被代理的内网服务）返回错误或不可达。"""

    code = "upstream_error"
    http_status = status.HTTP_502_BAD_GATEWAY
    message = "目标内网服务不可达"


def _payload(
    code: str, message: str, *, details: Any = None, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    if extra:
        error.update(extra)
    request_id = get_request_id()
    if request_id:
        error["request_id"] = request_id
    return {"error": error}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        if exc.http_status >= 500:
            logger.exception("业务异常(5xx): %s", exc.code)
        else:
            logger.warning("业务异常(%s): %s", exc.code, exc.message)
        return JSONResponse(
            status_code=exc.http_status,
            content=_payload(exc.code, exc.message, details=exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {
                "field": ".".join(str(part) for part in err.get("loc", ()) if part != "body"),
                "reason": err.get("msg", ""),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,  # 不用 status.HTTP_422_* 常量：Starlette 已将其重命名并标弃用
            content=_payload("validation_error", "请求参数校验失败", details=details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
        }.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) else "请求失败"
        return JSONResponse(status_code=exc.status_code, content=_payload(code, message))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_payload("internal_error", "服务内部错误，请查看服务端日志"),
        )
