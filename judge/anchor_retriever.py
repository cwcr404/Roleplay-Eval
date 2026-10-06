"""锚点检索器 —— 按场景检索相关风格锚点,供 Judge 注入。

设计原则:
- 池子厚(49条),注入薄(5~8条) —— 防 prompt 臃肿
- 官方句权重高于起草句
- 场景硬匹配优先:命中场景才进候选,不靠来源权重兜底
- 无外部依赖(纯标准库,便于 v0.1 跑通);接口与 VectorRetrieverStub 同构,后续可换嵌入检索
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ANCHOR_FILE = (
    Path(__file__).resolve().parent.parent / "anchors" / "anchors_role_consistency_v3.json"
)
SCENE_FILE = Path(__file__).resolve().parent.parent / "anchors" / "anchors_scene_v1.json"

# ── 查询意图 → 锚点场景关键词 ────────────────────────────────
# 命中哪组意图,就只从对应的锚点场景里取;未命中任何意图 → 走兜底(全域按来源权重)
INTENT_ROUTES: dict[str, list[str]] = {
    "疲惫": ["疲惫", "劝休", "催促", "接手", "深夜"],
    "加班": ["疲惫", "劝休", "催促", "深夜", "归家"],
    "自责": ["自责", "开解", "安抚", "劝解"],
    "搞砸": ["自责", "开解", "安抚", "劝解", "失败"],
    "失败": ["自责", "开解", "安抚", "失败"],
    "难过": ["安慰", "开解", "承接", "劝解"],
    "委屈": ["劝解", "安慰", "承接"],
    "生病": ["失眠", "关怀", "照顾", "天冷"],
    "失眠": ["失眠", "关怀", "深夜"],
    "熬夜": ["深夜", "催促", "劝休", "责备"],
    "被夸": ["被夸", "自谦", "卸力", "示弱", "自持"],
    "夸": ["被夸", "自谦", "卸力", "自持"],
    "被逗": ["被逗弄", "软刺", "调侃", "提醒"],
    "戏弄": ["被逗弄", "软刺", "调侃", "提醒"],
    "越界": ["越界", "提醒", "不满"],
    "边界": ["越界", "提醒", "不满", "关系分层"],
    "邀约": ["邀约", "日常", "照顾"],
    "求助": ["求助", "承接", "帮解"],
    "卡住": ["帮解", "卡壳", "开解"],
    "冲突": ["软刺", "不满", "留白", "责备", "争"],
    "意见": ["不满", "留白", "责备"],
    "问候": ["问候", "开场", "日常", "晚间", "清晨"],
    "想你": ["关系分层", "特殊对待", "承接"],
    "亲近": ["关系分层", "特殊对待", "邀约"],
    "感谢": ["致谢", "拒客套", "承接"],
    "低落": ["劝解", "示弱", "开解", "承接"],
}

SOURCE_WEIGHT = {"official": 1.0, "official-transcribed": 0.9, "draft": 0.6}
K_DEFAULT = 6


@dataclass(frozen=True)
class Anchor:
    id: str
    text: str
    scene: str
    tags: tuple[str, ...]
    source: str
    intent: str = ""
    domain: str = "闲聊"

    def weight(self) -> float:
        return SOURCE_WEIGHT.get(self.source, 0.5)


def load_anchors(path: Path = ANCHOR_FILE) -> list[Anchor]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        Anchor(
            id=a.get("id", ""),
            text=a["text"],
            scene=a.get("scene", ""),
            tags=tuple(a.get("tags", [])),
            source=a.get("source", "draft"),
            intent=a.get("intent", ""),
            domain="闲聊",
        )
        for a in raw["anchors"]
    ]


def load_scene_anchors(path: Path = SCENE_FILE) -> list[Anchor]:
    """攻略/关系域锚点(推演,单独一份)。"""
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        Anchor(
            id=a.get("id", ""),
            text=a["text"],
            scene=a.get("domain", ""),
            tags=tuple(a.get("tags", [])),
            source="draft",
            intent=a.get("intent", ""),
            domain="攻略" if a.get("domain", "").startswith("攻略") else "关系",
        )
        for a in raw["anchors"]
    ]


def _match_intents(query: str) -> list[str]:
    """找出 query 命中的所有意图关键词。"""
    return [kw for kw in INTENT_ROUTES if kw in query]


def _scene_hit(anchor_scene: str, hints: Iterable[str]) -> bool:
    return any(h in anchor_scene for h in hints)


def retrieve(
    query: str,
    task_type: str = "闲聊",
    k: int = K_DEFAULT,
    anchors: Iterable[Anchor] | None = None,
) -> list[Anchor]:
    """返回与 query 最相关的 k 条锚点(按相关度降序)。

    策略:
    1. 命中意图 → 只从对应场景池取,按 (来源权重, 标签命中) 排序
    2. 未命中意图 → 兜底:全域按来源权重取(保底不空手)
    3. 攻略任务 → 优先取场景锚点(攻略域)
    """
    pool = list(anchors) if anchors is not None else load_anchors()
    if not pool:
        return []

    hits = _match_intents(query)

    if hits:
        hints: list[str] = []
        for h in hits:
            hints.extend(INTENT_ROUTES[h])
        matched = [a for a in pool if _scene_hit(a.scene, hints)]
        if matched:
            ranked = sorted(
                matched,
                key=lambda a: (-a.weight(), a.id),
            )
            return ranked[:k]
        # 命中意图但池里无对应场景 → 落兜底

    # 兜底:全域按来源权重(官方优先),按 id 稳定排序
    ranked = sorted(pool, key=lambda a: (-a.weight(), a.id))
    return ranked[:k]


def retrieve_for_case(
    query: str,
    task_type: str = "闲聊",
    k: int = K_DEFAULT,
) -> list[Anchor]:
    """面向真实 case 的入口:闲聊走风格锚点,攻略走场景锚点。"""
    if task_type == "攻略":
        scene = load_scene_anchors()
        guide = [a for a in scene if a.domain == "攻略"]
        if guide:
            ranked = sorted(guide, key=lambda a: a.id)
            return ranked[: min(k, len(ranked))]
    return retrieve(query, task_type, k)


def render(anchors: Iterable[Anchor]) -> str:
    """渲染为注入 Judge 的文本块。"""
    items = list(anchors)
    if not items:
        return "(本次无锚点注入)"
    lines = []
    for a in items:
        lines.append(f'- [{a.id}] "{a.text}"  (场景:{a.scene}; 来源:{a.source})')
    return "\n".join(lines)


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "他今天加班到很晚,看起来很累"
    tt = sys.argv[2] if len(sys.argv) > 2 else "闲聊"
    got = retrieve_for_case(q, tt, k=6)
    print(f"query={q!r} task={tt} -> {len(got)} 条")
    print(render(got))
