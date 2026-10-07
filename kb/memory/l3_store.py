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
from .roles import tag_characters

# ── 常量 ────────────────────────────────────────────────────
HALF_LIFE_DAYS: float = 14.0          # 半衰期，与主动进口配额同律
STORAGE_CAP: float = 10.0             # 存储强度封顶
ENTRY_MIN_INTENSITY: int = 6          # 入场情绪强度门槛（绝对值）
DAILY_NEW_CAP: int = 1                # 每日新增上限
DUP_SCENE_OVERLAP: int = 2            # 查重：场景词重叠数
DORMANT_THRESHOLD: int = 2            # 沉睡：extraction ≤ 此值
ACTIVE_ENTRY_MIN_EXTRACTION: int = 6  # 主动进口候选：extraction ≥ 此值
_CONTENT_SIM_THRESHOLD: float = 0.8   # 内容高相似判据（字符级 Jaccard）

# ── 出口规则（首府裁定 2026-10-06 · 出口规则）──────────────
# 记忆应答免费（retrieve 不限流）；主动进口（芽衣主动提）才限流。
QUOTA_WINDOW_DAYS: float = 14.0       # 主动进口配额窗口：14 天最多 1 次
QUOTA_MONTH_WINDOW_DAYS: float = 30.0 # 月窗口
QUOTA_MAX_PER_MONTH: int = 2          # 每 30 天最多 2 次
FIRST_POP_MAX_EVOKED: int = 1         # 浮现次数 > 此值 → 关主动进口（提过就不再主动提）
REFRACTORY_HOURS: float = 72.0        # 不应期（跨会话、条目级）：任何浮现后 72h 内再触碰→降级

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
    # 【仅主动进口专用】最后一次【主动进口】的时间戳，0=从未。
    # 注意：当前为缓存字段（甲方案）；终态由 L1 active_entry 事件派生，
    # 那时本字段降级为缓存（先查缓存，缺失/可疑时回查 L1 重建）。
    # 不要把它当唯一真相源 —— 被动应答【不】更新此字段。
    last_active_entry: float = 0.0
    created_ts: float = field(default_factory=_now_ts)
    status: str = "active"                 # active / pending_review(被限额拦截)
    # ── 角色标（首府 2026-10-07 工单 P0-3）──
    # 入账时命中角色词典即打标（规范名列表，如 ["琪亚娜", "芽衣"]）。
    # 旧条目无此字段 → 默认空列表（向后兼容）；回填由工具批次做。
    # 零命中 = 空列表（不猜角色，没提不亮）。
    characters: list[str] = field(default_factory=list)

    # ── 不应期（条目级、跟会话，72h）──
    def in_refractory(self, now: Optional[float] = None) -> bool:
        """任何浮现（主+被动）后 72h 内 → True（再触碰降级为一句话确认）。

        注意：未浮现过的条目（evoked_count==0）不算在不应期内 ——
        新建条目 last_evoked=创建时刻，但它还未「浮现」过，应可被主动进口。
        """
        if self.evoked_count <= 0:
            return False
        now = now if now is not None else _now_ts()
        return (now - self.last_evoked) < REFRACTORY_HOURS * 3600.0

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
        for w in self._scene_keys_of(item):
            self._postings.setdefault(w, set()).add(item.id)

    @staticmethod
    def _scene_keys_of(item: L3Item) -> list[str]:
        """条目的全部索引键 = scene_words + 角色标。

        角色名进主索引（首府 P0-3 裁定：专有名词本就是倒排索引合法居民），
        否则「琪亚娜」永远勾不出那条记忆。读时合并（不写回 scene_words，
        保持 L3Item.scene_words 语义纯净）。
        """
        keys = list(item.scene_words)
        for c in getattr(item, "characters", ()) or ():
            if c not in keys:
                keys.append(c)
        return keys

    def remove(self, item: L3Item) -> None:
        for w in self._scene_keys_of(item):
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
def _is_cjk(ch: str) -> bool:
    """是否汉字（含扩展区）。非汉字（标点/空格/英文/数字）视为天然边界。"""
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF
            or 0xF900 <= o <= 0xFAFF or 0x20000 <= o <= 0x2FA1F)


# 高危单字（需过误报黑名单）：单字且易嵌入无关词造成子串误判。
STRICT_SINGLE_KEYS: frozenset[str] = frozenset({"面", "水"})

