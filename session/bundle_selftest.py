# coding: utf-8
"""注入块打包自测 —— 与 L2 同出口 + 出口纪律。

验：
- 关系块 / 记忆块 / 降级块 拼装
- 结构化状态（status/degraded）绝不进注入文本
- 降级条目只出一句话确认，不展开
- 预算：保记忆块，截关系块
- 端到端：UserMemory.injection()（隔离目录）
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 隔离数据目录（在 import 前设，防污染真账本）
_TMP = tempfile.mkdtemp(prefix="bundle_test_")
os.environ["KB_DATA_ROOT"] = _TMP

from kb.memory.bundle import (  # noqa: E402
    Hit, InjectBundle, build_bundle, HEAD_FADED, HEAD_MEMORY, HEAD_RELATION,
)
from kb.memory.l3_store import L3Store  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   %s %s" % (name, extra))
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


class _FakeL2:
    """最小 L2Profile 替身（只实现 to_markdown + distill_status）。"""
    def __init__(self, data, status="ok"):
        self.data = data
        self.distill_status = status

    def to_markdown(self):
        return "\n".join("- %s: %s" % (k, v) for k, v in self.data.items())


def _mk_item(content, scene_words, intensity=8, storage=8, uid="u1", n=1):
    st = L3Store(uid)
    st.nominate({"content": content, "tags": "a×b", "intensity": intensity,
                 "storage": storage, "scene_words": scene_words}, implicit=True)
    return st.active_items()[-1]


print("=" * 62)
print("注入块打包自测")
print("=" * 62)

# ── 1. 只有关系块 ──
print("\n[1] 只有关系块")
l2 = _FakeL2({"我们的故事": "已熟络，聊过几次深夜的事", "他是谁": "叫他舰长"})
b1 = build_bundle(l2, [])
check("含【关系】头", HEAD_RELATION in b1.text)
check("含画像内容", "已熟络" in b1.text)
check("无【记忆】头", HEAD_MEMORY not in b1.text)
check("l2_status 在系统侧", b1.l2_status == "ok")

# ── 2. 记忆块拼装 ──
print("\n[2] 记忆块拼装")
it1 = _mk_item("他说想一起去看海", ["天气", "约定"], uid="u2")
it2 = _mk_item("他第一次给我带早餐", ["早餐", "食物"], uid="u3")
b2 = build_bundle(l2, [Hit(item=it1, medium="上位词『约定』"),
                       Hit(item=it2, medium="上位词『食物』")])
check("含【记忆】头", HEAD_MEMORY in b2.text)
check("含条目1", "看海" in b2.text)
check("含条目2", "早餐" in b2.text)
check("n_memory=2", b2.n_memory == 2, str(b2.n_memory))
check("命中媒介不进注入文本", "上位词" not in b2.text)

# ── 3. 出口纪律：结构化状态不进文本 ──
print("\n[3] 出口纪律（防把系统状态当剧情演）")
l2_bad = _FakeL2({"我们的故事": "刚认识"}, status="memory_not_distilled")
b3 = build_bundle(l2_bad, [])
check("distill_status 不进 text", "memory_not_distilled" not in b3.text)
check("distill_status 在系统侧", b3.l2_status == "memory_not_distilled")
b3b = build_bundle(_FakeL2({"x": "y"}, status="budget_truncated"), [])
check("budget_truncated 不进 text", "budget_truncated" not in b3b.text)

# ── 4. 降级块：只一句话确认，不展开 ──
print("\n[4] 降级块（不应期内）")
it3 = _mk_item("那棵银杏树下他说了重要的话", ["雪", "天气"], uid="u4")
# 唤起一次（被动）→ 落入 72h 不应期
st4 = L3Store("u4")
st4.evoke(it3.id, now=it3.last_evoked, active=False)
now4 = it3.last_evoked + 3600
b4 = build_bundle(l2, [Hit(item=it3, medium="x", faded=True)])
check("含【近忆】头", HEAD_FADED in b4.text)
check("降级只一句话确认", "（嗯，你说过" in b4.text)
check("降级不展开全文", "重要的话" not in b4.text)
check("降级不进【记忆】块", HEAD_MEMORY not in b4.text)

# ── 5. 预算：保记忆块，截关系块 ──
print("\n[5] 预算（保记忆块，截关系块）")
l2_huge = _FakeL2({"我们的故事": "很长很长的关系叙述。" * 60})
b5 = build_bundle(l2_huge, [Hit(item=it1, medium="x")], budget=200)
check("超预算标记 truncated", b5.truncated is True)
check("记忆块被保", "看海" in b5.text)
check("总长 ≤ 预算", len(b5.text) <= 200, "len=%d" % len(b5.text))

# ── 6. 端到端：UserMemory.injection() ──
print("\n[6] 端到端：UserMemory.injection()")
from kb.memory.service import open_user_memory  # noqa: E402
m = open_user_memory("u_e2e")
m.record_event("共同经历", "一起在雨里跑回家", 5.0)
st6 = L3Store("u_e2e")
st6.nominate({"content": "暴雨天他撑着伞来接我", "tags": "亲近×感动",
              "intensity": 8, "storage": 8,
              "scene_words": ["雨", "天气"]}, implicit=True)
inj = m.injection("今天下雨了", k=3)
check("注入含【记忆】", HEAD_MEMORY in inj)
check("检索命中雨伞条目", "伞" in inj or "接我" in inj)
check("注入含【关系】(L2)", HEAD_RELATION in inj or "共同经历" in inj,
      "（L2 可能待蒸馏，此处容错）")
check("注入无系统状态词", "memory_not_distilled" not in inj
      and "budget_truncated" not in inj)

# 空查询：不检 L3，只出关系块
inj2 = m.injection("")
check("空查询不含【记忆】", HEAD_MEMORY not in inj2)

print("\n" + "=" * 62)
print("结果：%d 通过 / %d 失败" % (PASS, FAIL))
print("=" * 62)
sys.exit(1 if FAIL else 0)
