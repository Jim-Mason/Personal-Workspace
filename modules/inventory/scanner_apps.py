"""扫描本机已安装的应用。

两个来源，各有分工：

- **注册表 Uninstall 键**：已安装程序的权威来源，带版本、发布者、安装日期、
  估算占用和卸载命令。三处都要读（64 位 / 32 位 / 当前用户），
  漏掉 WOW6432Node 会少掉一大批 32 位程序。
- **开始菜单快捷方式**：补注册表覆盖不到的免安装 / 绿色软件。
  只登记 .lnk 文件路径本身，不需要解析它的指向 —— 启动时直接
  `os.startfile()` 交给系统处理即可。

全程只读，不写注册表、不动任何文件。
"""

from __future__ import annotations

import os
import winreg
from pathlib import Path

# (来源标识, 根键, 子键路径, 访问标志)
_HIVES: list[tuple[str, int, str, int]] = [
    (
        "HKLM64",
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
    ),
    (
        "HKLM32",
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        winreg.KEY_READ,
    ),
    (
        "HKCU",
        winreg.HKEY_CURRENT_USER,
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        winreg.KEY_READ,
    ),
]

# 这些 ReleaseType 是补丁，不是用户认知里的「我装了什么」。
_SKIP_RELEASE_TYPES = {
    "update",
    "security update",
    "hotfix",
    "servicepack",
    "service pack",
    "critical update",
}

# 开始菜单里这些快捷方式不是「一个应用」，是它的附属动作。
_SKIP_LNK_KEYWORDS = (
    "uninstall",
    "卸载",
    "帮助",
    "help",
    "readme",
    "read me",
    "文档",
    "documentation",
    "website",
    "官网",
    "homepage",
    "release notes",
    "更新日志",
    "repair",
    "修复",
    "license",
    "许可",
)


def _read_str(key, name: str) -> str | None:
    try:
        value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        # 注册表里常见带引号或带尾部空字符的脏数据，统一清掉。
        return text.strip('"').strip("\x00").strip() or None
    return str(value).strip() or None


def _read_dword(key, name: str) -> int | None:
    try:
        value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    if isinstance(value, int):
        return value
    return None


def _normalize_install_date(raw: str | None) -> str | None:
    """注册表里的 InstallDate 是 YYYYMMDD，转成人能读的样子。"""
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) != 8:
        return raw
    return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"


def _skip_reason(name: str | None, values: dict) -> str | None:
    if not name:
        return "no_display_name"
    if values.get("SystemComponent") == 1:
        return "system_component"
    if values.get("ParentKeyName"):
        return "child_component"
    release = (values.get("ReleaseType") or "").strip().lower()
    if release in _SKIP_RELEASE_TYPES:
        return "patch"
    return None


def scan_registry_apps() -> tuple[list[dict], int]:
    """读取三处 Uninstall 键。返回 (应用列表, 被跳过的条目数)。"""
    apps: list[dict] = []
    skipped = 0

    for tag, hive, subkey, access in _HIVES:
        try:
            root = winreg.OpenKey(hive, subkey, 0, access)
        except OSError:
            # HKCU 可能不存在，权限不足也需要静默跳过，不能让整次扫描失败。
            continue
        try:
            index = 0
            while True:
                try:
                    child_name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, child_name, 0, access) as child:
                        display_name = _read_str(child, "DisplayName")
                        values = {
                            "SystemComponent": _read_dword(child, "SystemComponent"),
                            "ParentKeyName": _read_str(child, "ParentKeyName"),
                            "ReleaseType": _read_str(child, "ReleaseType"),
                        }
                        if _skip_reason(display_name, values) is not None:
                            skipped += 1
                            continue
                        size_kb = _read_dword(child, "EstimatedSize")
                        apps.append(
                            {
                                "name": display_name,
                                "version": _read_str(child, "DisplayVersion"),
                                "publisher": _read_str(child, "Publisher"),
                                "install_date": _normalize_install_date(
                                    _read_str(child, "InstallDate")
                                ),
                                "install_location": _read_str(child, "InstallLocation"),
                                "uninstall_string": _read_str(child, "UninstallString"),
                                "size_bytes": size_kb * 1024 if size_kb else None,
                                "size_source": "registry" if size_kb else "unknown",
                                "source": "registry",
                                "source_key": f"{tag}|{child_name}",
                                "hive": tag,
                            }
                        )
                except OSError:
                    # 单个子键读不了不影响其它条目。
                    continue
        finally:
            winreg.CloseKey(root)

    return apps, skipped


def _start_menu_roots() -> list[tuple[str, Path]]:
    roots: list[tuple[str, Path]] = []
    program_data = os.environ.get("ProgramData")
    if program_data:
        roots.append(("all", Path(program_data) / "Microsoft/Windows/Start Menu/Programs"))
    app_data = os.environ.get("APPDATA")
    if app_data:
        roots.append(("user", Path(app_data) / "Microsoft/Windows/Start Menu/Programs"))
    return roots


def _should_skip_lnk(stem: str) -> bool:
    lowered = stem.lower()
    return any(keyword in lowered for keyword in _SKIP_LNK_KEYWORDS)


def scan_start_menu() -> list[dict]:
    """收集开始菜单快捷方式，仅登记 .lnk 路径本身。"""
    items: list[dict] = []
    seen: set[str] = set()

    for scope, root in _start_menu_roots():
        if not root.exists():
            continue
        try:
            lnk_paths = list(root.rglob("*.lnk"))
        except OSError:
            continue
        for lnk in lnk_paths:
            stem = lnk.stem.strip()
            if not stem or _should_skip_lnk(stem):
                continue
            try:
                relative = lnk.relative_to(root).as_posix()
            except ValueError:
                relative = lnk.name
            key = f"{scope}|{relative}"
            if key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "name": stem,
                    "path_or_url": str(lnk),
                    "source": "startmenu",
                    "source_key": key,
                    "size_source": "unknown",
                    "scope": scope,
                }
            )

    return items


def _dedupe_key(name: str) -> str:
    """归一化名称，用于判断开始菜单条目是否已出现在注册表里。"""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def scan_all() -> dict:
    """一次完整扫描。返回注册表应用、开始菜单条目，以及统计数字。"""
    registry_apps, skipped = scan_registry_apps()
    start_menu = scan_start_menu()

    known = {_dedupe_key(app["name"]) for app in registry_apps}
    extra_menu: list[dict] = []
    duplicates = 0
    for item in start_menu:
        if _dedupe_key(item["name"]) in known:
            duplicates += 1
            continue
        extra_menu.append(item)

    return {
        "registry_apps": registry_apps,
        "startmenu_extra": extra_menu,
        "stats": {
            "registry_total": len(registry_apps),
            "registry_skipped": skipped,
            "startmenu_total": len(start_menu),
            "startmenu_duplicated": duplicates,
            "startmenu_extra": len(extra_menu),
        },
    }
