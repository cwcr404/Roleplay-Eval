# coding: utf-8
"""kb.memory.emotion —— 情绪通道（**独立于场景词通道**）。

首府 2026-10-07 立法（三刀全过）：
 刀一：走**乙** —— 情绪通道独立。理由：`expand_scene_words` 是**双向管道**，
       情绪词一旦混入，场景词合法集就说不清来源；语义混仓是地基病，
       后续所有调参都成了错误地基上的精装修。故：场景词管「事」，情绪管「情」，
       读路径分两路，出口在 bundle 合流。
 刀二：数据层**多命中全留**（不取一，取一=替用户简化情绪）；
       注入层另设 **top-N=3** 限流（数据层不删、表达层限流，两码事不混）。
 刀三：弱情绪**进**，但「轻」必须落在**措辞**上 —— 0.5 的命中注出来就得读着像 0.5。
       零命中**写死合法**，双重否定**留白**给表达层。

零 LLM：全本地词法，毫秒级，每轮必跑。与场景词通道**零共享**（不 import
l3_vocab，也不被 l3_vocab import）。
"""
from __future__ import annotations

import re

# ── 情绪词表 v1（人工维护；与场景词表物理隔离）────────────────
# 族 → 口语变体。长词优先匹配（防「累」吃掉「好累」的边界）。
EMOTION_LEXICON: dict[str, tuple[str, ...]] = {
    "低落": ("提不起劲", "提不起精神", "没意思", "没劲", "没动力",
             "空落落", "心里空", "茫然", "迷茫", "低落", "郁闷", "憋屈",
             "丧气", "心灰", "糟糕", "糟", "丧", "emo"),
    "难受": ("不是滋味", "堵得慌", "绷不住", "撑不住", "熬不住", "挫败",
             "受挫", "失落", "扎心", "难受", "不舒服", "胸闷", "想哭",
             "心酸", "鼻酸", "痛"),
    "烦躁": ("受不了了", "受不了", "烦死", "气死", "烦躁", "恼火", "火大",
             "来气", "气人", "抓狂", "炸了", "崩溃", "烦"),
    "焦虑": ("喘不过气", "焦头烂额", "压力大", "赶不上", "来不及", "慌神",
             "焦虑", "心慌", "不安", "忐忑", "紧张", "慌"),
    "孤独": ("没人陪", "空荡荡", "被冷落", "一个人", "孤独", "孤单",
             "寂寞", "冷清", "没人"),
    "疲惫": ("精疲力尽", "扛不住", "顶不住", "没力气", "没电", "疲惫",
             "撑不住", "虚脱", "倦", "累了", "累"),
    "愉悦": ("太好了", "开心", "高兴", "愉快", "舒服", "满足", "幸福",
             "兴奋", "雀跃", "爽", "棒", "甜"),
    "平静": ("平静", "踏实", "安心", "放松", "惬意", "安稳", "还好"),
}

# ── 级联短语（口语主诉；显式映射到族，不做启发式猜测）───────
CASCADE_FAMILY: dict[str, str] = {
    "不太好": "低落", "不怎么好": "低落", "不太行": "低落",
    "不想说话": "低落", "不想动": "低落", "不想干": "低落",
    "没心情": "低落", "提不起劲": "低落", "高兴不起来": "低落",
    "笑不出来": "低落", "撑不下去": "疲惫", "熬不下去": "疲惫",
    "提不起精神": "疲惫",
}
CASCADE_PHRASES: tuple[str, ...] = tuple(
    sorted(CASCADE_FAMILY.keys(), key=len, reverse=True))

# ── 强度修饰词 ──────────────────────────────────────────────
INTENSITY_UP = ("很", "太", "超", "特别", "非常", "好", "真", "巨", "爆",
                "极", "最", "死了", "透了", "坏了")
INTENSITY_DOWN = ("有点", "稍微", "还算", "勉强", "一点点", "有些")

# ── 否定护栏 ───────────────────────────────────────────────
NEGATOR = ("不", "没", "别", "无")
# 否定 → 极性反转（「不开心」= 负面，不是正面）
NEG_POLARITY_FLIP: dict[str, str] = {"愉悦": "低落", "平静": "焦虑"}
# 双重否定（「不是不开心」）→ 留白，不乱猜
DOUBLE_NEG_MARKERS = ("不是不", "没有不", "不能不", "未必不")

# ── 强度分档 → 措辞（刀三：轻必须落在措辞上）────────────────
# 这是「察知」怎么写进注入文本的单一事实源。改措辞只改这里。
STRENGTH_PHRASES: tuple[tuple[float, str], ...] = (
    (1.5, "他这会儿情绪偏{fam}，看得出来压得挺沉"),
    (1.0, "他这会儿情绪偏{fam}"),
    (0.5, "他好像有点{fam}"),
    (0.0, "他隐约有点{fam}，不很明显"),
)


def _phrase_for(fam: str, strength: float) -> str:
    for lo, tpl in STRENGTH_PHRASES:
        if strength >= lo - 1e-9:
            return tpl.format(fam=fam)
    return STRENGTH_PHRASES[-1][1].format(fam=fam)


def _strength_of(text: str, idx: int, hit: str) -> float:
    """强度 0.5(弱) / 1.0(中) / 1.5(强)：只看词前紧邻 3 字修饰。"""
    pre = text[max(0, idx - 3):idx]
    if any(u in pre for u in INTENSITY_UP):
        return 1.5
    if any(d in pre for d in INTENSITY_DOWN):
        return 0.5
    return 1.0


def decompose_emotion(text: str) -> list[dict]:
    """零 LLM 情绪分解。

    Returns: [{family, hit, strength, negated, phrase}]，**全留**（数据层不删）。
    重复族按最高强度去重（同一族命中多次只留最强那次）。
    """
    text = (text or "").strip()
    if not text:
        return []

    # 双重否定 → 留白（刀三：交给表达层，不硬啃）
    for m in DOUBLE_NEG_MARKERS:
        if m in text:
            return []

    raw: list[dict] = []

    # 1) 级联短语（口语主诉，优先级最高）
    for p in CASCADE_PHRASES:
        if p in text:
            raw.append({"family": CASCADE_FAMILY[p], "hit": p,
                        "strength": 1.0, "negated": p.startswith(("不", "没"))})

    # 2) 裸情绪词（长词优先）
    for fam, words in EMOTION_LEXICON.items():
        for w in sorted(words, key=len, reverse=True):
            i = text.find(w)
            if i < 0:
                continue
            pre = text[max(0, i - 2):i]
            negated = any(n in pre for n in NEGATOR)
            eff_fam = NEG_POLARITY_FLIP.get(fam, fam) if negated else fam
            raw.append({"family": eff_fam, "hit": w,
                        "strength": _strength_of(text, i, w),
                        "negated": negated})
            break  # 每族只取最长命中

    # 3) 同族去重：保留最高强度（全留 ≠ 全重，同族重复是噪声）
    best: dict[str, dict] = {}
    for r in raw:
        cur = best.get(r["family"])
        if cur is None or r["strength"] > cur["strength"]:
            best[r["family"]] = r

    out = sorted(best.values(), key=lambda r: -r["strength"])
    for r in out:
        r["phrase"] = _phrase_for(r["family"], r["strength"])
    return out


def select_top(hits: list[dict], n: int = 3) -> list[dict]:
    """注入层限流（刀二）：数据层全留，表达层 top-N。

    按强度降序取前 n 条；同强度保持原序（稳定）。
    """
    return list(hits)[:max(0, n)]
