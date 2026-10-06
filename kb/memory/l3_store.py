# coding: utf-8
"""kb.memory.l3_store —— L3 关系事件层（值得记一辈子的瞬间）。

首府裁定（2026-10-06 · L3 三问 + 施工规格）：
- 存的是【关系事件】不是状态、不是弧线：状态归温度层，弧线归 L2。
- 与温度层【平行、互不干涉】（老朋友定理）：入口吃情绪强度不吃好感度，
  记忆存留永不归温度管，affinity 层与 L3 只读不写。
- L3 进【注入路径】不进推理路径：找用分离，找=本地词法（倒排索引）+ 向量层后接，
  CPU 活零 LLM；LLM 只参与「用」不参与「找」。
- 书记员（打分/提名）是旁路进程，不在主生成路径上。

入场规则（纯代码闸门）：
- 三条件同时满足：隐式涌现 + 情绪强度绝对值 ≥6 + 与现有条目不重复
- 查重：场景词重叠 ≥2 或内容高相似 → 合并旧条（浮现延续、存储+1、提取回升）
- 每日限额：一天最多新增 1 条；被拦截提名 L1 记「待复核」，不丢
- 出局=暂离不是死亡：落选全留 L1，可重扫补录

衰减（首府立法 · 澄清2）：
- 指数衰减，半衰期 14 天；惰性计算（不存 extraction，存 last_evoked + storage）
  extraction = max(0, round(storage × 0.5^(距上次唤起天数 / 14)))
- 唤起：last_evoked=now，同时 storage+1（封顶 10）
- 沉睡（extraction ≤2）：标灰可见，主动进口跳过，被动应答不受限
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from .l3_vocab import expand_scene_words, is_valid_scene_key

# ── 常量 ────────────────────────────────────────────────────
HALF_LIFE_DAYS: float = 14.0          # 半衰期，与主动进口配额同律
STORAGE_CAP: float = 10.0             # 存储强度封顶
ENTRY_MIN_INTENSITY: int = 6          # 入场情绪强度门槛（绝对值）
DAILY_NEW_CAP: int = 1                # 每日新增上限
DUP_SCENE_OVERLAP: int = 2            # 查重：场景词重叠数
DORMANT_THRESHOLD: int = 2            # 沉睡：extraction ≤ 此值
ACTIVE_ENTRY_MIN_EXTRACTION: int = 6  # 主动进口候选：extraction ≥ 此值
_CONTENT_SIM_THRESHOLD: float = 0.8   # 内容高相似判据（字符级 Jaccard）

# 日界时区（首府立法 2026-10-06 · ③）：内部时间戳一律 UTC 绝对时间，
# 所有【日界计算】（date 字段、每日限额的“一天”、按天衰减）按 Asia/Shanghai。
# 理由：账本记的是他与芽衣的关系事件，用户说“今天”就是 GMT+8 的今天；
# UTC 切日会把深夜对话（高发时段）记错天。
DAY_TZ_OFFSET_HOURS: float = 8.0


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _local_date(ts: Optional[float] = None) -> str:
    """把 UTC 时间戳换算为 Asia/Shanghai 的 YYYY-MM-DD（日界计算唯一出口）。"""
    ts = ts if ts is not None else _now_ts()
    shifted = datetime.fromtimestamp(ts, timezone.utc) + timedelta(
        hours=DAY_TZ_OFFSET_HOURS
    )
    return shifted.strftime("%Y-%m-%d")


def _days_between(ts_a: float, ts_b: float) -> float:
    return abs(ts_a - ts_b) / 86400.0


def _data_dir() -> str:
    base = os.environ.get("KB_DATA_ROOT") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data",
    )
    d = os.path.join(base, "memory")
    os.makedirs(d, exist_ok=True)
    return d


# ── 数据结构 ────────────────────────────────────────────────
@dataclass
class L3Item:
    """一条关系事件记忆。

    字段与裁定逐条对应：日期 / 内容 / 双维标签 / 存储强度 / 提取强度
    （惰性：存 last_evoked）/ 场景词 / 浮现次数 / 提名理由。
    """
    id: str
    date: str                              # YYYY-MM-DD
    content: str                           # 一句话具体画面（写画面不写评价）
    tags: str                              # 关系维×情绪维
    storage: float                         # 0-10，只涨不跌
    scene_words: list[str] = field(default_factory=list)   # 2-5 个
    reason: str = ""                       # 提名理由
    evoked_count: int = 0                  # 浮现次数
    last_evoked: float = field(default_factory=_now_ts)
    created_ts: float = field(default_factory=_now_ts)
    status: str = "active"                 # active / pending_review(被限额拦截)

    # ── 惰性提取强度 ──
    def extraction_at(self, now: Optional[float] = None) -> int:
        now = now if now is not None else _now_ts()
        days = _days_between(now, self.last_evoked)
        return max(0, round(self.storage * (0.5 ** (days / HALF_LIFE_DAYS))))

    def is_dormant(self, now: Optional[float] = None) -> bool:
        return self.extraction_at(now) <= DORMANT_THRESHOLD

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "L3Item":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


# ── 内容相似（字符级 Jaccard，零 LLM）────────────────────────
def _content_similarity(a: str, b: str) -> float:
    sa = set(re.sub(r"\s+", "", a))
    sb = set(re.sub(r"\s+", "", b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# ── 入场闸门 ────────────────────────────────────────────────
@dataclass
class AdmitVerdict:
    admitted: bool
    action: str          # "new" / "merge" / "reject"
    reason: str
    target_id: str = ""  # merge 时命中的旧条 id


def check_entry(
    nominee: dict,
    existing: list[L3Item],
    today_new_count: int,
    *,
    implicit: bool = True,
) -> AdmitVerdict:
    """入场三条件 + 查重 + 每日限额。纯代码闸门，零 LLM。

    nominee 形如：
      {"date","content","tags","storage","scene_words","reason","intensity"}
    """
    intensity = abs(float(nominee.get("intensity", nominee.get("storage", 0))))

    # 条件一：隐式涌现（显式信息归 L2，L3 不抢）
    if not implicit:
        return AdmitVerdict(False, "reject", "显式陈述——归 L2，L3 不抢")

    # 条件二：情绪强度绝对值 ≥6
    if intensity < ENTRY_MIN_INTENSITY:
        return AdmitVerdict(
            False, "reject",
            f"情绪强度 {intensity:.0f} < {ENTRY_MIN_INTENSITY}，不入场",
        )

    scene = list(nominee.get("scene_words", []))
    content = nominee.get("content", "")

    # 条件三：与现有条目不重复 → 查重
    for it in existing:
        if it.status != "active":
            continue
        overlap = len(set(scene) & set(it.scene_words))
        sim = _content_similarity(content, it.content)
        if overlap >= DUP_SCENE_OVERLAP or sim >= _CONTENT_SIM_THRESHOLD:
            return AdmitVerdict(
                True, "merge",
                f"与 {it.id} 重复（场景词重叠 {overlap} / 相似度 {sim:.2f}）"
                f"→ 合并：浮现延续、存储+1、提取回升",
                target_id=it.id,
            )

    # 每日限额
    if today_new_count >= DAILY_NEW_CAP:
        return AdmitVerdict(
            False, "reject",
            f"当日新增已达上限 {DAILY_NEW_CAP}，转 L1 记『待复核』（不丢）",
        )

    return AdmitVerdict(True, "new", "三条件满足，准予新建")


def merge_into(item: L3Item, nominee: dict, now: Optional[float] = None) -> None:
    """合并：旧条浮现延续、存储+1（封顶）、提取回升。"""
    now = now if now is not None else _now_ts()
    item.storage = min(STORAGE_CAP, item.storage + 1.0)
    item.evoked_count += 1
    item.last_evoked = now
    # 场景词并集（去宽泛词）
    merged = list(item.scene_words)
    for w in nominee.get("scene_words", []):
        if w not in merged and is_valid_scene_key(w):
            merged.append(w)
    item.scene_words = merged[:8]
    if not item.reason and nominee.get("reason"):
        item.reason = nominee["reason"]


# ── 倒排索引（场景词 → 条目 id）──────────────────────────────
class SceneIndex:
    """场景词倒排索引。纯本地词法，CPU 活零 LLM。"""

    def __init__(self) -> None:
        self._postings: dict[str, set[str]] = {}

    def add(self, item: L3Item) -> None:
        for w in item.scene_words:
            self._postings.setdefault(w, set()).add(item.id)

    def remove(self, item: L3Item) -> None:
        for w in item.scene_words:
            s = self._postings.get(w)
            if s:
                s.discard(item.id)
                if not s:
                    self._postings.pop(w, None)

    def query(self, words: list[str]) -> dict[str, int]:
        """返回 {item_id: 命中词数}，按命中数降序。"""
        hits: dict[str, int] = {}
        for w in words:
            for iid in self._postings.get(w, ()):
                hits[iid] = hits.get(iid, 0) + 1
        return dict(sorted(hits.items(), key=lambda kv: -kv[1]))

    def __len__(self) -> int:
        return len(self._postings)


# ── 检索侧本地分解器（与写入侧共用词表 —— 首府关键约束）──────
def decompose_query(text: str) -> list[str]:
    """把一条用户消息本地分解为场景词（三层扩展）。

    零 LLM、毫秒级、每轮必跑（无论情绪强度）。
    与写入侧共用 l3_vocab 的 expand_scene_words，保证写读对称。
    """
    from .l3_vocab import HYPERNYM_MAP, SITUATION_MAP

    found: list[str] = []
    for key in HYPERNYM_MAP:
        if key in text:
            found.append(key)
    for key in SITUATION_MAP:
        if key in text:
            found.append(key)
    return expand_scene_words(found)


# ── L3 存储 ────────────────────────────────────────────────
L3_FILENAME = "l3_items.jsonl"


class L3Store:
    """某用户的 L3 关系事件存储。线程安全。"""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self._items: list[L3Item] = []
        self._index = SceneIndex()
        self._lock = threading.Lock()
        self._path = os.path.join(_data_dir(), f"l3_{user_id}.jsonl")
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        with open(self._path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    it = L3Item.from_dict(json.loads(line))
                except Exception:
                    continue
                self._items.append(it)
                if it.status == "active":
                    self._index.add(it)

    def _persist_append(self, item: L3Item) -> None:
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")

    # ── 写路径 ──
    def nominate(self, nominee: dict, *, implicit: bool = True,
                 now: Optional[float] = None) -> AdmitVerdict:
        """提交一条提名：过入场闸门 → 新建/合并/拒绝。"""
        now = now if now is not None else _now_ts()
        with self._lock:
            today = _local_date(now)
            today_new = sum(
                1 for it in self._items
                if it.status == "active" and it.date == today
            )
            verdict = check_entry(nominee, self._items, today_new, implicit=implicit)

            if verdict.action == "merge":
                for it in self._items:
                    if it.id == verdict.target_id:
                        merge_into(it, nominee, now)
                        break
            elif verdict.action == "new":
                # 存储强度初值 = 当时情绪强度（裁定：存储强度写入时情绪强度作初值）
                init_storage = nominee.get("storage")
                if init_storage in (None, 0, 0.0):
                    init_storage = nominee.get("intensity", 0)
                item = L3Item(
                    id=f"L3-{self.user_id}-{len(self._items) + 1:03d}",
                    date=nominee.get("date", _local_date(now)),
                    content=nominee.get("content", ""),
                    tags=nominee.get("tags", ""),
                    storage=min(STORAGE_CAP, float(init_storage or 0)),
                    scene_words=list(nominee.get("scene_words", [])),
                    reason=nominee.get("reason", ""),
                    last_evoked=now,
                    created_ts=now,
                    status="active",
                )
                self._items.append(item)
                self._index.add(item)
                self._persist_append(item)
            else:
                # 被拒 → 记「待复核」，不丢
                _pend_storage = nominee.get("storage")
                if _pend_storage in (None, 0, 0.0):
                    _pend_storage = nominee.get("intensity", 0)
                flag = L3Item(
                    id=f"L3-{self.user_id}-pend-{len(self._items) + 1:03d}",
                    date=_local_date(now),
                    content=nominee.get("content", ""),
                    tags=nominee.get("tags", ""),
                    storage=min(STORAGE_CAP, float(_pend_storage or 0)),
                    scene_words=list(nominee.get("scene_words", [])),
                    reason=f"[待复核] {verdict.reason}",
                    status="pending_review",
                )
                self._items.append(flag)
                self._persist_append(flag)
            return verdict

    def evoke(self, item_id: str, now: Optional[float] = None) -> Optional[L3Item]:
        """唤起：last_evoked=now，storage+1（封顶），浮现次数+1。"""
        for it in self._items:
            if it.id == item_id:
                merge_into(it, {"scene_words": []}, now)
                return it
        return None

    # ── 读路径 ──
    def retrieve(self, query: str, *, k: int = 5,
                 now: Optional[float] = None) -> list[tuple[L3Item, int]]:
        """检索：本地分解 → 倒排命中 → 按(命中数, 提取强度)排序。"""
        words = decompose_query(query)
        hits = self._index.query(words)
        by_id = {it.id: it for it in self._items}
        out: list[tuple[L3Item, int]] = []
        for iid, cnt in hits.items():
            it = by_id.get(iid)
            if it and it.status == "active":
                out.append((it, cnt))
        out.sort(key=lambda p: (-p[1], -p[0].extraction_at(now)))
        return out[:k]

    def active_items(self) -> list[L3Item]:
        return [it for it in self._items if it.status == "active"]

    def sleeping_items(self, now: Optional[float] = None) -> list[L3Item]:
        return [it for it in self.active_items() if it.is_dormant(now)]

    # ── 主动进口候选（沉睡跳过；extraction ≥6 取最高）──
    def active_entry_candidate(self, now: Optional[float] = None) -> Optional[L3Item]:
        cands = [
            it for it in self.active_items()
            if not it.is_dormant(now)
            and it.extraction_at(now) >= ACTIVE_ENTRY_MIN_EXTRACTION
        ]
        if not cands:
            return None
        return max(cands, key=lambda it: it.extraction_at(now))

    def index_size(self) -> int:
        return len(self._index)
