"""解析器分派：认出这是哪种日志，然后交给它。

### 认不出来的情况怎么处理

**如实说认不出来，不硬套。** 一份 catalina.out 被当成 nginx 日志解析，
出来的报告比没有报告更糟 —— 用户会照着一堆假数字去排查。

所以这里的策略是：
1. 每种解析器都给出自己的 `sniff()` 分数（0~1）
2. 取分最高的那个，**但低于阈值就判为"认不出"**
3. 认不出时把"最像哪种、有多像"一起返回，界面上让用户手动指定

### 手动指定永远优先

自动识别是给人省事的，不是替人做决定的。用户可以强制指定类型 ——
尤其在第一版只支持三类的情况下（Tomcat catalina.out 就得手动挑 Java 应用日志）。
"""

from __future__ import annotations

from . import java, mysql, nginx

#: 支持的日志类型，供界面上的下拉框用
REGISTRY = {
    nginx.KIND: nginx,
    java.KIND: java,
    mysql.KIND: mysql,
}

#: 认出的最低把握。低于它就报"认不出来"
THRESHOLD = 0.35

#: 界面上的显示名
LABELS = {
    "nginx": "nginx access 日志",
    "java": "Java 应用日志",
    "mysql": "MySQL 慢查询日志",
    "unknown": "认不出类型",
}


def detect(rows: list[tuple[int, str]]) -> dict:
    """给一份文件打分，返回每种类型的把握与推荐结果。"""
    scores = {}
    for kind, module in REGISTRY.items():
        try:
            scores[kind] = round(float(module.sniff(rows)), 3)
        except Exception:  # noqa: BLE001 - 识别阶段不该因为某个解析器崩了就全废
            scores[kind] = 0.0

    # 一个不带空行的文件里，五种分数可能是 0；这时 best 会是 dict 里的第一个，
    # 那是"默认"而不是"认出"，所以要显式按分数判
    best = max(scores, key=lambda key: scores[key]) if scores else ""
    confident = bool(best) and scores[best] >= THRESHOLD
    return {
        "scores": scores,
        "best": best if confident else "",
        "confidence": scores.get(best, 0.0),
        "detected": confident,
        "labels": LABELS,
    }


def run(kind: str, rows: list[tuple[int, str]]) -> dict:
    """按指定类型解析。`kind` 必须是 REGISTRY 里的键。"""
    module = REGISTRY.get(kind)
    if module is None:
        raise ValueError(f"不支持的日志类型：{kind}")
    return module.parse(rows)


def rows_for(kind: str, report: dict, key: str, value: str) -> dict:
    module = REGISTRY.get(kind)
    if module is None:
        raise ValueError(f"不支持的日志类型：{kind}")
    return module.rows_for(report, key, value)
