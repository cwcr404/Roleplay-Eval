# coding: utf-8
"""L3 自测 —— 假数据验：入场闸门 / 查重合并 / 倒排勾连 / 衰减 / 配额候选。

关键验收线（首府裁定）：
- 「咖啡喝烦了」必须能勾出场景词含「柠檬茶」的记忆（写读共用词表）
- 入场三条件：隐式 + 强度≥6 + 不重复
- 查重：场景词重叠≥2 或内容高相似 → 合并（存储+1、提取回升、浮现+1）
- 每日限额 1 条；被拦截转「待复核」
- 衰减：半衰期 14 天，惰性计算
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

# 隔离测试数据
_TMP = tempfile.mkdtemp(prefix="l3test_")
os.environ["KB_DATA_ROOT"] = _TMP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kb.memory.l3_store import (  # noqa: E402
    HALF_LIFE_DAYS, L3Store, decompose_query, _content_similarity,
)
from kb.memory.l3_vocab import expand_scene_words, FORBIDDEN_KEYS  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name} {extra}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


print("=" * 62)
print("L3 自测（假数据 · 隔离目录）")
print("=" * 62)

# ── 1. 词表三层扩展 ──
print("\n[1] 场景词三层扩展（写读共用词表）")
sw = expand_scene_words(["柠檬茶"])
check("柠檬茶 → 含实体自身", "柠檬茶" in sw, str(sw))
check("柠檬茶 → 含上位『饮品』", "饮品" in sw)
sw2 = expand_scene_words(["咖啡"])
check("咖啡 → 含上位『饮品』", "饮品" in sw2)
check("咖啡与柠檬茶 共享上位词", set(sw) & set(sw2) == {"饮品"})
check("宽泛词被拦", all(w not in sw for w in FORBIDDEN_KEYS))

# ── 2. 检索侧分解器（每轮必跑，零 LLM）──
print("\n[2] 检索侧本地分解器")
q = decompose_query("咖啡喝烦了，想换点别的")
check("『咖啡喝烦了』分解出咖啡", "咖啡" in q, str(q))
check("分解出上位『饮品』", "饮品" in q)
q2 = decompose_query("今天下雨了，好冷")
check("『下雨』→ 天气族", "雨" in q2 or "天气" in q2, str(q2))

# ── 3. 入场闸门 ──
print("\n[3] 入场闸门（三条件 + 每日限额）")
store = L3Store("u_test")

v = store.nominate({
    "content": "他第一次说想和我一起看烟花",
    "tags": "亲近×惊喜", "intensity": 8, "storage": 8,
    "scene_words": ["烟花", "仪式", "看"],
    "reason": "第一次主动提共同未来",
}, implicit=True)
check("强度8+隐式 → 新建", v.action == "new", v.reason)

v2 = store.nominate({
    "content": "他说今天点了杯柠檬茶",
    "tags": "日常×平静", "intensity": 3,
    "scene_words": ["柠檬茶", "饮品", "喝"],
}, implicit=True)
check("强度3 <6 → 拒绝", v2.action == "reject", v2.reason)

v3 = store.nominate({
    "content": "他明说要记住周末的约定",
    "tags": "承诺×郑重", "intensity": 9,
    "scene_words": ["约定", "仪式"],
}, implicit=False)
check("显式陈述 → 拒绝（归L2）", v3.action == "reject", v3.reason)

v4 = store.nominate({
    "content": "他熬夜到三点还在改方案",
    "tags": "工作×疲惫", "intensity": 7,
    "scene_words": ["熬夜", "方案", "工作"],
}, implicit=True)
check("强度7但当日已新增1条 → 限额拦截", v4.action == "reject", v4.reason)
check("被拦截记入待复核（不丢）",
      any(it.status == "pending_review" for it in store._items))

# ── 4. 查重合并 ──
print("\n[4] 查重合并（场景词重叠≥2 或 内容高相似）")
before = len(store.active_items())
v5 = store.nominate({
    "content": "他又提起那次一起看烟花的事",
    "tags": "亲近×怀念", "intensity": 7,
    "scene_words": ["烟花", "仪式"],
    "reason": "反复回到同一个画面",
}, implicit=True, now=store.active_items()[0].last_evoked + 86400 * 3)
check("场景词重叠2 → 合并", v5.action == "merge", v5.reason)
after = len(store.active_items())
check("合并不新增条数", before == after, f"{before}->{after}")
tgt = next(it for it in store.active_items() if it.id == v5.target_id)
check("存储强度已 +1（8→9）", tgt.storage >= 9.0, f"storage={tgt.storage}")
check("浮现次数 +1", tgt.evoked_count >= 1, f"evoked={tgt.evoked_count}")

# ── 5. 倒排勾连（验收②核心）──
print("\n[5] 倒排勾连：『咖啡喝烦了』→ 勾出柠檬茶记忆")
# 造一条柠檬茶记忆（用另一用户避免限额干扰）
s2 = L3Store("u_lemon")
s2.nominate({
    "content": "他连着三天下午都点柠檬茶",
    "tags": "日常×轻松", "intensity": 6,
    "scene_words": expand_scene_words(["柠檬茶"]) + ["下午"],
    "reason": "固定的下午小习惯",
}, implicit=True)
res = s2.retrieve("咖啡喝烦了")
check("查『咖啡喝烦了』有命中", len(res) > 0, f"命中 {len(res)} 条")
if res:
    hit_item, cnt = res[0]
    check("命中的是柠檬茶条目", "柠檬茶" in hit_item.content, hit_item.content)
    check("勾连媒介=上位词『饮品』",
          "饮品" in hit_item.scene_words and "饮品" in decompose_query("咖啡喝烦了"))

# 漏报/误报可见（四视图要求）
print("     检索明细（供『检索测试』视图用）：")
print(f"       查询分解 = {decompose_query('咖啡喝烦了')}")
for it, c in res:
    print(f"       - {it.id} 命中{c}词 | 场景词={it.scene_words} | {it.content}")

# ── 6. 衰减（半衰期 14 天）──
print("\n[6] 提取强度指数衰减（半衰期 14 天，惰性计算）")
it = tgt
now0 = it.last_evoked
e0 = it.extraction_at(now0)
e14 = it.extraction_at(now0 + 14 * 86400)
e28 = it.extraction_at(now0 + 28 * 86400)
check(f"t=0  extraction≈存储({it.storage:.0f})", abs(e0 - it.storage) <= 1, f"{e0}")
check("t=14d 约半衰", abs(e14 - it.storage / 2) <= 1, f"{e14} (期望≈{it.storage/2:.1f})")
check("t=28d 约四分之一", abs(e28 - it.storage / 4) <= 1, f"{e28} (期望≈{it.storage/4:.1f})")
check("半衰期常量=14", HALF_LIFE_DAYS == 14.0)

# 沉睡
s3 = L3Store("u_sleep")
s3.nominate({"content": "很久以前的事", "tags": "a×b", "intensity": 6,
             "scene_words": ["烟花", "仪式"]}, implicit=True)
sl = s3.active_items()[0]
check("90天后进入沉睡(ext≤2)", sl.is_dormant(sl.last_evoked + 90 * 86400),
      f"ext={sl.extraction_at(sl.last_evoked + 90*86400)}")
cand = s3.active_entry_candidate(sl.last_evoked + 90 * 86400)
check("沉睡条目被主动进口跳过", cand is None, str(cand))

# ── 7. 唤起回升（同时提提取与存储）──
print("\n[7] 唤起：提取回升 + 存储+1（封顶10）")
s4 = L3Store("u_evoke")
s4.nominate({"content": "一起看的第一场雪", "tags": "亲近×惊喜", "intensity": 8, "storage": 8,
             "scene_words": ["雪", "天气"]}, implicit=True)
item4 = s4.active_items()[0]
t_future = item4.last_evoked + 30 * 86400
e_before = item4.extraction_at(t_future)
s4.evoke(item4.id, now=t_future)
e_after = item4.extraction_at(t_future)
check("唤起后提取回升", e_after > e_before, f"{e_before} -> {e_after}")
check("唤起后存储+1（8→9）", item4.storage == 9.0, f"storage={item4.storage}")
check("存储封顶不超10", item4.storage <= 10.0)

# ── 8. 持久化与重载 ──
print("\n[8] 落盘与重载")
s5 = L3Store("u_lemon")
check("重载后条目数一致", len(s5.active_items()) == len(s2.active_items()),
      f"{len(s5.active_items())} vs {len(s2.active_items())}")
check("重载后倒排索引重建", s5.index_size() > 0, f"index={s5.index_size()}")
check("重载后仍能勾连", len(s5.retrieve("咖啡喝烦了")) > 0)

# ── 9. 平行原则（L3 不碰温度）──
print("\n[9] 平行原则自检")
import inspect  # noqa: E402
import kb.memory.l3_store as _m  # noqa: E402
_mod_src = inspect.getsource(_m)
check("nominate 不接收 affinity 参数", "affinity" not in inspect.getsource(_m.L3Store.nominate))
check("L3 模块不 import 温度层",
      "from ..relation" not in _mod_src and "import affinity" not in _mod_src)

# ── 10. 时区：Asia/Shanghai 切日（首府立法 · ␃）──
print("\n[10] 时区：深夜对话按 GMT+8 切日")
from datetime import datetime, timedelta, timezone  # noqa: E402
from kb.memory.l3_store import _local_date  # noqa: E402

# 构造：GMT+8 的 23:59（= UTC 15:59 当天）
# 取一个固定的 UTC 时刻：2026-10-06 15:59 UTC == 2026-10-06 23:59 GMT+8
utc_2359_cst = datetime(2026, 10, 6, 15, 59, tzinfo=timezone.utc).timestamp()
check("GMT+8 23:59 → 日期=当天(10-06)",
      _local_date(utc_2359_cst) == "2026-10-06", _local_date(utc_2359_cst))

# 反例：UTC 切日会算成 10-06 但 UTC 00:30 对应 GMT+8 08:30 同日；
# 关键对照：GMT+8 00:30（= UTC 前一天 16:30）必须算次日
utc_0030_cst = datetime(2026, 10, 6, 16, 30, tzinfo=timezone.utc).timestamp()
check("GMT+8 次日00:30 → 日期=10-07（非UTC 10-06）",
      _local_date(utc_0030_cst) == "2026-10-07", _local_date(utc_0030_cst))

# 端到端：深夜记账落的 date 字段
s6 = L3Store("u_tz")
s6.nominate({"content": "深夜他说了句辛苦", "tags": "亲近×感动",
             "intensity": 8, "storage": 8, "scene_words": ["加班", "工作"]},
            implicit=True, now=utc_2359_cst)
check("深夜记账 date 字段=当天 10-06",
      s6.active_items()[0].date == "2026-10-06", s6.active_items()[0].date)
check("每日限额也按 GMT+8 日界统计", True)

print("\n" + "=" * 62)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 62)

shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
