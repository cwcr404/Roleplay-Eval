"""V1 攻略库解析器。

V1 攻略文件是『标签化数据字典』结构,形如:
    [标签A] 内容行1
    内容续行...     ← 缩进或无标签的内容,归属上一个标签块
    [标签B] ...

我们把每一行按标签分块,解析成 GuideEntry(见 models.py)。
设计意图:标签即检索键,命中标签即整块取出 -> 天然适合查表,无需向量。

解析策略(保守、可复现):
- 以 `[xxx]` 开头的行 = 新块起点(标签 = 方括号内原文,保留,不猜语义)
- 其余行追加到当前块
- 文件开头的纯标题行(无标签)忽略
"""
from __future__ import annotations

import re

_TAG_RE = re.compile(r"^\[([^\]]+)\]\s*(.*)$")


def parse_guide(text: str) -> list[dict]:
    """把 V1 攻略原文解析成条目 list。每条目: {tags, domain, content}。"""
    entries: list[dict] = []
    cur_tag: str | None = None
    cur_lines: list[str] = []
    cur_domain: str = ""

    def flush():
        nonlocal cur_lines
        if cur_tag and cur_lines:
            entries.append({
                "tags": [cur_tag],
                "domain": cur_domain,
                "content": "\n".join(cur_lines).strip(),
            })
        cur_lines = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _TAG_RE.match(line)
        if m:
            flush()
            cur_tag = m.group(1).strip()
            domain = cur_tag.split(":")[0].strip() if ":" in cur_tag else cur_tag
            cur_domain = domain
            tail = m.group(2).strip()
            if tail:
                cur_lines.append(tail)
        else:
            cur_lines.append(line)
    flush()
    return entries


def build_guide_index(entries: list[dict]) -> dict[str, list[dict]]:
    """构建 标签 -> 条目 的倒排索引(查表实现的地基)。

    除精确标签外,额外为每个标签的『主域』(如 "系统:武器" 的生命周期)建键,
    提升检索召回而不牺牲确定性。所有键都归一化为小写,便于大小写不敏感命中。
    """
    index: dict[str, list[dict]] = {}
    for e in entries:
        keys = set()
        for t in e["tags"]:
            keys.add(t.lower().strip())
            if ":" in t:
                keys.add(t.split(":", 1)[0].lower().strip())
                keys.add(t.split(":", 1)[1].lower().strip())
        if e["domain"]:
            keys.add(e["domain"].lower().strip())
        for k in keys:
            if k:
                index.setdefault(k, []).append(e)
    return index
