"""知识库 → Agent2 上文的事实注入(方案 A)。

v0.1 里 case 的 framework 仍是『指挥棒』(评测可控性优先)——它决定芽衣
这次要答的攻略方向;KB 检索器则作为 Agent2 手边的『事实保险』,在 framework
没覆盖到的点(克制关系、武器归属、圣痕机制等)提供真实、可复现的知识条目,
防止芽衣照 framework 改写时瞎编。

注入原则:
- V1(攻略):guide case 时,用用户问题检索真实攻略条目,塞进【知识库事实】区。
- V2(背景):仅在用户话题命中某段背景时,整块带出该叙事模块(不超过上限),
  让 Agent2 在聊到相关人事时『记得』对应过往,但内容仍以角色卡人格常量为主。

所有检索都走 BaseRetriever 接口——将来换向量实现,这里零改动。
"""
from __future__ import annotations

from typing import Optional

MAX_GUIDE_BLOCKS = 6   # 单次最多注入的攻略事实条目
MAX_LORE_BLOCKS = 2    # 单次最多注入的背景模块(V2 整块语义,宁可少而整)

# V2 背景按 case 分类的注入策略:
#   chat_relationship → 强相关(聊到琪亚娜/布洛妮娅等 → 带记忆整块)
#   chat_emotional    → 轻注入(最多 1 块,避免反客为主)
#   guide/v1 意图      → 背景不主动注入(攻略为主,防喧宾夺主)
#   adversarial_ooc   → 禁止注入(对抗要测的是纯人格防线的稳定性,
#                       喂背景模块可能反而给『怀念律者力量』一类陷阱递话,
#                       故只靠角色卡人格常量作答——这正是 OOC 防线的意义)
_LORE_BY_CATEGORY = {
    "chat_relationship": MAX_LORE_BLOCKS,
    "chat_emotional": 1,
}


def build_kb_context(case: dict, kb: dict, category_fallback: Optional[str] = None) -> str:
    """根据 case + 检索器,生成要注入 Agent2 上文的【知识库事实】文本。

    Args:
        case: 单条评测用例。
        kb: {"guide": GuideRetriever, "lore": LoreRetriever},来自 build_kb()。
        category_fallback: 当 case 无 category 时的兜底(通常不用)。

    Returns:
        一块 Markdown 文本(可能为空串);调用方 concat 到用户输入之后。
    """
    user = (case.get("user") or "").strip()
    if not user:
        return ""
    category = case.get("category") or category_fallback or "chat"

    parts: list[str] = []

    # 1) V1 攻略事实:仅 guide 意图下注入
    if category == "guide":
        guide = kb["guide"]
        try:
            hits = guide.query(user, top_k=MAX_GUIDE_BLOCKS)
        except Exception:
            hits = []
        if hits:
            blocks = [h.content.strip() for h in hits]
            parts.append("【知识库·游戏攻略事实】(仅作改写参考,不是唯一来源;与用户实际关卡冲突时以用户为准):\n" +
                         "\n---\n".join(blocks))

    # 2) V2 背景记忆:仅对会聊到人物羁绊的 case 分类注入,按整块带给
    lore_limit = _LORE_BY_CATEGORY.get(category, 0)
    if lore_limit > 0:
        lore = kb["lore"]
        try:
            lhits = lore.query(user, top_k=lore_limit)
        except Exception:
            lhits = []
        if lhits:
            lore_blocks = [f"〔{h.key}〕\n{h.content.strip()}" for h in lhits[:lore_limit]]
            parts.append("【知识库·角色背景记忆】(你确实记得的相关过往,自然引用即可,别生硬背诵):\n" +
                         "\n\n".join(lore_blocks))

    return "\n\n".join(parts)
