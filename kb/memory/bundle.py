# coding: utf-8
"""kb.memory.bundle —— 注入块打包（L3 命中 + L2 关系包 同出口）。

首府裁定（2026-10-06 · 注入路径）：
- L3 不进推理路径，进**注入路径**；走 `l2()` **同出口**。
- 「找用分离」：找（本地词法零 LLM）→ 用（注入文本给模型）。

打包纪律（与 L2 `to_markdown()` 同源）：
- **只出描述性文本**，结构化状态（degraded / status / extraction 数值）
  **绝不进注入文本** —— 防模型把「未蒸馏」「低唤起」当剧情演。
- 降级走**独立通道**（一句话确认），不与完整展开混排。

块构成（默认可组合）：
  [关系]  ← L2 画像（to_markdown）
  [记忆]  ← L3 场景命中条目（描述性）
  [察知]  ← 情绪通道命中（已知事实，非指令）★ 2026-10-07 立法
  [降级]  ← 不应期内一句话确认（不展开）

【察知】块纪律（首府 2026-10-07 · 情绪通道三刀）：
- 情绪命中注成**已知事实**，绝不注成指令。
  ✓ 「他这会儿情绪偏低落」 / ✗ 「你要安慰他」
- 同一句「怎么接是你的分寸」是芽衣与客服脚本的国境线。
- 数据层全留、注入层 top-N（默认3）：命中五条全塞 = 情绪清单轰炸。
- 措辞跟强度走（0.5 读起来就是 0.5），短语由 emotion.STRENGTH_PHRASES 单一事实源提供。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ── 块标题（注入文本的骨架标签；中性词，不带系统语气）──
HEAD_RELATION = "【关系】"
HEAD_MEMORY = "【记忆】"
HEAD_FADED = "【近忆】"
HEAD_SENSE = "【此刻的察知】"

# 情绪通道注入上限（刀二：数据层全留，表达层限流）
EMOTION_TOP_N = 3

# 注入文本硬上限（字符代理，与 L2 的 CHAR_BUDGET 同思路：宁严不松）
INJECT_CHAR_BUDGET = 600


@dataclass
class Hit:
    """一条 L3 命中：条目 + 命中媒介（显示用，不注入数值）。"""
    item: object
    medium: str = ""          # 命中媒介描述，如 "上位词『饮品』"
    faded: bool = False       # 不应期内 → 只一句话确认


@dataclass
class SenseHit:
    """一条情绪察知（非 L3 条目；来自 emotion 通道）。"""
    family: str
    strength: float
    phrase: str = ""          # 措辞（单一事实源 = emotion.STRENGTH_PHRASES）
    hit: str = ""             # 原始命中词（仅系统侧，不进文本）


@dataclass
class InjectBundle:
    """打包结果。text 是可注入文本；其余为**系统侧**状态（不进 text）。"""
    text: str = ""
    # ── 系统侧（绝不进注入文本）──
    l2_status: str = ""       # ok / memory_not_distilled / budget_truncated
    hits: list = field(default_factory=list)   # list[Hit]
    n_memory: int = 0
    n_sense: int = 0          # 情绪察知条数（系统侧）
    truncated: bool = False   # 打包时超预算被截

    def to_prompt(self) -> str:
        return self.text


def _clean(s: str) -> str:
    return (s or "").strip()


def _line(desc: str) -> str:
    return "- " + desc


def render_hits(hits: list, *, use_faded: bool = True) -> str:
    """L3 命中 → 描述性行。降级条目只出标签+短语，不展开内容。"""
    lines = []
    for h in hits:
        it = h.item
        if h.faded and use_faded:
            # 一句话确认：只取内容前段作指代，不叙述细节
            head = _clean(getattr(it, "content", ""))[:12]
            lines.append(_line(f"（嗯，你说过{head}那件事）"))
        else:
            c = _clean(getattr(it, "content", ""))
            if c:
                lines.append(_line(c))
    return "\n".join(lines)


def render_sense(senses: list) -> str:
    """情绪命中 → 察知行。**已知事实口吻，绝无指令**。

    措辞直接取 sense.phrase（由 emotion.STRENGTH_PHRASES 按强度给出，
    保证 0.5 读起来就是 0.5）。
    """
    lines = []
    for s in senses:
        p = _clean(getattr(s, "phrase", ""))
        if p:
            lines.append(_line(p))
    return "\n".join(lines)


def build_bundle(
    l2_profile=None,
    hits: Optional[list] = None,
    *,
    senses: Optional[list] = None,
    include_relation: bool = True,
    budget: int = INJECT_CHAR_BUDGET,
) -> InjectBundle:
    """打包注入块。

    l2_profile: L2Profile | None（其 to_markdown() 为关系块）
    hits: list[Hit]（L3 场景命中，按优先级排好）
    senses: list[SenseHit]（情绪通道命中，已 top-N 限流）
    """
    hits = hits or []
    senses = senses or []
    parts = []
    sys_status = ""

    # ── 关系块（L2）──
    if include_relation and l2_profile is not None:
        rel = l2_profile.to_markdown()
        if _clean(rel):
            parts.append(HEAD_RELATION + "\n" + rel)
        sys_status = getattr(l2_profile, "distill_status", "") or ""

    # ── 情绪察知块（优先于场景记忆：先接住人，再说旧事）──
    if senses:
        body = render_sense(senses)
        if body:
            parts.append(HEAD_SENSE + "\n" + body)

    # ── 记忆块（L3），拆降级/展开两段 ──
    full_hits = [h for h in hits if not h.faded]
    faded_hits = [h for h in hits if h.faded]
    n_mem = 0
    if full_hits:
        body = render_hits(full_hits)
        if body:
            parts.append(HEAD_MEMORY + "\n" + body)
            n_mem += len(full_hits)
    if faded_hits:
        body = render_hits(faded_hits, use_faded=True)
        if body:
            parts.append(HEAD_FADED + "\n" + body)

    text = "\n\n".join(parts)

    # ── 预算（保情绪+记忆块，截关系块）──
    truncated = False
    if len(text) > budget:
        sense_block = (HEAD_SENSE + "\n" + render_sense(senses)) if senses else ""
        mem_block = ""
        if full_hits:
            mem_block = HEAD_MEMORY + "\n" + render_hits(full_hits)
        faded_block = ""
        if faded_hits:
            faded_block = HEAD_FADED + "\n" + render_hits(faded_hits, use_faded=True)
        # 察知（此刻）优先于记忆（旧事）与关系（长背景）
        kept = [b for b in (sense_block, mem_block, faded_block) if _clean(b)]
        text = "\n\n".join(kept)
        truncated = True
        if len(text) > budget:
            text = text[:budget]

    return InjectBundle(
        text=text,
        l2_status=sys_status,
        hits=hits,
        n_memory=n_mem,
        n_sense=len(senses),
        truncated=truncated,
    )
