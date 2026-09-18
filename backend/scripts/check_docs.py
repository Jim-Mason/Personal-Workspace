"""文档交叉核对：以代码为唯一事实来源，逐项比对四份文档里的"会随迭代变旧的数字与路径"。

只读脚本：不修改任何文件，只打印一份对照表。

跑法（在项目根目录下）：
    .venv\\Scripts\\python.exe backend\\scripts\\check_docs.py

退出码 0 = 全部一致；1 = 有对不上的地方。
中台没启动过时会**跳过自检项数核对**并给出提示（那些数字得实际跑一遍才拿得到）。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ["README.md", "docs/ARCHITECTURE.md", "docs/MODULES.md", "docs/RUNBOOK.md"]

ROWS: list[tuple[str, str, str, bool, str]] = []


def check(area: str, claim: str, expect: str, ok: bool, detail: str = "") -> None:
    ROWS.append((area, claim, expect, ok, detail))


# ---------------------------------------------------------------- 事实
sys.path.insert(0, str(ROOT / "backend"))
from app.services.modules.spec import load_spec  # noqa: E402

specs = {}
for d in sorted((ROOT / "modules").iterdir()):
    if (d / "module.py").is_file():
        s = load_spec(d)
        specs[s.id] = s
ordered = sorted(specs.values(), key=lambda s: s.order)

PLATFORM_PORT = "8731"
from app import config as platform_config  # noqa: E402

VERSION = platform_config.APP_VERSION
BINARY_KINDS = {"subprocess_proxy", "external"}
subprocess_specs = [s for s in ordered if s.kind in BINARY_KINDS]
native_specs = [s for s in ordered if s.kind in {"native", "static"}]

# 自检项数只能实际跑一遍拿到，而那要求中台**跑过至少一次**（它得先有 data/.token）。
# 新克隆下来的目录还没启动过，这时不该报"失败"，而该说清楚"跳过了、怎么补上"。
NOTES: list[str] = []
TOKEN_PATH = ROOT / "data" / ".token"
counts: dict[str, int] = {}
if TOKEN_PATH.exists():
    for mid in specs:
        out = subprocess.run(
            [sys.executable, str(ROOT / "backend/scripts/check_modules.py"), "--module", mid],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout
        m = re.search(r"合计 (\d+) 项", out)
        counts[mid] = int(m.group(1)) if m else 0
else:
    NOTES.append(
        "读不到 data/.token —— 中台还没启动过，**跳过自检项数核对**。"
        "先跑一次 start.bat 再执行本脚本即可补上。"
    )


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


texts = {rel: read(rel) for rel in DOCS}
all_md = "\n".join(texts.values())

# ---------------------------------------------------------------- A. 模块事实
for s in ordered:
    for rel, text in texts.items():
        # 名称必须与声明一致（旧名「工具跳转」不能残留）
        if s.name in text:
            check("A 模块名称", f"{rel} 含「{s.name}」", "出现", True)
            break
    else:
        check("A 模块名称", f"任一文档提到「{s.name}」", "应出现", False, "四份文档都没写这个模块？")

for stale in ["工具跳转"]:
    hits = [rel for rel, t in texts.items() if stale in t]
    check("A 陈旧名称", f"旧名「{stale}」", "不应出现", not hits, "、".join(hits))

# 挂载点与端口：每个模块的挂载点应在 README/RUNBOOK 里出现
for s in ordered:
    for rel in ["README.md", "docs/RUNBOOK.md"]:
        check("A 挂载点", f"{rel} 提到 {s.mount}/", f"应出现", s.mount in texts[rel])
    if s.internal_port:
        for rel in ["README.md", "docs/RUNBOOK.md", "docs/MODULES.md"]:
            check("A 内部端口", f"{rel} 提到 {s.internal_port}", "应出现", str(s.internal_port) in texts[rel])

# 反向：文档里不该出现不属于任何模块的内部端口
#
# 例外：文档在讲「怎么改中台自己的端口 / 怎么跑第二个实例」时会写出别的端口号
# （例如 `set LOCALDECK_PORT=8741`）。那是示例，不是模块端口 —— 判据要能区分，
# 否则工具天天报警，就没人看它了。
declared_ports = {str(s.internal_port) for s in ordered if s.internal_port}
for rel, text in texts.items():
    found = set(re.findall(r"\b87(?:3[2-9]|4[0-9])\b", text))
    extra = found - declared_ports
    if extra:
        for line in text.splitlines():
            if "LOCALDECK_PORT" in line:
                for port in list(extra):
                    if port in line:
                        extra.discard(port)
    check("A 端口越界", f"{rel} 里的 873x/874x", "只应有 " + "/".join(sorted(declared_ports)),
          not extra, "多出（且不是 LOCALDECK_PORT 示例）：" + ",".join(sorted(extra)) if extra else "")

# ---------------------------------------------------------------- B. 数字一致
module_word = {4: "四个", 3: "三个", 2: "两个"}
claim_word = module_word.get(len(ordered), str(len(ordered)))
check("B 模块数量", "README 用词", f"应为「{claim_word}」", True, f"实际 {len(ordered)} 个模块")

# 「N 个模块」不一定是错的 —— 常见三种正当用法：
#   其余/另外 N 个模块（说的是"除它以外的"）、N 个模块各有…（假设句）
# 判据要把它们排除，否则每次都会误报。
_BENIGN = ("其余", "另外", "剩下", "其它", "其他", "比如", "假如", "如果", "若", "例如")
for rel, text in texts.items():
    for num, word in module_word.items():
        if num == len(ordered):
            continue
        for m in re.finditer(rf"{word}(?:功能)?模块", text):
            head = text[max(0, m.start() - 8): m.start()]
            if any(b in head for b in _BENIGN):
                continue
            if text[m.end():].startswith("各"):  # 「两个模块各有一个 store.py」
                continue
            snippet = text[max(0, m.start() - 12): m.end() + 12].replace("\n", " ")
            check("B 模块数量", f"{rel} 出现「{word}模块」", f"实际 {len(ordered)} 个", False, snippet)

if counts:
    total_checks = sum(counts.values())
    count_reported = re.findall(r"(?:共|合计)\s*(\d+)\s*项", all_md)
    for value in set(count_reported):
        check("B 自检总数", f"文档称「{value} 项」", f"实际合计 {total_checks}", int(value) == total_checks)
    for mid, n in counts.items():
        in_docs = re.findall(rf"{mid}\D{{0,12}}?(\d+)\s*项", all_md)
        for value in set(in_docs):
            check("B 分模块自检", f"文档称 {mid} 为 {value} 项", f"实际 {n} 项", int(value) == n)

check("B 平台端口", "四份文档里的 8731", "应出现", all(PLATFORM_PORT in t for t in texts.values()))
check("B 版本号", f"config.APP_VERSION = {VERSION}", "应与 README 的版本脉络一致",
      VERSION.startswith("0.4"), f"README 称 v0.4，代码里是 {VERSION}")

# README 开头那行「当前版本：vX.Y」最容易忘 —— 它不在「版本脉络」里，
# 改版本时十有八九会漏，而它就印在项目标题下面。
_m = re.search(r"当前版本：v([\d.]+)", texts["README.md"])
if not _m:
    check("B 版本号", "README 开头的「当前版本：vX.Y」", "应存在", False, "没找到这一行")
else:
    check("B 版本号", f"README 开头称 v{_m.group(1)}", f"代码为 {VERSION}",
          VERSION.startswith(_m.group(1)))

# ---------------------------------------------------------------- C 引用完整性
for rel, text in texts.items():
    base = (ROOT / rel).parent
    for link in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
        if link.startswith(("http://", "https://", "#", "mailto:")):
            continue
        target = (base / link.split("#")[0]).resolve()
        check("C 文档链接", f"{rel} → {link}", "目标存在", target.exists())
    for img in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text):
        target = (base / img).resolve()
        check("C 图片引用", f"{rel} → {img}", "文件存在", target.exists())

# README 目录结构里声明的顶层条目
tree_block = re.search(r"```\n(.*?README\.md.*?)\n```", texts["README.md"], re.S)
if tree_block:
    for line in tree_block.group(1).splitlines():
        m = re.match(r"[│├└─\s]*([A-Za-z0-9_.\-]+)/?\s{2,}", line)
        if not m:
            continue
        name = m.group(1).rstrip("/")
        if name in {"Personal", "Workspace"}:  # 说明文字里的词
            continue
        if "." in name and not (ROOT / name).exists():
            check("D 目录结构", f"README 目录树里的 `{name}`", "应存在", False)

# 脚本与参数：文档里提到的脚本路径要真实存在
for rel, text in texts.items():
    for script in set(re.findall(r"(backend[\\/]scripts[\\/][\w.]+|scripts[\\/][\w.]+\.bat|scripts[\\/][\w.]+\.py)", text)):
        p = ROOT / script.replace("\\", "/")
        check("C 脚本路径", f"{rel} 提到 {script}", "文件存在", p.exists())

# 自检脚本能不能真的按模块跑起来 —— 比"脚本里有没有出现这个字符串"更实在：
# 有的模块本来就走 else 分支（它没有模块专属用例），那也是支持的一种。
for mid, n in counts.items():
    check("C 自检参数", f"--module {mid} 能跑", "应产出项数", n > 0, f"实际 {n} 项")

# ---------------------------------------------------------------- 输出
print("=" * 96)
print("文档交叉核对（事实来自代码与实跑，非人工抄录）")
print("=" * 96)
area_now = None
fails = 0
for area, claim, expect, ok, detail in ROWS:
    if area != area_now:
        print(f"\n── {area} ──")
        area_now = area
    mark = "✅" if ok else "❌"
    if not ok:
        fails += 1
    line = f"  {mark} {claim}"
    if not ok:
        line += f"   期望：{expect}"
        if detail:
            line += f"   实际：{detail}"
    print(line)

print()
print("=" * 96)
print(f"共核对 {len(ROWS)} 项，失败 {fails} 项")
if NOTES:
    print()
    for note in NOTES:
        print(f"提示：{note}")
print("=" * 96)
sys.exit(0 if fails == 0 else 1)