# 高危单字的误报搭配（黑名单）—— 只有命中这些才丢弃。
# 理由：白名单（列举“吃面/煮面”）穷举不完，黑名单（“外面/见面”）才是
# 有限集且即错集。邻字判定的方向反了：不问“是否像真词”，只问“是否已知误报”。
# 依据：首府 2026-10-07 坑1 裁定（不用词表，先堵最脏的一个）。
FALSE_POSITIVE_PAIRS: dict[str, frozenset[str]] = {
    "面": frozenset({
        "外", "里", "见", "方", "上", "下", "前", "后", "片", "局",
        "场", "地", "表", "画", "会", "书", "页", "市", "门", "世",
        "着", "一", "全", "多", "正", "反", "侧", "剖", "截", "断",
    }),
    "水": frozenset({
        "果", "平", "香", "药", "墨", "逆", "洪", "雨", "汗", "泪",
        "口", "泉", "源", "流", "蓄", "废", "污", "防", "抽", "脱",
    }),
}


def _boundary_ok(text: str, key: str, pos: int) -> bool:
    """词边界判定（首府 2026-10-07 坑1 补丁）。

    规则分两档：
    - **高危单字**（STRICT_SINGLE_KEYS）：“面/水”，只在**命中误报黑名单**时丢弃：
      「外面/见面/方面」的「面」丢；「吃面/煮了面/面条」的「面」留。
      方向是黑名单而非白名单：误报集有限且已知，白名单穷举不完。
    - **其余 key**（含“雨/雪/晴”及全部多字词）：允许嵌入
      （“下雨”“晴天”“大晴天”都是真命中）。
    只修匹配边界，不碰词表内容；误报黑名单可随实测滚动扩充。
    """
    if key not in STRICT_SINGLE_KEYS:
        return True
    bad = FALSE_POSITIVE_PAIRS.get(key, frozenset())
    end = pos + len(key)
    prev = text[pos - 1] if pos > 0 else ""
    nxt = text[end] if end < len(text) else ""
    if prev in bad or nxt in bad:
        return False
    return True


def decompose_query(text: str) -> list[str]:
    """把一条用户消息本地分解为场景词（三层扩展 + 角色名）。

    零 LLM、毫秒级、每轮必跑（无论情绪强度）。
    与写入侧共用 l3_vocab 的 expand_scene_words，保证写读对称。
    词边界：所有 key 按整词匹配（防子串误判，见 _boundary_ok）。

    角色名（首府 2026-10-07 P0-3 裁定）：
      角色名是**专有名词**，本就该是倒排索引的合法居民 → 直接进场景词集合，
      不学情绪词走独立通道。角色名由 roles.decompose_roles 本地识别（零 LLM）。
      **不猜**：没提角色就不加任何角色词。
    """
    from .l3_vocab import HYPERNYM_MAP, SITUATION_MAP

    found: list[str] = []

    def _scan(keys) -> None:
        for key in keys:
            if not key:
                continue
            # 多字 key 优先（长词优先，避免短词吃掉长词边界）
            start = 0
            hit = False
            while True:
                i = text.find(key, start)
                if i < 0:
                    break
                if _boundary_ok(text, key, i):
                    hit = True
                    break
                start = i + 1
            if hit:
                found.append(key)

    ordered = sorted(HYPERNYM_MAP.keys(), key=len, reverse=True)
    _scan(ordered)
    _scan(sorted(SITUATION_MAP.keys(), key=len, reverse=True))
    # 角色名进主索引（专有名词，非独立通道）；零命中即不加（不猜）
    from .roles import decompose_roles
    found.extend(decompose_roles(text))
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
                    # 角色标：入账即打（零 LLM，命中词典才打；不猜）
                    characters=nominee.get("characters")
                    or tag_characters(nominee.get("content", "")),
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
                    characters=nominee.get("characters")
                    or tag_characters(nominee.get("content", "")),
                )
                self._items.append(flag)
                self._persist_append(flag)
            return verdict

    def evoke(self, item_id: str, now: Optional[float] = None, *,
              active: bool = False) -> Optional[L3Item]:
        """唤起：last_evoked=now，storage+1（封顶），浮现次数+1。

        active=True 时同时更新 last_active_entry（仅主动进口）。
        被动应答（用户问到）active=False，不耗主动配额。
        """
        now = now if now is not None else _now_ts()
        for it in self._items:
            if it.id == item_id:
                merge_into(it, {"scene_words": []}, now)
                if active:
                    it.last_active_entry = now
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

    def retrieve_by_role(self, role: str, *, k: int = 5,
                         now: Optional[float] = None
                         ) -> list[tuple[L3Item, int]]:
        """按角色标检索（证据路专用）：返回带有该角色标的 active 条目。

        首府 P0-3 裁定：
        - 氛围路 = 命中角色词典即可亮（不依赖库，见 roles.decompose_roles）
        - 证据路 = 该角色的**库内真条目**进注入区（此函数）
        两路解耦：库里无该角色条目 → 返回空 → **不造记忆**（不猜）。
        """
        out: list[tuple[L3Item, int]] = []
        for it in self._items:
            if it.status != "active":
                continue
            if role in (getattr(it, "characters", ()) or ()):
                out.append((it, 1))
        out.sort(key=lambda p: -p[0].extraction_at(now))
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


