"""轻量版本化迁移执行器。

约定：
- 迁移文件放在 app/db/migrations/ 下，命名 `NNNN_description.sql`
- 同名 `.down.sql` 为可选回滚脚本（`NNNN_description.down.sql`）
- 每个文件在**单个事务**内执行，成功后才写入 schema_migrations；
  任一语句失败则整体回滚，不会留下半截 schema。

命令行用法：
    python -m app.db.migrate            # 升级到最新
    python -m app.db.migrate --status   # 查看已应用版本
    python -m app.db.migrate --down 3   # 回滚到版本 3
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from app.core.config import settings
from app.db.session import engine

logger = logging.getLogger("app.migrate")

_FILE_RE = re.compile(r"^(\d{4})_([A-Za-z0-9_\-]+)\.sql$")
_VERSION_RE = re.compile(r"^(\d{4})_.*\.down\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    down_path: Path | None

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()[:16]


def discover_migrations(directory: Path | None = None) -> list[Migration]:
    directory = directory or settings.migrations_dir
    if not directory.exists():
        return []

    down_map: dict[int, Path] = {}
    up_files: list[tuple[int, str, Path]] = []

    for path in sorted(directory.glob("*.sql")):
        down_match = _VERSION_RE.match(path.name)
        if down_match:
            down_map[int(down_match.group(1))] = path
            continue
        up_match = _FILE_RE.match(path.name)
        if not up_match:
            logger.warning("跳过不符合命名规范的迁移文件: %s", path.name)
            continue
        up_files.append((int(up_match.group(1)), up_match.group(2), path))

    migrations = [
        Migration(version=v, name=n, path=p, down_path=down_map.get(v))
        for v, n, p in sorted(up_files, key=lambda item: item[0])
    ]

    versions = [m.version for m in migrations]
    if len(versions) != len(set(versions)):
        raise RuntimeError(f"迁移版本号重复: {versions}")
    return migrations


def _ensure_registry(conn) -> None:  # noqa: ANN001
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     INTEGER PRIMARY KEY,
                name        VARCHAR(128) NOT NULL,
                checksum    VARCHAR(32)  NOT NULL,
                applied_at  VARCHAR(32)  NOT NULL
            )
            """
        )
    )


def _applied(conn) -> dict[int, str]:  # noqa: ANN001
    rows = conn.execute(text("SELECT version, checksum FROM schema_migrations")).fetchall()
    return {int(row[0]): str(row[1]) for row in rows}


def _split_statements(sql: str) -> list[str]:
    """按分号切分语句。

    说明：本项目的迁移脚本不包含触发器/存储过程等需要保留分号的语句，
    因此使用朴素切分；若未来引入复杂 DDL，请改用完整 SQL 解析器。
    """
    cleaned_lines: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        cleaned_lines.append(line)
    raw = "\n".join(cleaned_lines)
    return [stmt.strip() for stmt in raw.split(";") if stmt.strip()]


def upgrade(target: int | None = None) -> list[int]:
    migrations = discover_migrations()
    applied_versions: list[int] = []

    with engine.begin() as conn:
        _ensure_registry(conn)
        current = _applied(conn)

    for migration in migrations:
        if migration.version in current:
            if current[migration.version] != migration.checksum:
                raise RuntimeError(
                    f"迁移 {migration.version:04d} 在已应用后被修改（checksum 不一致），"
                    "请新增一个迁移文件而不是修改历史文件。"
                )
            continue
        if target is not None and migration.version > target:
            break

        logger.info("应用迁移 %04d_%s", migration.version, migration.name)
        statements = _split_statements(migration.path.read_text(encoding="utf-8"))
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))
            conn.execute(
                text(
                    "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
                    "VALUES (:v, :n, :c, :t)"
                ),
                {
                    "v": migration.version,
                    "n": migration.name,
                    "c": migration.checksum,
                    "t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                },
            )
        applied_versions.append(migration.version)

    return applied_versions


def downgrade(to_version: int) -> list[int]:
    migrations = {m.version: m for m in discover_migrations()}
    reverted: list[int] = []

    with engine.begin() as conn:
        _ensure_registry(conn)
        current = sorted(_applied(conn).keys(), reverse=True)

    for version in current:
        if version <= to_version:
            break
        migration = migrations.get(version)
        if migration is None or migration.down_path is None:
            raise RuntimeError(
                f"迁移 {version:04d} 缺少对应的 .down.sql，无法自动回滚。"
                "请手工处理后再执行。"
            )
        logger.info("回滚迁移 %04d_%s", version, migration.name)
        statements = _split_statements(migration.down_path.read_text(encoding="utf-8"))
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))
            conn.execute(text("DELETE FROM schema_migrations WHERE version = :v"), {"v": version})
        reverted.append(version)

    return reverted


def status() -> list[tuple[int, str, bool]]:
    migrations = discover_migrations()
    with engine.begin() as conn:
        _ensure_registry(conn)
        current = _applied(conn)
    return [(m.version, m.name, m.version in current) for m in migrations]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="开发工具箱数据库迁移")
    parser.add_argument("--status", action="store_true", help="显示迁移状态")
    parser.add_argument("--down", type=int, metavar="VERSION", help="回滚到指定版本")
    parser.add_argument("--target", type=int, metavar="VERSION", help="升级到指定版本")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.status:
        for version, name, applied in status():
            print(f"[{'x' if applied else ' '}] {version:04d}_{name}")
        return 0

    if args.down is not None:
        reverted = downgrade(args.down)
        print(f"已回滚: {reverted or '无'}")
        return 0

    applied = upgrade(args.target)
    print(f"已应用: {applied or '无（已是最新）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
