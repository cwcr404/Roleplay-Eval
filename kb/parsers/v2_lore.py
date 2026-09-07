"""V2 背景故事库解析器。

V2 文件是『连贯叙事 + 锚点标签』结构,形如:
    [芽衣·背景·家世与出身] 我叫雷电芽衣……
    [芽衣·视角·关于琪亚娜] ...

与 V1 的关键差异(V2 用『模块整块加载』而非碎片召回的根据):
- 每一块是一段**完整自洽的叙事单元**,内部语义咬合。
- 将一块切碎、按相关度 top-k 召回,会把连贯人格叙事打散成拼贴 -> 这正是
  向量 RAG 最不适合的形态。
- 因此 V2 检索 = **按主题/维度精确命中整块**,不碎片化、不做相似度召回。

解析策略:以 [芽衣·xxx·yyy] 为块起点,块内行合并成一段完整文本。
块头本身(如 芽衣·背景·家世与出身)携带三叉语义:
    persona(固定"芽衣")· 维度(背景/视角/认知…) · 主题(家世与出身/关于琪亚娜…)
我们借此建 主题/维度 两层索引,供按需整块带出。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_BLOCK_RE = re.compile(r"^\[([^\]]+)\]\s*(.*)$")


@dataclass
class LoreBlock:
    """一段 V2 叙事块。整块存储、整块检索。"""

    key: str               # 原始块头,如 "芽衣·背景·家世与出身"
    dimension: str         # "背景" / "视角" / "认知" …
    topic: str             # 主题去重后的人类可读主题
    text: str = ""         # 完整叙事文本(不含块头本身重复段)


def _split_key(key: str) -> tuple[str, str]:
    """把 '芽衣·背景·家世与出身' 拆成 (主域, 主题)。"""
    parts = [p.strip() for p in key.split("·") if p.strip()]
    # 去掉统一的 '芽衣' 前缀后,尽量取中段作维度、末段作主题
    if len(parts) >= 2 and parts[0] in ("芽衣", "芽衣视角", "芽衣·视角", "雷电芽衣"):
        parts = parts[1:]
    if len(parts) >= 2:
        # e.g. parts=['背景','家世与出身']
        return parts[0], "·".join(parts[1:])
    if len(parts) == 1:
        return parts[0], parts[0]
    return "背景", key


def parse_lore(text: str) -> list[LoreBlock]:
    blocks: list[LoreBlock] = []
    cur: LoreBlock | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = _BLOCK_RE.match(line)
        if m:
            if cur:
                blocks.append(cur)
            key = m.group(1).strip()
            dimension, topic = _split_key(key)
            head = m.group(2).strip()
            cur = LoreBlock(key=key, dimension=dimension, topic=topic, text=head)
        else:
            if cur is not None:
                cur.text = (cur.text + "\n" + line.strip()).strip()
    if cur:
        blocks.append(cur)
    return [b for b in blocks if b.text]


def build_lore_index(blocks: list[LoreBlock]) -> dict[str, list[LoreBlock]]:
    """主题/维度两级索引。检索时整块返回,不做相关性排序。"""
    idx: dict[str, list[LoreBlock]] = {}
    for b in blocks:
        for key in {b.topic.lower(), b.dimension.lower(), b.key.lower()}:
            idx.setdefault(key, []).append(b)
    return idx