# ── 出口规则：主动进口配额 / 不应期 / 浮现关主动 ─────────────
@dataclass
class EntryQuota:
    """主动进口配额状态（跟踪主动浮现的时间戳）。

    裁定（出口规则）：
    - 记忆应答免费（retrieve 不限流）；
    - 主动进口配额 14 天 1 次 / 月 2 次；
    - 不应期：刚浮现的不展开第二次；
    - 浮现次数 >1 → 关主动进口（提过就不再主动提）。

    持久化：每条 L3 条目的 last_evoked 已是“最近一次浮现”的记录，
    本类不另存状态，全部从条目字段推导（“状态=查询派生”原则）。
    """

    @staticmethod
    def active_entry_allowed(
        store: "L3Store",
        now: Optional[float] = None,
        *,
        quota_window_days: float = QUOTA_WINDOW_DAYS,
        month_window_days: float = QUOTA_MONTH_WINDOW_DAYS,
        max_per_month: int = QUOTA_MAX_PER_MONTH,
    ) -> tuple[bool, str]:
        """总闸门：当前时刻是否允许一次主动进口。返回 (允许, 理由)。

        只统计【主动进口】（last_active_entry>0），被动应答不耗配额。
        """
        now = now if now is not None else _now_ts()
        recent_14 = [
            it for it in store.active_items()
            if it.last_active_entry > 0
            and _days_between(now, it.last_active_entry) < quota_window_days
        ]
        if recent_14:
            return False, (
                f"配额窗口内（{quota_window_days:.0f}天）已主动进口过"
                f"（{recent_14[0].id}）"
            )
        recent_30 = [
            it for it in store.active_items()
            if it.last_active_entry > 0
            and _days_between(now, it.last_active_entry) < month_window_days
        ]
        if len(recent_30) >= max_per_month:
            return False, (
                f"月窗口（{month_window_days:.0f}天）内已主动进口 {len(recent_30)} 次"
                f"（上限 {max_per_month}）"
            )
        return True, "配额允许"

    @staticmethod
    def item_eligible(item: "L3Item", now: Optional[float] = None,
                      *, max_evoked: int = FIRST_POP_MAX_EVOKED) -> bool:
        """单条闸门：浮现次数 >1 → 关主动进口；沉睡跳过；不应期内跳过。"""
        now = now if now is not None else _now_ts()
        return (
            item.evoked_count <= max_evoked
            and not item.is_dormant(now)
            and not item.in_refractory(now)
        )

    @staticmethod
    def render_faded(item: "L3Item") -> str:
        """不应期降级：不展开，只一句确认（不耗配额，无信息增量）。"""
        head = (item.content or "")[:8]
        return "（嗯，你说过" + head + "那件事）"

    @classmethod
    def pick_active_entry(
        cls,
        store: "L3Store",
        now: Optional[float] = None,
    ) -> tuple[Optional["L3Item"], str]:
        """选一条可主动浮现的条目：总闸门 THEN 单条闸门 THEN 取 extraction 最高。

        返回 (条目 or None, 理由)。
        """
        now = now if now is not None else _now_ts()
        ok, why = cls.active_entry_allowed(store, now)
        if not ok:
            return None, why
        cands = [
            it for it in store.active_items()
            if it.extraction_at(now) >= ACTIVE_ENTRY_MIN_EXTRACTION
            and cls.item_eligible(it, now)
        ]
        if not cands:
            return None, "无合格候选项（extraction≥6 且 浮现≤1 且 未沉睡且过不应期）"
        return max(cands, key=lambda it: it.extraction_at(now)), "配额允许"
