# coding: utf-8
"""L2 蒸馏画像缓存(故事层,不衰减)。

文档 §三:
- 每 10 轮从 L1 蒸馏一次,硬上限 500 token,超限强制压缩。
- 蒸馏方向:「我们的故事」,不是「用户是什么人」。
- L2 是身份层:永不衰减。
- L2 是**缓存不是账本** —— 删除它必须能从 L1 完整重建(容灾验收项)。

本模块只负责「缓存容器 + 重建入口 + token 预算」,真正的蒸馏(summarize via LLM)
由运行时注入 distill_fn(通常指向 prompts/distill_l2.md 调廉价模型)。
为可测试与容灾,内置一个**确定性规则回退**(无 LLM 也能从 L1 重建最简画像)。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from ..relation.affinity import LedgerEvent

# 与 memory_architecture §三 一致
MAX_TOKENS = 500
DISTILL_EVERY_N_TURNS = 10

_CACHE_SUBDIR = "l2_profiles"


@dataclass
class L2Profile:
    """单个用户的蒸馏画像缓存(缓存,非账本)。"""

    user_id: str
    data: dict = field(default_factory=dict)
    updated_utc: float = 0.0
    rebuilt_from_l1: bool = False   # 标记:本内容是重建产物(容灾可证)
    events_seen: int = 0            # 蒸馏时账本里有多少事件(供新鲜度判定)

    def to_markdown(self) -> str:
        """注入 prompt 用的文本(仅描述性,绝无指令性)。"""
        lines = []
        for k, v in self.data.items():
            if isinstance(v, str) and v:
                lines.append(f"- {k}: {v}")
        return "\n".join(lines) if lines else ""


def _cache_root() -> str:
    data = os.environ.get("KB_DATA_ROOT")
    root = data or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")
    base = os.path.join(root, "memory", _CACHE_SUBDIR)
    os.makedirs(base, exist_ok=True)
    return base


def _cache_path(user_id: str) -> str:
    return os.path.join(_cache_root(), f"{user_id}.json")


def save_l2(profile: L2Profile) -> None:
    payload = {
        "user_id": profile.user_id,
        "data": profile.data,
        "updated_utc": profile.updated_utc,
        "rebuilt_from_l1": profile.rebuilt_from_l1,
        "events_seen": profile.events_seen,
    }
    with open(_cache_path(profile.user_id), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_l2(user_id: str) -> Optional[L2Profile]:
    path = _cache_path(user_id)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    return L2Profile(
        user_id=payload.get("user_id", user_id),
        data=payload.get("data", {}),
        updated_utc=payload.get("updated_utc", 0.0),
        rebuilt_from_l1=payload.get("rebuilt_from_l1", False),
        events_seen=payload.get("events_seen", 0),
    )


def delete_l2(user_id: str) -> bool:
    """容灾验收：删掉 L2 缓存文件。返回是否确实存在并删除。"""
    path = _cache_path(user_id)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


# ---- 确定性规则回退(无 LLM 也能重建最简画像,兼可测试) ----

def _rule_fallback(events: list[LedgerEvent], user_id: str) -> dict:
    """从 L1 提炼一个确定性、低 token 的画像(不调 LLM)。

    仅供:无 LLM / 测试 / 容灾兜底。真实蒸馏应走 distill_fn。
    """
    d: dict = {}
    # 玩家身份偏好:显式记忆指令 + 身份类
    for ev in events:
        if ev.type == "显式记忆指令":
            d.setdefault("用户强调记住", []).append(ev.content)
        elif ev.type == "玩家身份与偏好":
            d.setdefault("玩家身份与偏好", []).append(ev.content)
    # 我们的故事:共同经历/承诺/情绪,取最近最多 3 条概括意图(整块,不碎)
    story = []
    for ev in reversed(events[-30:]):
        if ev.type in ("共同经历", "承诺与约定", "情绪显著时刻"):
            story.append(ev.content)
        if len(story) >= 3:
            break
    if story:
        d["我们的故事(最近)"] = " / ".join(reversed(story))
    return d


def _enforce_budget(d: dict, max_len_chars: int = MAX_TOKENS * 2) -> dict:
    """粗略把 data 压缩进 token 预算(中文约 1 token ≈ 0.6~1 字,这里给个字符上界)。"""
    out: dict = {}
    budget = max_len_chars
    for k, v in d.items():
        if isinstance(v, list):
            v = "、".join(str(x) for x in v[:3])
        text = f"{k}: {v}"
        if len(text) <= budget:
            out[k] = v
            budget -= len(text)
        else:
            out[k] = str(v)[: budget] if budget > 0 else ""
            break
    return out


def distill(events: list[LedgerEvent], user_id: str,
            distill_fn: Optional[Callable[[str], str]] = None) -> dict:
    """从 L1 蒸馏出 L2 画像数据。

    distill_fn: 可选,接收一个已注入『L1 事件摘要』的 prompt,返回画像文本/JSON。
        None 时用 _rule_fallback(确定性、可测试、零成本)。
    """
    if distill_fn is not None:
        # 真实链路:把 L1 汇总交给 LLM 蒸馏(LLM 实现跑在运行时)
        raw = distill_fn(_summarize_input(events, user_id))
        # 期待返回尽量结构化;此处宽松解析,失败回退规则版
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return _enforce_budget(parsed)
        except Exception:
            pass
        return _enforce_budget({"我们的故事": raw.strip()[:MAX_TOKENS * 3]})
    return _enforce_budget(_rule_fallback(events, user_id))


def _summarize_input(events: list[LedgerEvent], user_id: str) -> str:
    head = f"用户 {user_id} 的 L1 事件账本(共 {len(events)} 条,最近优先):\n"
    body = "\n".join(
        f"- [{ev.type}] {ev.content}"
        for ev in reversed(events[-200:])
    )
    return head + body


def rebuild_l2_from_ledger(user_id: str,
                           events: list[LedgerEvent],
                           distill_fn: Optional[Callable[[str], str]] = None) -> L2Profile:
    """容灾验收入口：从 L1 全量重建并写回 L2 缓存,标记 rebuilt_from_l1=True。"""
    data = distill(events, user_id, distill_fn=distill_fn)
    prof = L2Profile(
        user_id=user_id, data=data,
        updated_utc=datetime.now(timezone.utc).timestamp(),
        rebuilt_from_l1=True,
        events_seen=len(events),
    )
    save_l2(prof)
    return prof
