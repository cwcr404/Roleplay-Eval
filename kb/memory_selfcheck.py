# coding: utf-8
"""记忆/关系层本地自检(不调 API,验证 memory_architecture v1.1 的纯逻辑)。

覆盖:隔离(user_id)、好感度读取即算 + time-travel 回放、ritual 升段门槛、
floor/量纲、L2 从 L1 重建容灾、权重钳制边界。
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

# ---- 数据根指向临时目录,避免污染真实 roleplay-eval/data/memory ----
os.environ["KB_DATA_ROOT"] = tempfile.mkdtemp(prefix="kb_mem_selfcheck_")

from kb.relation.params import AffinityParams, clamp_weight
from kb.relation.affinity import tier_index_of, ritual_gate
from kb.memory.service import open_user_memory


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def test_isolated_users():
    """验收1(隔离侧):A/B 两把独立 user_id,互不串味。"""
    a = open_user_memory("u_A")
    b = open_user_memory("u_B")
    a.record_event("玩家身份与偏好", "玩家喜欢甜品", 1.0)
    # A 的好感上升,但 B 仍是初始 50(+无任何事件)
    assert a.affinity_now() > 50.0, f"A should rise, got {a.affinity_now()}"
    assert abs(b.affinity_now() - 50.0) < 1e-6, f"B must stay init 50, got {b.affinity_now()}"
    print("[ok] user_id 隔离: A 上升, B 冷启动停留初始值")


def test_time_travel_decay():
    """验收2:time-travel 回放 + 指数半衰期衰减。"""
    a = open_user_memory("u_travel")
    a.params = AffinityParams(half_life_days=1.0)  # 用 1 天便于观察
    now = _now()
    base = now - 1_000_000   # ~11.6 天前
    # 在 base 写入多笔共同经历(+2 各,钳制上限内)。
    # 注意本测试只验【time-travel + 衰减】,刻意压在建账净额 ~76(<80)让
    # 写时 80+ 递减不参与 —— 那套由 session.acceptance_probe 的跨80断言单独覆盖。
    # 若堆到 90,写在 80+ 的 +2 会被写时折扣压低,写入快照不再等于 50+N*2。
    for i in range(13):
        a.record_event("共同经历", f"玩家与芽衣共同经历第{i}件事", 2.0, ts=base)
    # 回放到写入刚结束时:衰减≈0 -> init50 + 13*2 = 76(未及 80,无折扣,整数)
    snap_at_write = a.affinity_at(base + 60)
    assert 75.5 <= snap_at_write <= 76.5, f"回放到写入时异常: {snap_at_write}"
    # 当下:每笔按 0.5^(天数/1) 衰减 -> 50 + 26*极小 ≈ 50(明显低于写入快照)
    got_now = a.affinity_now()
    assert got_now < snap_at_write - 10, f"衰减未生效: now={got_now}, write={snap_at_write}"
    assert 49.9 <= got_now <= 52, f"当下应接近冷启动剩残值: {got_now}"
    print(f"[ok] time-travel: 写入时快照≈{snap_at_write:.1f}, 当下≈{got_now:.2f}(指数衰减已生效)")


def test_ritual_gate():
    """验收标准 test case:数值 80 但 ritual 未完成,不得越段。"""
    p = AffinityParams()
    cur = tier_index_of(50.0, p)
    # 未完成仪式 -> 被门拦,放行到亲近(2)
    g_no = ritual_gate(90.0, cur, ritual_completed=False, params=p)
    assert g_no.allowed_tier_idx == 2, f"ritual 未完成竟升段: {g_no.allowed_tier}"
    assert g_no.blocked_by_ritual is True
    # 完成仪式 -> 放行到羁绊(3)
    g_yes = ritual_gate(90.0, cur, ritual_completed=True, params=p)
    assert g_yes.allowed_tier_idx == 3
    assert g_yes.blocked_by_ritual is False
    print(f"[ok] ritual 门槛: 90分未做仪式→{g_no.allowed_tier}(拦), 做仪式→{g_yes.allowed_tier}(放)")


def test_floor_and_clamp():
    p = AffinityParams()
    # clamp 边界
    assert clamp_weight("承诺与约定", 9.0) == 3.0, "承诺+3封顶"
    assert clamp_weight("情绪显著时刻", 5.0) == 3.0
    assert clamp_weight("情绪显著时刻", -9.0) == -3.0
    assert clamp_weight("共同经历", 5.0) == 2.0
    assert clamp_weight("玩家身份与偏好", 2.0) == 1.0
    print("[ok] weight_delta 钳制边界")
    # floor:大量负面后不破 5
    um = open_user_memory("u_floor")
    um.params = AffinityParams(floor=5.0)
    for _ in range(60):
        um.record_event("情绪显著时刻", "玩家表达了严重的负面情绪", -3.0)
    aff = um.affinity_now()
    assert aff >= 5.0 - 1e-6, f"跌破 floor: {aff}"
    print(f"[ok] floor=5 兜底: 连扣仍停在 {aff:.2f}")


def test_l2_rebuild_disaster_recovery():
    """验收3:删掉 L2,从 L1 完整重建。"""
    um = open_user_memory("u_l2")
    um.record_event("显式记忆指令", "玩家要求芽衣记住自己怕高", 2.0)
    um.record_event("共同经历", "玩家与芽衣一起看了海市蜃楼", 2.0)
    um.record_event("承诺与约定", "玩家答应下次带芽衣去看极光", 3.0)
    prof1 = um.l2()
    assert prof1 is not None and prof1.data, "L2 未生成"
    had = um.delete_l2_cache()
    assert had, "缓存本应存在"
    prof2 = um.rebuild_l2()
    assert prof2.rebuilt_from_l1 and prof2.data, "重建产物应非空且带 rebuilt 标记"
    assert "怕高" in str(prof2.data), "重建丢失显式记忆"
    print("[ok] L2 容灾重建: 删除→从 L1 重建,记忆无损(rebuilt_from_l1=True)")


def main():
    test_isolated_users()
    test_time_travel_decay()
    test_ritual_gate()
    test_floor_and_clamp()
    test_l2_rebuild_disaster_recovery()
    print("\n[OK] 记忆/关系自检全部通过")


if __name__ == "__main__":
    main()
