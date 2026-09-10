# coding: utf-8
"""L2 蒸馏画像缓存(故事层,不衰减)。

文档 §三:
- 每 10 轮从 L1 蒸馏一次,硬上限 500 字符,超限强制压缩。
- 蒸馏方向:「我们的故事」,不是「用户是什么人」。
- L2 是身份层:永不衰减。
- L2 是**缓存不是账本** —— 删除它必须能从 L1 完整重建(容灾验收项)。

本模块只负责「缓存容器 + 重建入口 + 字符预算」,真正的蒸馏(summarize via LLM)
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

# 与 memory_architecture §三 一致(规格单位:**字符**,钉死于 2026-09-10 清偿)
MAX_TOKENS = 500  # 历史符号名(遗留:名字里的 TOKEN 是清偿前的口径)。
#   清偿后的唯一权威口径:规格数字 500 的单位就是**字符**,enforce 执行同一单位、
#   同一数字。符号名不再改名(改名会动到 import 面),但**语义以『字符』为准**。
DISTILL_EVERY_N_TURNS = 10

# ---- 计量单位钉死(2026-09-08 立 · 2026-09-10 清偿后口径收紧) ----
# 权威口径(仅此一条):L2 预算规格以**字符**表述,enforce 侧执行同一单位、同一数字。
#   规格数字恒为唯一权威,不存在第二套换算标准,也不存在『token 规格 + 字符代理』的
#   旧口径。选字符的理由:确定性截断不依赖 tokenizer,单进程零依赖即可复现。
# 安全余量(不是另一套标准,是副证):对任一真实 CJK tokenizer,1 字符至多耗 1 token
#   (CJK≈0.6~0.7, ASCII≪1),故 500 字符 ⇒ 必然 ≤500 token —— 宁严不松。
#   代价:比 DeepSeek 实测容量(≈740+ 中文字)留了保守余量;若嫌挤,后续可换 tokenizer
#   真数把容量放回 —— 届时只改 CHAR_BUDGET 与一段文档,不动规格数字。
#   注:『1 字符≤1 token』是选字符单位的**依据**,不是把字符当代理去换算 token 的开端;
#   不要再写成『规格以 token 表述』(那是清偿前旧状态,已废)。
# 派生量:故事层主诉与【他是谁】副产的预算分工(500≈400/100)在 prompt 侧表达;
#   enforce 只对『总和 500 字符』做机械封顶,不替 prompt 管内部配比。

# L2 画像的蒸馏状态（引擎级日志用结构化标记；不进芽衣可见注入文本）：
#   ok                  = 真实 LLM 蒸馏成功（且未超预算截断）
#   memory_not_distilled = LLM 调用失败/异常/空输出 → 规则回退（L1 镜像），未真实蒸馏
#   budget_truncated     = LLM 蒸馏成功但初稿超预算 → 定向压缩/句边界截断后落盘
# 注：这两个降级标记是系统看的结构化状态，禁止泄漏进 _memory_context 注入区。
STATUS_OK = "ok"
STATUS_NOT_DISTILLED = "memory_not_distilled"
STATUS_BUDGET_TRUNCATED = "budget_truncated"

# 输出两段分隔行(distill_l2.md 与其对齐)
WHO_SPLIT = "---WHO---"
# enforce 用字符上界(=规格单位本身,max_len_chars):见文件头『计量单位钉死』——
# 规格单位就是字符,CHAR_BUDGET = MAX_TOKENS 与规格数字同单位、同数字。
# (原 MAX_TOKENS*2 把 1 字算成 0.5 token,是单位混用的隐患,已废。)
CHAR_BUDGET = MAX_TOKENS


_CACHE_SUBDIR = "l2_profiles"


@dataclass
class L2Profile:
    """单个用户的蒸馏画像缓存(缓存,非账本)。"""

    user_id: str
    data: dict = field(default_factory=dict)
    updated_utc: float = 0.0
    rebuilt_from_l1: bool = False   # 标记:本内容是重建产物(容灾可证)
    events_seen: int = 0            # 本缓存内容覆盖到账本里几条(注入层/规则重建用它算新鲜)
    distilled_events_seen: int = 0  # 引擎真实 LLM 蒸馏吃到的账本条数(蒸馏游标;规则重建不写它)
    distill_status: str = STATUS_OK  # ok / memory_not_distilled / budget_truncated(系统看,不进注入)
    budget_truncated: bool = False   # 兼容别名:本内容曾被句边界截断(历史/外部读数用)

    def to_markdown(self) -> str:
        """注入 prompt 用的文本(仅描述性,绝无指令性)。

        只输出可注入的描述性字串字段; distill_status / events_seen 等结构化
        状态完全不进这里 —— 防模型把「memory_not_distilled」当剧情素材演。
        """
        lines = []
        for k, v in self.data.items():
            if isinstance(v, str) and v:
                # 独立字段一律并排注入;「他是谁」也是纯描述性事实,可注入为参照注
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
        "distilled_events_seen": profile.distilled_events_seen,
        "distill_status": profile.distill_status,
    }
    with open(_cache_path(profile.user_id), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def save_distilled(user_id: str, data: dict, events_seen: int,
                   status: str = STATUS_OK,
                   rebuilt: bool = False) -> L2Profile:
    """引擎级专用落盘口：写一份真实蒸馏/降级标记过的 L2 画像缓存。

    data      : {我们的故事, 他是谁}（distill 输出切开后的两段）。
    events_seen: 本次真实蒸馏吃到的账本事件数 —— 同时写进蒸馏游标
                （distilled_events_seen）；规则重建不写它，故引擎能据此只吃新增。
    status    : ok / memory_not_distilled / budget_truncated(见模块常量)。
    rebuilt   : 本次产物是否重建(容灾)；真实蒸馏为 False。
    """
    prof = L2Profile(
        user_id=user_id,
        data=data,
        updated_utc=datetime.now(timezone.utc).timestamp(),
        rebuilt_from_l1=rebuilt,
        events_seen=events_seen,
        distilled_events_seen=events_seen,
        distill_status=status,
        budget_truncated=(status == STATUS_BUDGET_TRUNCATED),
    )
    save_l2(prof)
    return prof



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
        distilled_events_seen=payload.get("distilled_events_seen", 0),
        distill_status=payload.get("distill_status", STATUS_OK),
        budget_truncated=payload.get("budget_truncated", False),
    )


def delete_l2(user_id: str) -> bool:
    """容灾验收：删掉 L2 缓存文件。返回是否确实存在并删除。"""
    path = _cache_path(user_id)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def _split_who(text: str) -> dict:
    """把 distill_l2.md 规定的两段输出切开成 {我们的故事, 他是谁}。

    - story: 主诉叙事层（>=400 token 预算大头）。
    - who  : 【他是谁·参照注】独立字段（<=100 token）；无分隔行则 story 即全文。
    运行时不对『谁在哪个字段』做臆测推断——只按分隔行切；缺块就当空。
    """
    story, who = "", ""
    if WHO_SPLIT in text:
        head, _, tail = text.partition(WHO_SPLIT)
        story = head.strip()
        # tail 里去掉可能残留的【他是谁·参照注】标题行
        who = tail.strip()
        if who.startswith("【他是谁"):
            _, _, w2 = who.partition("】")
            who = w2.strip()
    else:
        story = text.strip()
    return {"我们的故事": story, "他是谁": who}


def truncate_to_budget(s: str, max_chars: int = CHAR_BUDGET) -> str:
    """句边界机械截断（最后兜底；用于压缩仍超预算时）。只在句号/换行处切，不劈词。"""
    s = s.strip()
    if len(s) <= max_chars:
        return s
    cut = s[:max_chars]
    # 回退到最近的句末标点，避免截断半句
    for sep in ("。", "！", "？", "\n", ".", "!"):
        i = cut.rfind(sep)
        if i > 0 and i < max_chars - 4:
            return cut[: i + 1]
    return cut


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


def _enforce_budget(d: dict, max_len_chars: int = CHAR_BUDGET) -> dict:
    """把 data 压缩进字符上界 CHAR_BUDGET(即 500 字符的绝对安全 token 代理)。
    逐键扣减;超预算键截到剩量,余额用完即停。
    """
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
        # 不在此处再设中间截断——_enforce_budget 是唯一封顶点(500 字符安全上界)。
        # (原 [:MAX_TOKENS * 3] 是残留的『×3』单位混用痕,已删;封顶口径恒为 CHAR_BUDGET。)
        return _enforce_budget({"我们的故事": raw.strip()})
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
        rebuilt_from_l1=True,
        events_seen=len(events),
        distill_status=STATUS_NOT_DISTILLED if distill_fn is None else STATUS_OK,
    )
    save_l2(prof)
    return prof
