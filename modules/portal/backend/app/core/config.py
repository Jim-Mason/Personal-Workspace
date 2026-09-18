"""集中配置：全部来自环境变量，启动时校验并快速失败。

任何缺失的关键配置（如 SECRET_KEY）都会在进程启动阶段直接抛错，
而不是等到运行期某个请求才 500。
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> backend/
BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = BACKEND_DIR.parent

_INSECURE_SECRET_PLACEHOLDERS = {
    "",
    "change-me",
    "changeme",
    "secret",
    "your-secret-key",
    "please-change-this-secret-key",
}


class Settings(BaseSettings):
    """应用配置。

    所有字段均可通过环境变量或 .env 文件覆盖（大小写不敏感）。
    """

    model_config = SettingsConfigDict(
        env_file=(PROJECT_DIR / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 基础 ----------
    app_name: str = "开发工具箱"
    app_version: str = "1.0.0"
    env: str = Field(default="dev", description="dev | test | prod")
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    root_path: str = ""

    # ---------- 安全 / 认证 ----------
    secret_key: str = Field(default="")
    access_token_expire_minutes: int = 60
    refresh_token_expire_days: int = 7
    cookie_secure: bool = False
    cookie_samesite: str = "lax"
    cookie_domain: str | None = None

    # 首次启动时自动创建的管理员
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = Field(default="")
    allow_bootstrap_admin: bool = True

    # ---------- 挂在中台后面时的免登录 ----------
    # 打开后，**经中台代理而来**的请求视为已登录，不再弹登录页。
    #
    # 为什么需要：这个模块挂在中台 `/portal/` 下时，中台自己已经有完整的
    # 边界（只绑 127.0.0.1 + Host 校验 + 访问令牌），再叠一层登录页只是
    # 让本机自用的自己多敲一次密码。
    #
    # ⚠️ **独立运行（不经中台、直接跑 uvicorn）时绝不要打开**：
    # 那时外面没有任何上游边界，免登录等于把门户和它代理的全部内网系统
    # 向任何能访问该端口的人敞开。中台只在启动本模块时注入这个开关，
    # 所以默认值必须是 False。
    trust_local_proxy: bool = False
    # 免登录时以谁的身份行事。留空则取用户名最小的那个管理员。
    trust_username: str = "admin"

    # ---------- 数据库 ----------
    database_url: str = ""
    db_path: str = ""
    sql_echo: bool = False

    # ---------- CORS（仅前后端分离开发时需要） ----------
    cors_origins: list[str] = Field(default_factory=list)

    # ---------- 反向代理 ----------
    proxy_enabled: bool = True
    proxy_timeout_seconds: float = 60.0
    proxy_connect_timeout_seconds: float = 10.0
    proxy_max_redirects: int = 5
    proxy_verify_tls: bool = False
    # 允许代理到私有/内网地址（跳板机场景通常必须为 True）
    proxy_allow_private_targets: bool = True
    # 可选白名单：非空时只允许代理到这些 host（支持 *.example.com 通配）
    proxy_allowed_hosts: list[str] = Field(default_factory=list)
    # 是否重写 HTML 中的根路径资源地址（让内嵌页面能加载 CSS/JS）
    proxy_rewrite_html: bool = True
    # 是否剥离上游的 X-Frame-Options / CSP，便于 iframe 或跨端嵌入
    proxy_strip_security_headers: bool = True
    # 是否重命名上游 Cookie 加 slug 前缀，避免多个内网系统 Cookie 互相覆盖
    proxy_cookie_namespace: bool = False
    # 是否读取系统 HTTP_PROXY/HTTPS_PROXY 环境变量（内网转发通常应关闭）
    proxy_trust_env: bool = False
    # 请求体最大缓冲（字节），超出直接拒绝，防止大文件上传打爆内存
    proxy_max_body_bytes: int = 64 * 1024 * 1024

    # ---------- 卡片图标上传 ----------
    uploads_enabled: bool = True
    # 图标单文件上限（字节），默认 512KB；图标本身很小，卡死上限可防滥用
    upload_max_icon_bytes: int = 512 * 1024
    # 卡片配置导入文件上限（字节），默认 5MB：一张卡片约 1KB，5000 行也远小于此
    upload_max_import_bytes: int = 5 * 1024 * 1024
    # 上传根目录，留空则用 <项目>/data/uploads
    uploads_dir: str = ""

    # ---------- 日志 ----------
    log_level: str = "INFO"
    log_json: bool = True

    # ------------------------------------------------------------------
    @field_validator("cors_origins", "proxy_allowed_hosts", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """支持 `CORS_ORIGINS=a,b` 这种逗号分隔写法。"""
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith("["):
                return value  # 交给 pydantic 解析 JSON
            return [item.strip() for item in text.split(",") if item.strip()]
        return value

    @field_validator("cookie_samesite")
    @classmethod
    def _check_samesite(cls, value: str) -> str:
        allowed = {"lax", "strict", "none"}
        lowered = value.lower()
        if lowered not in allowed:
            raise ValueError(f"COOKIE_SAMESITE 必须是 {allowed} 之一，当前为 {value!r}")
        return lowered

    @model_validator(mode="after")
    def _ensure_secret_key(self) -> "Settings":
        if not self.secret_key or self.secret_key.strip().lower() in _INSECURE_SECRET_PLACEHOLDERS:
            if self.is_production:
                raise ValueError(
                    "生产环境必须显式设置 SECRET_KEY（建议 `openssl rand -hex 32` 生成）。"
                )
            # 开发环境自动生成一个临时 key，避免开箱即用的门槛过高
            self.secret_key = secrets.token_hex(32)
        if len(self.secret_key) < 16 and self.is_production:
            raise ValueError("SECRET_KEY 长度至少 16 位。")
        if self.cookie_samesite == "none" and not self.cookie_secure:
            raise ValueError("COOKIE_SAMESITE=none 时必须同时设置 COOKIE_SECURE=true。")
        return self

    @model_validator(mode="after")
    def _ensure_database_url(self) -> "Settings":
        if self.database_url:
            return self
        if self.db_path:
            raw = self.db_path
        else:
            raw = str(PROJECT_DIR / "data" / "devtoolbox.db")
        path = Path(raw)
        if not path.is_absolute():
            path = (PROJECT_DIR / path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.database_url = f"sqlite:///{path.as_posix()}"
        self.db_path = str(path)
        return self

    # ------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.env.lower() in {"prod", "production"}

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def static_dir(self) -> Path:
        return BACKEND_DIR / "app" / "static"

    @property
    def uploads_path(self) -> Path:
        """上传根目录（绝对路径），不存在时自动创建。"""
        raw = self.uploads_dir.strip() if self.uploads_dir else ""
        path = Path(raw) if raw else (PROJECT_DIR / "data" / "uploads")
        if not path.is_absolute():
            path = (PROJECT_DIR / path).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def icons_path(self) -> Path:
        """卡片图标目录。"""
        path = self.uploads_path / "icons"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def migrations_dir(self) -> Path:
        return BACKEND_DIR / "app" / "db" / "migrations"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。"""
    return Settings()


settings = get_settings()
