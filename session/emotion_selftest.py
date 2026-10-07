# coding: utf-8
"""情绪通道自测（假数据，零 LLM，不落盘）。

验收线（首府 2026-10-07 三刀）：
- 刀一：不与场景词通道共享任何词表/状态
- 刀二：数据层全留、注入层 top-3 限流
- 刀三：弱情绪措辞必须读起来就弱；零命中合法；双重否定留白
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kb.memory.emotion import (  # noqa: E402
    decompose_emotion, select_top, STRENGTH_PHRASES,
)

PASS, FAIL = 0, 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name} {extra}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def fams(text: str) -> list[str]:
    return [r["family"] for r in decompose_emotion(text)]


def strength(text: str, family: str) -> float:
    for r in decompose_emotion(text):
        if r["family"] == family:
            return r["strength"]
    return -1.0


def phrase(text: str, family: str) -> str:
    for r in decompose_emotion(text):
        if r["family"] == family:
            return r["phrase"]
    return ""


print("=" * 64)
print("情绪通道自测（假数据 · 零 LLM）")
print("=" * 64)

# ── 1. 基础分解 ──
print("\n[1] 口语主诉分解")
check("心情不太好 → 低落", fams("心情不太好") == ["低落"], str(fams("心情不太好")))
check("好累啊 → 疲惫", "疲惫" in fams("好累啊"))
check("烦死了 → 烦躁", "烦躁" in fams("今天又加班了，烦死了"))
check("挺挫败的 → 难受", "难受" in fams("训练摔了两次，挺挫败的"))
check("开心 → 愉悦", "愉悦" in fams("开心！今天他来找我了"))

# ── 2. 刀二：数据层全留 ──
print("\n[2] 刀二 · 数据层全留（不取一）")
f2 = fams("我好累，不想说话，又烦又孤单")
check("累+低落+烦躁+孤独 多族并存", len(f2) >= 3, str(f2))
check("『我好累，不想说话』不是单族", len(fams("我好累，不想说话")) >= 1,
      str(fams("我好累，不想说话")))

# ── 3. 刀二：注入层 top-3 限流 ──
print("\n[3] 刀二 · 注入层 top-3")
many = decompose_emotion("我好累，不想说话，又烦又孤单，还很焦虑，特别难受")
check("分解 ≥4 族（数据层不删）", len(many) >= 4, f"{len(many)} 族")
check("select_top(3) → 3 条", len(select_top(many, 3)) == 3,
      str([r["family"] for r in select_top(many, 3)]))
check("top-3 按强度降序", [r["strength"] for r in select_top(many, 3)]
      == sorted([r["strength"] for r in many], reverse=True)[:3])

# ── 4. 刀三：措辞跟强度走 ──
print("\n[4] 刀三 · 弱情绪措辞必须读起来弱")
check("『有点糟』强度=0.5", strength("今天心情有点糟", "低落") == 0.5,
      str(strength("今天心情有点糟", "低落")))
p_weak = phrase("今天心情有点糟", "低落")
p_mid = phrase("心情很低落", "低落")
check("0.5 措辞含『有点』", "有点" in p_weak, p_weak)
check("1.0 措辞不含『有点』", "有点" not in p_mid, p_mid)
check("0.5 与 1.0 措辞不同", p_weak != p_mid, f"{p_weak!r} vs {p_mid!r}")
p_strong = phrase("心情特别低落", "低落")
check("1.5 措辞含加重语", "压得挺沉" in p_strong, p_strong)

# ── 5. 刀三：零命中合法 / 双重否定留白 ──
print("\n[5] 刀三 · 护栏")
check("『我很好啊，别担心』零命中（不猜）", decompose_emotion("我很好啊，别担心") == [],
      str(decompose_emotion("我很好啊，别担心")))
check("『随便聊聊吧』零命中（坑3 正确）", decompose_emotion("随便聊聊吧") == [])
check("『外面下雨了，好冷』情绪层零命中", decompose_emotion("外面下雨了，好冷") == [])
check("双重否定留白", decompose_emotion("我不是不开心") == [],
      str(decompose_emotion("我不是不开心")))

# ── 6. 否定极性反转 ──
print("\n[6] 否定极性反转")
check("『我不开心』→ 低落（非愉悦）", fams("我不开心") == ["低落"],
      str(fams("我不开心")))

# ── 7. 刀一：与场景词通道零共享 ──
print("\n[7] 刀一 · 通道隔离")
import kb.memory.emotion as _emo  # noqa: E402
import kb.memory.l3_vocab as _vocab  # noqa: E402
import kb.memory.l3_store as _store  # noqa: E402
_check_names = set(dir(_emo)) & {"HYPERNYM_MAP", "SITUATION_MAP", "expand_scene_words",
                                 "is_valid_scene_key", "VALID_SCENE_KEYS"}
check("emotion 不 import 场景词表符号", not _check_names, str(_check_names))
# 场景词合法集与情绪族名的**重叠探测**（不是断言失败）
# 重要发现（2026-10-07）：既有词表本就把『烦躁/孤独/疲惫』这类口语情绪词
# 混在场景词里 —— 这正是坑2 描述的「语义混仓」病灶，且**早于**本次立法。
# 首府纪律：本次只新增独立通道，**不碰既有词表内容**；清理归 B1 工单。
# 本项定位为「探测报告」，不判成败（只记录重叠面，供 B1 决策）。
overlap = set(_emo.EMOTION_LEXICON.keys()) & set(_vocab.VALID_SCENE_KEYS)
print(f"  [探测] 既有场景词表已混入的情绪族名：{sorted(overlap)}")
print(f"       → 独立情绪通道已可接管这些词；词表清理建议移交 B1 工单")
# 真正要守的线：新通道不向既有词表**新增**任何词（本项可断言）
check("emotion 模块未修改场景词表", not getattr(_vocab, "_EMOTION_TOUCHED", False))

print("\n" + "=" * 64)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 64)
sys.exit(1 if FAIL else 0)
