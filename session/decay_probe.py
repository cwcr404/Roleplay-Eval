# coding: utf-8
"""session.decay_probe —— 关系衰减【纯函数】单测矩阵(离线,零 API)。

验收对象:kb/relation/affinity.py 的纯函数机械正确性。
文档依据:docs/memory_architecture.md §四(衰减公式) / §五(量纲·升段·80+)。

覆盖四项(2026-09-13 迟旭裁定):
  ① 半衰期曲线:T+1/7/14/30/90 断言区间(14 天为硬锚 = 恰好 0.5)
  ② 层切换边界:第 7 天 / 第 30 天 的段位边界行为(数值侧)
  ③ 升温侧:正向事件累加 + 80+ 写时递减(与衰减耦合,不可分开验)
  ④ time-travel 回放:同一历史时刻,内存算 == 重载磁盘算(定数,零二次缩放)

设计原则(本仓风格):纯函数、自建断言、返回 (desc, passed)、main 聚合。
全部拿固定 epoch base,不读墙钟 —— 回放可重复。
"""
from __future__ import annotations

import os
import tempfile

from kb.relation.affinity import (
    LedgerEvent, decay_weight, compute_affinity, tier_index_of, tier_of,
    write_discount, priced_delta, ritual_gate, TIER_NAMES,
)
from kb.relation.params import AffinityParams

_SEC_PER_DAY = 24 * 3600.0
BASE = 1_800_000_000.0          # 固定 UTC epoch(2027-01-15 前后),不依赖墙钟


def _aff(delta, days, type_="共同经历"):
    """单笔事件在 T+days 的亲和(init=50 + delta×因子)。"""
    ev = [LedgerEvent("u", BASE, type_, "x", float(delta))]
    return compute_affinity(ev, as_of_time=BASE + days * _SEC_PER_DAY)


# ---------------------------------------------------------------------------
# ① 半衰期曲线:T+1/7/14/30/90 区间断言
# ---------------------------------------------------------------------------
def case_half_life_curve():
    """因子 = 0.5^(t/14)。断言各时刻因子落在 [下界, 上界] 且 T+14 恰好 0.5。"""
    p = AffinityParams()
    checks = [
        # (天, 期望因子, 容差)
        (0,  1.000000, 1e-9),
        (1,  0.5 ** (1 / 14),  1e-9),   # ≈0.951695
        (7,  0.5 ** (7 / 14),  1e-9),   # ≈0.707107 = 1/√2
        (14, 0.5,              1e-9),   # 硬锚:恰好一个半衰期
        (30, 0.5 ** (30 / 14), 1e-9),   # ≈0.226541
        (90, 0.5 ** (90 / 14), 1e-9),   # ≈0.011611
    ]
    ok = True
    detail = []
    for days, expect, tol in checks:
        got = decay_weight(1.0, days, p)
        passed = abs(got - expect) < tol
        ok = ok and passed
        detail.append("T+%d=%.6f%s" % (days, got, "" if passed else "!FAIL"))
    # T+14 恰好一半(硬锚,单独强调)
    anchor = abs(decay_weight(10.0, 14, p) - 5.0) < 1e-9
    return ("① 半衰期曲线 T+0/1/7/14/30/90 [%s] 硬锚T+14=0.5(%s)"
            % (" ".join(detail), "OK" if anchor else "FAIL"), ok and anchor)


def case_decay_monotonic_and_zero():
    """衰减单调递减;elapsed<=0 原值不衰(未发生的时间不衰)。"""
    p = AffinityParams()
    seq = [decay_weight(10.0, d, p) for d in [0, 1, 7, 14, 30, 90, 365]]
    mono = all(a >= b for a, b in zip(seq, seq[1:]))
    nonneg = all(v >= 0 for v in seq)
    # elapsed<=0 -> 原值
    zero = decay_weight(7.5, 0, p) == 7.5 and decay_weight(7.5, -3, p) == 7.5
    return ("② 衰减单调递减(%s) & 非负(%s) & elapsed<=0原值(%s)"
            % (mono, nonneg, zero), mono and nonneg and zero)


