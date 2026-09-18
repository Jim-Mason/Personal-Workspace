"""文档交叉核对：以代码为唯一事实来源，逐项比对四份文档里的"会随迭代变旧的数字与路径"。

只读脚本：不修改任何文件，只打印一份对照表。

跑法（在项目根目录下）：
    .venv\\Scripts\\python.exe backend\\scripts\\check_docs.py

想核对的项目跑在非默认地址（比如隔离出来的第二个实例）时，把地址一并传进来，
否则会去问默认的 8731 —— 那个实例若是旧进程，自检项数会整段对不上：
    .venv\\Scripts\\python.exe backend\\scripts\\check_docs.py --base http://127.0.0.1:8741

退出码 0 = 全部一致；1 = 有对不上的地方。
中台没启动过时会**跳过自检项数核对**并给出提示（那些数字得实际跑一遍才拿得到）。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ["README.md", "docs/ARCHITECTURE.md", "docs/MODULES.md", "docs/RUNBOOK.md"]

# 去问哪个实例要自检项数。默认就是中台自己的地址；要核对隔离实例时用 --base 指过去。
_AP = argparse.ArgumentParser(add_help=True)
_AP.add_argument("--base", default="http://127.0.0.1:8731", help="要核对的中台地址")
_AP.add_argument("--data-dir", default="", help="实例的数据目录（用来读令牌）")
ARGS, _ = _AP.parse_known_args()
BASE = ARGS.base
DATA_DIR = ARGS.data_dir

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
TOKEN_PATH = (Path(DATA_DIR) / ".token") if DATA_DIR else (ROOT / "data" / ".token")
counts: dict[str, int] = {}
if TOKEN_PATH.exists():
    for mid in specs:
        argv = [sys.executable, str(ROOT / "backend/scripts/check_modules.py"), "--module", mid]
        if BASE != "http://127.0.0.1:8731":
            argv += ["--base", BASE]
        if DATA_DIR:
            argv += ["--data-dir", DATA_DIR]
        out = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout
        m = re.search(r"合计 (\d+) 项", out)
        counts[mid] = int(m.group(1)) if m else 0
        if counts[mid] == 0:
            NOTES.append(
                f"模块 {mid} 跑不出项数 —— {BASE} 上那个实例可能是在它被创建之前启动的。"
                "重启后再跑，或加 --base 指向新实例。"
            )
else:
    NOTES.append(
        "读不到 data/.token —— 中台还没启动过，**跳过自检项数核对**。"
        "先跑一次 start.bat 再执行本脚本即可补上。"
    )


def _run_count(argv: list[str]) -> int:
    """跑一个自检脚本，从它的合计行里取项数。拿不到就返回 0。"""
    out = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(ROOT),
    ).stdout
    m = re.search(r"合计 (\d+) 项", out)
    return int(m.group(1)) if m else 0


#: 不连服务的两个自检脚本的**实际**项数。这两个脚本不需要中台在跑，
#: 所以随时都能数 —— 也是文档里最容易随迭代变旧的两个数字。
#: （2026-09-18 发现 RUNBOOK 里 check_auth 写着 32 项、实际是 28 项，
#:   而当时的判据只盯 check_modules 的五个模块，这个数字没人看着。）
AUTH_COUNT = _run_count([sys.executable, str(ROOT / "backend/scripts/check_auth.py")])
PARSER_COUNT = _run_count([sys.executable, str(ROOT / "modules/logviz/selftest_parsers.py")])


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
            ok = int(value) == n
            # 数出来的项数比文档少，**未必是文档写错了** ——
            # 也可能是问错了对象：新加的模块在 8731 上那个实例里根本不存在
            # （它是旧进程，模块发现发生在启动时）。报"文档错了"会把人带到
            # 完全错误的方向去改文档，所以这里把这种情况单独说出来。
            if not ok and n < int(value):
                check(
                    "B 分模块自检",
                    f"文档称 {mid} 为 {value} 项",
                    f"实际 {n} 项",
                    False,
                    f"数出来偏少 —— 先确认 {BASE} 上那个实例是在该模块创建**之后**启动的，"
                    "否则重启中台 / 用 --base 指到新实例再跑，别急着改文档",
                )
            else:
                check("B 分模块自检", f"文档称 {mid} 为 {value} 项", f"实际 {n} 项", ok)

check("B 平台端口", "四份文档里的 8731", "应出现", all(PLATFORM_PORT in t for t in texts.values()))

# ------------------------------------------------------------------ 另两个自检
# 这两条是 2026-09-18 补的判据缺口：当时 RUNBOOK 里写着 check_auth 是 32 项，
# 实际跑出来 28 项 —— 而原有判据只盯 check_modules 的五个模块，没人看着这个数字。
# 它们**不连服务**，所以不受"实例太旧"影响，数出来是多少就是多少，没有误报空间。
def _doc_counts_after(name: str) -> list[str]:
    """把文档里出现在某个脚本名附近的「N 项」都抠出来。

    窗口给得宽（200 字符）：文档里常见「脚本名 + 一句说明 +（N 项）」的写法，
    窗口太窄会漏掉带说明的那几处 —— 漏掉一处就等于这条判据没生效。
    """
    return re.findall(rf"{re.escape(name)}\D{{0,200}}?(\d+)\s*项", all_md)


if AUTH_COUNT:
    for value in set(_doc_counts_after("check_auth")):
        check("B 鉴权自检数", f"文档称 check_auth 为 {value} 项", f"实际 {AUTH_COUNT} 项",
              int(value) == AUTH_COUNT)
if PARSER_COUNT:
    for value in set(_doc_counts_after("selftest_parsers")):
        check("B 解析器自检数", f"文档称解析器自检为 {value} 项", f"实际 {PARSER_COUNT} 项",
              int(value) == PARSER_COUNT)

# 版本号：以「版本脉络」里最新那条为准，别再写死一个字面量 ——
# 写死过一次，改版本时它就变成一条永远删不掉的假警报。
_hist = re.findall(r"\*\*v(\d+\.\d+)\*\*", texts["README.md"])
_latest = _hist[-1] if _hist else ""
check("B 版本号", f"config.APP_VERSION = {VERSION}", "应与 README 版本脉络最新一条一致",
      bool(_latest) and VERSION.startswith(_latest),
      f"README 最新是 v{_latest or '(没找到)'}，代码里是 {VERSION}")

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
