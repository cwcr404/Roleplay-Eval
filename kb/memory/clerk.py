# coding: utf-8
"""kb.memory.clerk —— 书记员（L3 记账旁路进程）。

首府裁定（2026-10-06 · 入场规则 + 书记员两段式）：
- 书记员是【旁路进程】：打分/提名，不生成对话、不扮演角色。
- 两段式：每轮粗筛（便宜小模型，只吐情绪强度 0-10）
  → <4 只记 L1；≥4 才全套（双维标签/显式性/场景词/JSON）。
- API 挂了 → weight 退启发式，L1 标「降级」。
- 不用 LLM 参与「找」：粗筛只出数；全套出 JSON；检索是本地词法。

挂载点（裁定 · 挂载点）：
- record_event()（不动，书记员旁路挂此）
- l2()（L3 注入块同出口）
- signal()（传输通道）
- affinity 层（平行零写入）

本模块零 LLM 依赖时可跑（mock 模式）——真实模型接线停在等迟旭选模型/起服务。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

# ── prompt 模板（首府提供 stub v1，迟旭终审前可替换）────────
SCREEN_PROMPT = """\
你是情绪强度粗筛器。读下面这轮对话（用户消息+芽衣回复），只输出一个 0-10 的整数：这轮用户的情绪强度（绝对值，不分正负。平静日常=1-3，明显起伏=4-6，强烈=7-10）。只输出数字，不输出任何其他文字。

对话：
{round}"""

FULL_PROMPT = """\
你是「书记员」，维护「他」与芽衣之间的记忆账本。只出账，不生成对话、不扮演任何角色。分析对话记录，只输出一个 JSON 对象，无其他文字、无 markdown 标记。结构：
{{"l1_rows":[{{"round":n,"tags":"关系维×情绪维","intensity":0-10,"explicit":bool,"scene_words":["按三层规范抽2-5个"],"note":"一行"}}],
"l3_nomination":bool,
"l3_nominee":{{"date":"YYYY-MM-DD","content":"一句话具体画面，写画面不写评价","tags":"关系维×情绪维","storage":"=当时情绪强度","scene_words":[...],"reason":"为什么值得记一辈子（一句话）"}},
"l2_pack":"≤500字：显式登记+温度+当前关系一句话"}}
纪律：L1 全量每轮一行不省略；L3 提名三条件（隐式涌现+强度绝对值≥6+不重复）任一不满足则 nomination=false 且 nominee 留空；每次最多提名 1 条；只写真实发生的，宁缺毋滥。

【现有 L3 条目（查重用）】
{existing}