# ---------------------------------------------------------------------------
# ② 层切换边界:第 7 天 / 第 30 天(数值侧段位边界)
# ---------------------------------------------------------------------------
def case_tier_boundary_values():
    """段位边界值:20/50/80 的开闭。0-20冷淡 20-50熟人 50-80亲近 80+羁绊。"""
    p = AffinityParams()
    expect = [
        (19.9, 0, "冷淡"), (20.0, 1, "熟人"),
        (49.9, 1, "熟人"), (50.0, 2, "亲近"),
        (79.9, 2, "亲近"), (80.0, 3, "羁绊"),
        (100.0, 3, "羁绊"),
    ]
    ok = True
    for a, idx, name in expect:
        got_idx = tier_index_of(a, p)
        got_name = tier_of(a, p)
        if got_idx != idx or got_name != name:
            ok = False
    # 边界是「左闭」:>= 门槛即入该段
    left_closed = tier_index_of(20.0, p) == 1 and tier_index_of(50.0, p) == 2
    return ("③ 段位边界 20/50/80 开闭(%s) 左闭(%s)" % (ok, left_closed),
            ok and left_closed)


def case_tier_at_day7_day30():
    """第 7 / 30 天层切换边界:一笔跨 50 线的正事件,7/30 天均不跌回(init 不衰减)。

    语义钉死(防误判为 bug):aff = 50 + Σ(正项×因子),因子恒>0 ⇒ 正事件抬过 50 后
    衰减永不把它拉回 50 以下。「衰减到 floor=5」只在负向事件把总和压到 5 以下才生效,
    是 clamp 兜底,不是自然轨迹 —— 此处锁死该事实,防未来读者误以为是漏实现。
    """
    p = AffinityParams()
    ev = [LedgerEvent("u", BASE, "共同经历", "x", 10.0)]   # 50 -> 60
    a7 = compute_affinity(ev, as_of_time=BASE + 7 * _SEC_PER_DAY, params=p)
    a30 = compute_affinity(ev, as_of_time=BASE + 30 * _SEC_PER_DAY, params=p)
    # 期望值【硬编码】,不复刻实现(防『用实现验证实现』):
    #   7 天:50+10×0.70710678=57.071068 → round2 = 57.07
    #   30 天:50+10×0.22643092=52.264309 → round2 = 52.26
    # compute_affinity 出口 round(total,2),故按两位小数比。
    ok7 = abs(a7 - 57.07) < 1e-9
    ok30 = abs(a30 - 52.26) < 1e-9
    never_below_50 = a7 >= 50 and a30 >= 50
    # 负向事件才可能跌破 floor:一笔 -60 应被 clamp 到 floor=5
    ev_neg = [LedgerEvent("u", BASE, "情绪显著时刻", "x", -60.0)]
    floored = compute_affinity(ev_neg, as_of_time=BASE, params=p)
    floor_ok = abs(floored - 5.0) < 1e-9
    return ("④ 层边界 T+7(%.3f) T+30(%.3f) 不跌回50(%s) & 负向触floor=5(%s)"
            % (a7, a30, never_below_50, floor_ok),
            ok7 and ok30 and never_below_50 and floor_ok)


# ---------------------------------------------------------------------------
# ③ 升温侧:正向累加 + 80+ 写时递减(与衰减耦合)
# ---------------------------------------------------------------------------
def case_heating_and_diminishing():
    """升温侧:多笔正事件累加爬升;80+ 后写时递减(越亲密越难涨);负向不过折。"""
    p = AffinityParams()
    # 累加:3 笔 +3,几乎无衰减(T+0) -> 50 + 3*3 = 59
    ev = [LedgerEvent("u", BASE + i * 0.001, "承诺与约定", "x", 3.0) for i in range(3)]
    a = compute_affinity(ev, as_of_time=BASE + 1.0, params=p)
    add_ok = abs(a - 59.0) < 1e-6
    # 80+ 写时递减:write_discount 在 80 处=1.0,在 100 处=0.3
    d80 = write_discount(80.0, p)
    d100 = write_discount(100.0, p)
    d_mid = write_discount(90.0, p)
    disc_ok = (abs(d80 - 1.0) < 1e-9 and abs(d100 - 0.3) < 1e-9
               and 0.3 < d_mid < 1.0)
    # priced_delta:同一 +3 在 affinity<80 时原值,在 >=80 时被打折
    lo = priced_delta("承诺与约定", 3.0, 50.0, p)      # 不到 80 -> 3.0
    hi = priced_delta("承诺与约定", 3.0, 90.0, p)      # 90 -> 3.0×discount(90)
    price_ok = abs(lo - 3.0) < 1e-9 and hi < 3.0
    # 负向不过折:违约 -4 在 affinity=90 仍落 -4
    neg = priced_delta("承诺违约", -4.0, 90.0, p)
    neg_ok = abs(neg - (-4.0)) < 1e-9
    return ("⑤ 升温累加(50→%.1f) & 80+递减(80=%.2f,100=%.2f) & 负向不过折(%s)"
            % (a, d80, d100, neg_ok),
            add_ok and disc_ok and price_ok and neg_ok)


