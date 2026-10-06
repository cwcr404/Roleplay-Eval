# coding: utf-8
"""书记员自测 —— mock 模式（不烧 token）。

验：
- 粗筛：mock LLM 出数 / 无 LLM 走启发式并标 degraded
- 阈值：<4 只记 L1（不跑全套）；≥4 才全套
- JSON 解析：容忍 markdown 围栏/噪声
- API 挂 → 降级，不冒充真实
- 端到端：一轮对话 → 记账
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kb.memory.clerk import (  # noqa: E402
    Clerk, heuristic_intensity, parse_full_json, parse_intensity,
)

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   %s %s" % (name, extra))
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


print("=" * 62)
print("书记员自测（mock 模式）")
print("=" * 62)

# ── 1. 解析工具 ──
print("\n[1] 解析工具（容忍噪声）")
check("纯数字", parse_intensity("7") == 7)
check("带噪声", parse_intensity("这轮强度大概是 8 分") == 8)
check("10 分", parse_intensity("10") == 10)
check("空 → None", parse_intensity("") is None)
check("围栏 JSON", parse_full_json('```json\n{"a":1}\n```') == {"a": 1})
check("前后噪声", parse_full_json('好的：{"a":1} 完成') == {"a": 1})
check("坏 JSON → None", parse_full_json("{not json") is None)

# ── 2. 启发式回退 ──
print("\n[2] 启发式回退（零 LLM）")
check("平淡 → 低分", heuristic_intensity("嗯", "") <= 3,
      str(heuristic_intensity("嗯", "")))
check("强情绪 → 高分", heuristic_intensity("我真的崩溃了", "") >= 5,
      str(heuristic_intensity("我真的崩溃了", "")))
check("范围 1-10", 1 <= heuristic_intensity("谢谢！" * 30, "") <= 10)

# ── 3. 无 LLM → 降级 ──
print("\n[3] 无 LLM → 降级（不冒充真实）")
c0 = Clerk(llm=None)
s0 = c0.screen("今天好累", "")
check("粗筛标记 degraded", s0.degraded is True)
f0 = c0.full("用户：今天好累")
check("全套标记 degraded", f0.degraded is True)

# ── 4. mock LLM 粗筛 ──
print("\n[4] mock LLM 粗筛")
c1 = Clerk(llm=lambda p: "7")
s1 = c1.screen("今天真的好累", "早点休息")
check("粗筛 7", s1.intensity == 7, str(s1.intensity))
check("非降级", s1.degraded is False)

# ── 5. 阈值：<4 不跑全套 ──
print("\n[5] 阈值：<4 只记 L1")
calls = []
def llm_probe(p):
    calls.append(p)
    return "3"
c2 = Clerk(llm=llm_probe)
r2 = c2.process_round("嗯", "嗯", threshold=4)
check("强度3 → 不跑全套（只 1 次 LLM 调用）", len(calls) == 1, "calls=%d" % len(calls))
check("full 为 None", r2["full"] is None)

# ── 6. 阈值：≥4 跑全套 ──
print("\n[6] 阈值：≥4 才全套")
mock_full = {
    "l1_rows": [{"round": 1, "tags": "亲近×感动", "intensity": 8,
                 "explicit": False, "scene_words": ["雨", "天气"], "note": "他说来接我"}],
    "l3_nomination": True,
    "l3_nominee": {"date": "2026-10-06", "content": "暴雨天他来接我",
                   "tags": "亲近×感动", "storage": 8,
                   "scene_words": ["雨", "天气"], "reason": "第一次被接"},
    "l2_pack": "关系：已熟络",
}
c3 = Clerk(llm=lambda p: "8")
# 全套走 mock_full 需无 llm；改用能返回 JSON 的 mock LLM
import json as _json  # noqa: E402
c3b = Clerk(llm=lambda p: _json.dumps(mock_full, ensure_ascii=False))
r3scr = c3.screen("下暴雨了", "我来接你")
check("粗筛 8", r3scr.intensity == 8)
r3 = c3b.full("用户：下暴雨了\n芽衣：我来接你")
check("强度8 → 跑全套", r3 is not None)
check("L3 提名被解析", r3.l3_nomination is True)
check("nominee content 正确",
      r3.l3_nominee["content"] == "暴雨天他来接我")
check("l1_rows 解析", len(r3.l1_rows) == 1)

# mock_full 路径（无 LLM）
c3c = Clerk(llm=None, mock_full=mock_full)
f3c = c3c.full("x")
check("mock_full 无 LLM 可跑", f3c.l3_nomination is True)

# process_round 端到端（能返回 JSON 的 LLM）
def _llm_router(p):
    if "粗筛器" in p:
        return "8"
    return _json.dumps(mock_full, ensure_ascii=False)
c3d = Clerk(llm=_llm_router)
r3d = c3d.process_round("下暴雨了", "我来接你", threshold=4)
check("端到端：强度8 走全套", r3d["full"] is not None
      and r3d["full"].l3_nomination)

# ── 7. LLM 输出坏 JSON → 降级 ──
print("\n[7] LLM 坏输出 → 降级")
c4 = Clerk(llm=lambda p: "抱歉我不会")
f4 = c4.full("用户：x")
check("坏输出 → degraded", f4.degraded is True)

# ── 8. LLM 崩 → 降级不抛 ──
print("\n[8] LLM 异常 → 降级不抛")
def boom(p):
    raise RuntimeError("API 挂了")
c5 = Clerk(llm=boom)
s5 = c5.screen("今天好累", "")
check("异常 → 启发式 + degraded", s5.degraded is True)
f5 = c5.full("x")
check("全套异常 → degraded", f5.degraded is True)

# ── 9. prompt 契约（首府原文）──
print("\n[9] prompt 契约")
from kb.memory.clerk import FULL_PROMPT, SCREEN_PROMPT  # noqa: E402
check("粗筛含『只输出数字』", "只输出数字" in SCREEN_PROMPT)
check("全套含三条件纪律", "隐式涌现" in FULL_PROMPT and "≥6" in FULL_PROMPT)
check("全套含查重占位", "{existing}" in FULL_PROMPT)
check("全套含对话占位", "{conversation}" in FULL_PROMPT)
check("全套 JSON 花括号已转义", '{{"l1_rows"' in FULL_PROMPT)
# 渲染不炸
_ = FULL_PROMPT.format(existing="[]", conversation="用户：x")
check("prompt 渲染不炸", True)

print("\n" + "=" * 62)
print("结果：%d 通过 / %d 失败" % (PASS, FAIL))
print("=" * 62)
sys.exit(1 if FAIL else 0)
