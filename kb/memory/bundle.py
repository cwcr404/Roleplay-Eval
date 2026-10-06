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
  [记忆]  ← L3 命中条目（描述性）
  [降级]  ← 不应期内一句话确认（不展开）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ── 块标题（注入文本的骨架标签；中性词，不带系统语气）──
HEAD_RELATION = "【关系】"
HEAD_MEMORY = "【记忆】"
HEAD_FADED = "【近忆】"

# 注入文本硬上限（字符代理，与 L2 的 CHAR_BUDGET 同思路：宁严不松）
INJECT_CHAR_BUDGET = 600


@dataclass
class Hit:
    """一条 L3 命中：条目 + 命中媒介（显示用，不注入数值）。"""
    item: object
    medium: str = ""          # 命中媒介描述，如 "上位词『饮品』"
    faded: bool = False       # 不应期内 → 只一句话确认


@dataclass
class InjectBundle:
    """打包结果。text 是可注入文本；其余为**系统侧**状态（不进 text）。"""
    text: str = ""
    # ── 系统侧（绝不进注入文本）──
    l2_status: str = ""       # ok / memory_not_distilled / budget_truncated
    hits: list = field(default_factory=list)   # list[Hit]
    n_memory: int = 0
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


def build_bundle(
    l2_profile=None,
    hits: Optional[list] = None,
    *,
    include_relation: bool = True,
    budget: int = INJECT_CHAR_BUDGET,
) -> InjectBundle:
    """打包注入块。

    l2_profile: L2Profile | None（其 to_markdown() 为关系块）
    hits: list[Hit]（L3 命中，按优先级排好）
    """
    hits = hits or []
    parts = []
    sys_status = ""

    # ── 关系块（L2）──
    if include_relation and l2_profile is not None:
        rel = l2_profile.to_markdown()
        if _clean(rel):
            parts.append(HEAD_RELATION + "\n" + rel)
        sys_status = getattr(l2_profile, "distill_status", "") or ""

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

    # ── 预算（保记忆块，截关系块）──
    truncated = False
    if len(text) > budget:
        mem_block = ""
        if full_hits:
            mem_block = HEAD_MEMORY + "\n" + render_hits(full_hits)
        faded_block = ""
        if faded_hits:
            faded_block = HEAD_FADED + "\n" + render_hits(faded_hits, use_faded=True)
        # 记忆块优先，关系块被截
        kept = [b for b in (mem_block, faded_block) if _clean(b)]
        text = "\n\n".join(kept)
        truncated = True
        if len(text) > budget:
            text = text[:budget]

    return InjectBundle(
        text=text,
        l2_status=sys_status,
        hits=hits,
        n_memory=n_mem,
        truncated=truncated,
    )
