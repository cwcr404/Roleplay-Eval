# coding: utf-8
"""session/async_quality —— 产品线 Agent3 异步质检判决层（编排 doc §三/§六）。

定位：与同步守门人（改写进玩家流）分家。产品线里 Agent3 是【异步质检】：
主回复已流式发给玩家 → 这里单独跑一程质检 → 产出一份结构化判决 verdict，
落【面板/日志】+ 折成【下一轮】经固定描述壳温和带入 Agent2。【绝不阻塞本轮、
绝不改写玩家已见回复】(防"同步化回退"验收断言)。

本模块专注两件事，其它（web 面板渲染/引擎接线）交给产品线组装：
  1. 用 agent3_async_verdict.md 跑一次判判决，解析成结构化 Verdict。
  2. 把判决卷进固定壳 + 频次闸，产出"能进 agent2 下一轮 prompt"的描述性注入文本。

纪律（与记忆蒸馏/文字入注同源）：
- verdict.reason 只准描述、禁指令；壳外内容(尤其任何"请改写为…")一律不进 agent2 注入。
- 不碰 L1 五项账本、不 judge 人格设定、不 infer 到 is_plot_critical。
- judge LLM 失败 → 记日志降级为 pass+note("质检离线/本次未判")，绝不 pretend 已认真判。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Optional

# 固定描述壳(洞3 · 写死):只准壳内是纯描述;壳外在代码层一概丢弃,不进 agent2 prompt。
SHELL_PREFIX = "上一轮质检意见："
# 连续修正频次闸(洞3b):同向连续修正 ≤N 轮,超闸不再注入只记日志,防渐进式讨好质检。
POLISH_GATE_N = 3
# reason 机械防线(09-08 首府裁决 3a):注入前正则黑名单,指令式句子一律剥离,
# 只留描述部分。黑名单词是「指令动词/祈使句特征」,与纯描述(事实/感受/诊断)相区。
_COMMAND_HINTS = (
    "请改", "请你修改", "请把", "请这样", "下次务必", "记得要", "你应该",
    "你应当", "你应该改", "需要改成", "需改成", "改成", "改为", "你最好",
    "拜托你", "希望你能写", "能不能写", "把这句话写", "重写", "改写", "请删除",
    "请加", "补上", "你要多用", "你要少用", "试着", "尽量做到", "务必保持",
)
import re as _re
_RE_CMDHINT = _re.compile("|".join(_re.escape(w) for w in _COMMAND_HINTS))
# 句子切分:中文句读(。！？!?;)为界(避免把整段误当一个句子)。
_RE_SENT = _re.compile(r"(?<=[。！？!?；;])\s*")


VERDICTS = ("pass", "polish", "blocked")
CATEGORIES = ("客服腔", "OOC", "有用性", "none")
REGISTERS = ("hard", "soft", "none")


@dataclass
class Verdict:
    verdict: str = "pass"        # pass | polish | blocked
    category: str = "none"       # 客服腔 | OOC | 有用性 | none
    register: str = "none"       # hard | soft | none
    anchor: str = ""             # 引用证据句(原样摘录)
    reason: str = ""             # 纯描述(禁指令)
    note: str = ""               # 正向肯定
    sanitized: bool = False       # reason 是否被机械防线剥过(09-08 3a) → 可记 reason_sanitized:true
    ok: bool = False             # 解析成功?

    @property
    def decision(self):
        return self.verdict


def _clean_reason(t: str) -> str:
    """把 reason 切成纯描述:去掉折行空白;整体信任 prompt 契约。"""
    return " ".join((t or "").split())


def parse_verdict(raw: str) -> Verdict:
    """宽松解析判决 JSON；失败只降级成一笔不可靠的 pass(记 ok=False)，不抛。"""
    v = Verdict()
    if not raw or not raw.strip():
        return v
    txt = raw.strip()
    # 剥可能的 ```json ``` 围栏
    if txt.startswith("```"):
        txt = txt.strip("`")
        i = txt.find("\n")
        if i != -1:
            txt = txt[i + 1:]
        txt = txt.strip()
    try:
        # 提取首对 {} 之间的 JSON(允许尾部废话)
        a, b = txt.find("{"), txt.rfind("}")
        if a == -1 or b <= a:
            return v
        obj = json.loads(txt[a:b + 1])
        v.verdict = obj.get("verdict") if obj.get("verdict") in VERDICTS else "pass"
        v.category = obj.get("category") if obj.get("category") in CATEGORIES else "none"
        v.register = obj.get("register") if obj.get("register") in REGISTERS else \
            ("hard" if v.verdict == "blocked" else "soft" if v.verdict == "polish" else "none")
        v.anchor = _clean_reason(obj.get("anchor", ""))
        v.reason = _clean_reason(obj.get("reason", ""))
        v.note = _clean_reason(obj.get("note", ""))
        v.ok = True
    except Exception:
        v.ok = False
    return v


def judge_once(
    prompt: str,
    player_msg: str,
    mode: str,
    reply: str,
    facts: str = "",
    llm_chat: Optional[Callable] = None,
    model: str = "deepseek-chat",
) -> Verdict:
    """对一条 agent2 已发回复跑一次异步质检判决。llm_chat 注入便于真/假两用。"""
    import sys, os
    if llm_chat is None:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from utils.api import chat
        llm_chat = chat
    payload = (
        f"【玩家原话】\n{player_msg}\n\n"
        f"【意图分类】{mode}\n\n"
        f"【Agent2 已发回复】\n{reply}\n\n"
        + (f"【本轮到手素材(供信息核对)】\n{facts}\n\n" if facts else "")
        + "请给出判决 JSON。"
    )
    raw = llm_chat(prompt, payload, model=model, temperature=0.2)
    return parse_verdict(raw)


def sanitize_reason(text: str) -> tuple[str, bool]:
    """reason 机械防线(09-08 裁决 3a):剔除含指令黑名单词的句子,返回 (干净文本, 是否剥过)。"""
    if not text:
        return "", False
    sents = [s.strip() for s in _RE_SENT.split(text) if s and s.strip()]
    if not sents:
        # 无句读分隔 → 整段当一句判
        return ("" if _RE_CMDHINT.search(text) else text), bool(_RE_CMDHINT.search(text))
    kept = [s for s in sents if not _RE_CMDHINT.search(s)]
    hit = len(kept) < len(sents)
    return "".join(kept), hit


def inject_fragment(prev: Optional[Verdict], consecutive_polish: int):
    """把上一轮判决折成『能进下周 agent2 prompt 的注入文本』。

    洞3/洞3b 纪律：
      - 只在 verdict ∈ {polish, blocked} 且未超频次闸时产出；pass → 空(不打扰)。
      - 产出 = 固定壳 + 纯描述 reason(+anchor 原句);任何指令性尾注被丢弃。
      - consecutive_polish 为调用方跟踪的连续修正计数;>=POLISH_GATE_N 则不再注入。
    Returns (注入文本或空串, 是否应把本次计入连续修正计数)。
    """
    if prev is None or prev.verdict == "pass":
        return "", False
    if consecutive_polish >= POLISH_GATE_N:
        # 超出频次闸:不再注入,防渐进式讨好;只留痕迹(返回空,计一次用于计数推进)
        return "", True
    # reason 机械防线:剥指令句(09-08 裁决 3a) —— 剥过则打 sanitized 标记。
    raw = prev.reason or prev.anchor or ""
    clean, hit = sanitize_reason(raw)
    prev.sanitized = hit
    if clean:
        core = clean
    elif raw:
        # 描述部分被全剥光 → 只剩落锚还在,给一句不越界的兜底描述(不重复指令词)
        core = (prev.anchor or "").strip() or "该回应与芽衣的声音或有用性有偏差,详情见质检面板。"
    else:
        core = prev.anchor or "回应偏了仪态,请保持芽衣的从容与有用。"
    return f"{SHELL_PREFIX}{core}", True


# ---------------------------------------------------------------------------
# 决策表(供确定性测试 / 面板同款)
# ---------------------------------------------------------------------------
def to_decision(v: Verdict) -> str:
    """把判决映射到人类可见动作(面板/日志用,非机器路由)。"""
    if v.verdict == "blocked":
        return f"拦({v.category}/{v.register})  {v.reason}"
    if v.verdict == "polish":
        return f"微调({v.category}/{v.register})  {v.reason}"
    return "通过"