def case_clamp_weight_by_type():
    """按类型钳制:五类权重范围(宪法 §二.5)。越界建议一律夹回。"""
    p = AffinityParams()
    # 承诺与约定 限 [+3,+3]:建议 +10 应夹到 3
    c1 = priced_delta("承诺与约定", 10.0, 50.0, p) == 3.0
    # 承诺违约 限 [-4,-4]
    c2 = priced_delta("承诺违约", -1.0, 50.0, p) == -4.0
    # 玩家身份与偏好 限 [+1,+1]
    c3 = priced_delta("玩家身份与偏好", 5.0, 50.0, p) == 1.0
    # 共同经历 限 [+1,+2]
    c4 = priced_delta("共同经历", 9.0, 50.0, p) == 2.0
    return ("⑥ 类型钳制:承诺+3/%s 违约-4/%s 身份+1/%s 共历+2/%s"
            % (c1, c2, c3, c4), c1 and c2 and c3 and c4)


# ---------------------------------------------------------------------------
# ④ time-travel 回放一致性:内存算 == 重载磁盘算
# ---------------------------------------------------------------------------
def case_timetravel_replay_consistency():
    """同一历史时刻,内存算 与 重载磁盘再算 完全相等(定数,读时零二次缩放)。"""
    os.environ["KB_DATA_ROOT"] = tempfile.mkdtemp(prefix="decay_tt_")
    from kb.memory.service import open_user_memory

    p = AffinityParams()
    mem = open_user_memory("u_decay_tt", params=AffinityParams())
    for i in range(6):
        mem.record_event("共同经历", "共历%d" % i, 2.0, ts=BASE + 0.001 * i)

    probes = [BASE + 0.5, BASE + 7 * _SEC_PER_DAY,
              BASE + 14 * _SEC_PER_DAY, BASE + 30 * _SEC_PER_DAY]
    mem_vals = [mem.affinity_at(t) for t in probes]
    reloaded = open_user_memory("u_decay_tt", params=AffinityParams())
    disk_vals = [reloaded.affinity_at(t) for t in probes]
    consistent = all(abs(a - b) < 1e-9 for a, b in zip(mem_vals, disk_vals))
    # 未来事件不计:回放到写入前一刻,事件不参与
    before = reloaded.affinity_at(BASE - 100.0)
    future_ok = abs(before - 50.0) < 1e-9     # 只有 init 基线
    return ("⑦ time-travel 回放一致:内存==磁盘(%s) 四探测点=%s & 未来不计(%s)"
            % (consistent, ["%.2f" % v for v in mem_vals], future_ok),
            consistent and future_ok)


def case_ritual_gate():
    """跨段仪式门槛:数值到位但 ritual 未完成 → 按住不升(§五)。"""
    p = AffinityParams()
    # 数值 85(羁绊段 idx=3),ritual 未完成 -> 被按住到 idx=2(亲近)
    blocked = ritual_gate(85.0, current_tier_idx=2, ritual_completed=False, params=p)
    gate_ok = (blocked.blocked_by_ritual and blocked.allowed_tier_idx == 2
               and blocked.allowed_tier == "亲近")
    # ritual 完成 -> 放行到 idx=3
    passed = ritual_gate(85.0, current_tier_idx=2, ritual_completed=True, params=p)
    pass_ok = (not passed.blocked_by_ritual and passed.allowed_tier_idx == 3)
    # 信号包是 {affinity, tier, ritual_blocked} 而非裸数字
    sig = blocked.to_signal()
    sig_ok = set(sig.keys()) == {"affinity", "tier", "ritual_blocked"}
    return ("⑧ 仪式门槛:85分未仪式被按到「%s」(%s) & 已仪式放行(%s) & 信号包(%s)"
            % (blocked.allowed_tier, gate_ok, pass_ok, sig_ok),
            gate_ok and pass_ok and sig_ok)


def main() -> int:
    print("\n==== 关系衰减 纯函数单测矩阵(离线,零 API) ====")
    cases = [
        case_half_life_curve,
        case_decay_monotonic_and_zero,
        case_tier_boundary_values,
        case_tier_at_day7_day30,
        case_heating_and_diminishing,
        case_clamp_weight_by_type,
        case_timetravel_replay_consistency,
        case_ritual_gate,
    ]
    ok_all = True
    for fn in cases:
        desc, passed = fn()
        print(("  PASS " if passed else "  FAIL ") + desc)
        ok_all = ok_all and passed
    if not ok_all:
        print("\n存在失败 —— 衰减纯函数不达标,请修。")
        return 1
    print("\n全部通过:半衰期曲线 / 层边界 / 升温侧 / time-travel 回放 四项成立。")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