【对话记录】
{conversation}"""


# ── 结果结构 ────────────────────────────────────────────────
@dataclass
class ScreenResult:
    intensity: int
    degraded: bool = False          # True = 启发式回退（API 挂了）


@dataclass
class FullResult:
    l1_rows: list[dict] = field(default_factory=list)
    l3_nomination: bool = False
    l3_nominee: Optional[dict] = None
    l2_pack: str = ""
    degraded: bool = False
    raw: str = ""


# ── 启发式回退（API 挂了时用，非 LLM）────────────────────────
_STRONG_WORDS = ("崩溃", "难受", "难受", "痛苦", "绝望", "孤独", "委屈",
                 "生气", "愤怒", "害怕", "紧张", "焦虑", "感动", "开心",
                 "惊喜", "难过", "想哭", "撑不住", "熬不住", "谢谢")
_WEAK_MARKERS = ("嗯", "哦", "好的", "收到", "在", "行")


def heuristic_intensity(user_msg: str, reply: str = "") -> int:
    """零 LLM 启发式情绪强度（降级用）。宁可低估，不夸大。"""
    text = (user_msg or "") + (reply or "")
    score = 3
    for w in _STRONG_WORDS:
        if w in text:
            score += 2
            break
    if "！" in text or "!" in text:
        score += 1
    if len(user_msg or "") > 60:
        score += 1
    if (user_msg or "").strip() in _WEAK_MARKERS:
        score = 2
    return max(1, min(10, score))


def parse_intensity(raw: str) -> Optional[int]:
    """从粗筛输出里抠出 0-10 整数（容忍噪声）。"""
    if not raw:
        return None
    m = re.search(r"\b(10|[0-9])\b", raw.strip())
    if not m:
        return None
    v = int(m.group(1))
    return v if 0 <= v <= 10 else None


def parse_full_json(raw: str) -> Optional[dict]:
    """从全套输出里抠 JSON（容忍 markdown 围栏/前后噪声）。"""
    if not raw:
        return None
    t = raw.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j == -1 or j <= i:
        return None
    try:
        return json.loads(t[i:j + 1])
    except Exception:
        return None


# ── 书记员 ─────────────────────────────────────────────────
class Clerk:
    """两段式书记员。

    llm: 可调用对象 (prompt: str) -> str；None 则走 mock/启发式。
    """

    def __init__(self, llm: Optional[Callable[[str], str]] = None,
                 *, mock_full: Optional[dict] = None) -> None:
        self.llm = llm
        self.mock_full = mock_full

    def screen(self, user_msg: str, reply: str = "") -> ScreenResult:
        """粗筛：只出情绪强度。"""
        if self.llm is None:
            return ScreenResult(heuristic_intensity(user_msg, reply), degraded=True)
        rnd = f"用户：{user_msg}\n芽衣：{reply}"
        try:
            raw = self.llm(SCREEN_PROMPT.format(round=rnd))
            v = parse_intensity(raw)
            if v is None:
                return ScreenResult(heuristic_intensity(user_msg, reply), degraded=True)
            return ScreenResult(v)
        except Exception:
            return ScreenResult(heuristic_intensity(user_msg, reply), degraded=True)

    def full(self, conversation: str, existing: list = None) -> FullResult:
        """全套：≥4 触发，出 L1 行 + L3 提名 + L2 包。"""
        existing = existing or []
        existing_txt = json.dumps(
            [{"id": getattr(it, "id", "?"), "content": getattr(it, "content", ""),
              "scene_words": getattr(it, "scene_words", [])} for it in existing],
            ensure_ascii=False)
        if self.llm is None:
            if self.mock_full is not None:
                return FullResult(
                    l1_rows=self.mock_full.get("l1_rows", []),
                    l3_nomination=self.mock_full.get("l3_nomination", False),
                    l3_nominee=self.mock_full.get("l3_nominee"),
                    l2_pack=self.mock_full.get("l2_pack", ""),
                )
            return FullResult(degraded=True)
        try:
            raw = self.llm(FULL_PROMPT.format(
                existing=existing_txt, conversation=conversation))
            obj = parse_full_json(raw)
            if obj is None:
                return FullResult(degraded=True, raw=raw)
            return FullResult(
                l1_rows=obj.get("l1_rows", []),
                l3_nomination=bool(obj.get("l3_nomination", False)),
                l3_nominee=obj.get("l3_nominee") or None,
                l2_pack=obj.get("l2_pack", ""),
                raw=raw,
            )
        except Exception:
            return FullResult(degraded=True)

    # ── 端到端：一轮对话 → 记账 ──
    def process_round(self, user_msg: str, reply: str,
                      conversation: str = "", existing: list = None,
                      *, threshold: int = 4) -> dict:
        """一轮完整流程：粗筛 → (≥threshold) 全套。

        返回 {"intensity","degraded","full":FullResult|None}
        """
        scr = self.screen(user_msg, reply)
        out = {"intensity": scr.intensity, "degraded": scr.degraded, "full": None}
        if scr.intensity >= threshold:
            out["full"] = self.full(conversation or
                                    f"用户：{user_msg}\n芽衣：{reply}", existing)
        return out
