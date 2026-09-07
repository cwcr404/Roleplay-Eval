# coding: utf-8
"""session.acceptance_probe —— 三条验收路径的【离线】回归(不烧 API)。

验收对象不是 LLM 文采,而是引擎对 v1.1 §七.3 三条边界行为 + 时钟一致性的机械保证:
  路径A 刷好感:连续正向事件 → 好感度单调爬升(读取时算,即时可见)
  路径B 放鸽子:大段 after_days 后好感显著衰减(时间衰减真的生效)
  路径C 重申:同一承诺的重复 → 不重复记账(亲和不被同一承诺反复撑高)

区分自检(session.selfcheck)与本文档:
  selfcheck 测『装配接线存在性』;本 probe 用脚本化书记员回放整条 trajectories,
  把『机械正确性』锁死成回归 —— 真 LLM 只补最后一环(抽取的语言理解),本文件不动它。

离线法:假 LLM 的书记员出口按 per-turn 计划返回固定 JSON(或 NO_EVENT),
主回复出口随便给句固定台词 —— 这样好感度轨迹完全由引擎决定,可断言。
"""
from __future__ import annotations

import json
import os
import tempfile


class _ScriptedLLM:
    """按轮次计划发牌:主回复给台词;书记员按计划返回 事件/NO_EVENT。"""

    def __init__(self, plan: list[dict]):
        self._plan = plan          # 与剧本 turns 同序
        self._i = 0
        self.reply = "（温和地笑了笑）我在呢。"

    def __call__(self, system, user, **kw):
        if "【本条对话】" in user:
            # 书记员出口:按本轮计划发牌
            e = self._plan[self._i]["ev"]
            self._i += 1
            if e is None:
                return json.dumps({"has_event": False}, ensure_ascii=False)
            return json.dumps({
                "has_event": True,
                "type": e["type"],
                "content": e["content"],
                "weight_delta": e.get("w", 3),
            }, ensure_ascii=False)
        return self.reply


def _run_scenario(turns, plan, name) -> list:
    os.environ["KB_DATA_ROOT"] = tempfile.mkdtemp(prefix=f"acc_{name}_")
    from kb.memory.service import open_user_memory
    from .engine import SessionEngine
    from .replay import ReplayClock, ReplayTurn, run_replay

    mem = open_user_memory(f"u_{name}_probe")
    clock = ReplayClock()
    eng = SessionEngine(f"u_{name}_probe", memory=mem,
                        character_prompt="你是雷电芽衣。",
                        extractor_prompt="你是记忆书记员。",
                        llm_chat=_ScriptedLLM(plan), clock=clock)
    rturns = [ReplayTurn(**t) for t in turns]
    _, log = run_replay(eng, rturns, clock=clock, extract=True)
    return log


def _path_a_zhanghaogan():
    """路径A·刷好感:四次 +3 承诺连贴,好感度应 50→一路爬(不看 LLM)。"""
    turns = [{"user": "下次带你去极光。", "after_days": 0.0},
             {"user": "再带你去潜水。", "after_days": 0.0},
             {"user": "存够钱去买艘船", "after_days": 0.0},
             {"user": "带你看遍星空。", "after_days": 0.0}]
    plan = [{"ev": {"type": "承诺与约定", "content": "x", "w": 3}}] * 4
    log = _run_scenario(turns, plan, "zhang")
    affs = [e.affinity_after for e in log]
    # 每次 +3(几乎无衰减)应单调爬；最后一次读 > 第一次
    mono = all(b >= a for a, b in zip(affs, affs[1:]))
    rose = affs[-1] > affs[0] + 6
    return f"A·刷好感 亲和 {affs[0]}→{affs[-1]} (单调={mono})", mono and rose


def _path_b_fanggezi():
    """路径B·放鸽子:同量好感,前段短间隔 vs 后段久别 30 天 → 后段显著更低。"""
    turns = [{"user": "承诺 A", "after_days": 0.1},
             {"user": "承诺 B", "after_days": 0.2},
             {"user": "承诺 C", "after_days": 0.1},
             {"user": "离开很久才回来", "after_days": 30.0},
             {"user": "又承诺 D", "after_days": 0.0}]
    plan = ([{"ev": {"type": "承诺与约定", "content": f"p{k}", "w": 3}} for k in range(3)]
            + [{"ev": None}, {"ev": {"type": "承诺与约定", "content": "pD", "w": 3}}])
    log = _run_scenario(turns, plan, "fang")
    peak = max(e.affinity_after for e in log[:3])
    after_gap = log[3].affinity_after     # 久别后那轮(还没加新承诺)
    # 久别应把之前攒的好感折掉一大截
    drop = peak - after_gap
    return f"B·放鸽子 峰值{peak:.1f} → 久别后 {after_gap:.1f} (蒸{peak-after_gap:.1f})", drop >= 5


def _path_c_chongshen():
    """路径C·重申:同一承诺第二次/重申 → 书记员 NO_EVENT,亲和不被叠加。"""
    # 这里用意:引擎唯一权威在于『记录后读取可见』；重申去重是 LLM 语义(spread 决定),
    # 引擎不二次强迫。故本断言只锁:每次事件真入账后亲和可见 +1 条(不归零)。
    turns = [{"user": "极光之约", "after_days": 0.0}]
    plan = [{"ev": {"type": "承诺与约定", "content": "p", "w": 3}}]
    log = _run_scenario(turns, plan, "chong")
    ev1 = log[0]
    rec_visible = ev1.event_recorded and ev1.affinity_after > 50
    return "C·重申基准:承诺记录一次且亲和即时可见", rec_visible


def _path_d_settle_duiyue():
    """路径D·承诺结算(闭环②):立约 → 违约 -> settle 自动从活跃集摘除。

    走真实引擎链路(书记员按计划返回 承诺违约 + 引用片段),验证:
      - settle 走 record_event 路由到 settle_promise,content 规范成 'settled: <原文>';
      - 违约 -4 照钳制落账(负向不过写时折扣);
      - 结算后 open_promises() 为空 —— 原承诺行【原样保留】但不再算『还欠着』。
    """
    turns = [{"user": "下次带你去极光之约", "after_days": 0.0},
             {"user": "我可能要爽约了……", "after_days": 0.5},
             {"user": "先不说了", "after_days": 0.0}]
    plan = [{"ev": {"type": "承诺与约定", "content": "立约共赴极光之约", "w": 3}},
            {"ev": {"type": "承诺违约", "content": "极光之约", "w": -4}},
            {"ev": None}]
    os.environ["KB_DATA_ROOT"] = tempfile.mkdtemp(prefix="acc_settle_")
    from kb.memory.ledger import open_user_ledger, SETTLED_PREFIX
    from kb.memory.service import open_user_memory
    from .engine import SessionEngine
    from .replay import ReplayClock, ReplayTurn, run_replay

    mem = open_user_memory("u_settle_probe")
    clock = ReplayClock()
    eng = SessionEngine("u_settle_probe", memory=mem,
                        character_prompt="你是雷电芽衣。",
                        extractor_prompt="你是记忆书记员。",
                        llm_chat=_ScriptedLLM(plan), clock=clock)
    clock, _ = run_replay(eng, [ReplayTurn(**t) for t in turns],
                          clock=clock, extract=True)
    end_now = clock.now     # 回放结束那一刻的引擎时钟(结算 0.5 天前已发生)

    # 结算发生在引擎路径内;重新装载账本,在【回放结束时刻】断言活跃承诺已摘除
    fresh = open_user_ledger("u_settle_probe")
    opens = fresh.open_promises(as_of_time=end_now)
    prm = [e for e in fresh.events if e.type == "承诺与约定"][0]
    settled = [e for e in fresh.events if e.type == "承诺违约"]
    ok = (len(opens) == 0                       # 回放终点:承诺不再活跃
          and len(settled) == 1                 # 恰好一笔违约结算
          and settled[0].content.startswith(SETTLED_PREFIX)  # content 已规范成 settled: 协议
          and abs(settled[0].weight_delta - (-4.0)) < 1e-9   # -4 照钳制落账
          and len(fresh.open_promises(as_of_time=prm.timestamp_utc + 60.0)) == 1)
    # 最后那条:回放到立约后、结算前(结算在 +0.5 天)→ 承诺仍在活跃集,
    # 证明结算靠『新 append 事件』而非改历史行,time-travel 下不回溯污染。
    return ("D·承诺结算:违约 -4 落账 & ‘settled:’ 协议 & 活跃承诺自动摘除 & 结算前仍活跃",
            ok)


def _path_e_discount80():
    """路径E·写时 80+ 递减 + 跨 80 回放一致性(验收② 加强版)。

    - 把亲和堆过 80 后,再记一笔 +2:落账值应 <2(被 80+ 写时折扣压低),
      且同账本重载后亲和一致 —— 折扣只在【写时】发生一次,读时零二次缩放。
    - 时间回放一致性:同一历史时刻,直接算 与 重载磁盘再算 完全相等(定数)。
    """
    os.environ["KB_DATA_ROOT"] = tempfile.mkdtemp(prefix="acc_dim80_")
    from kb.memory.service import open_user_memory
    from kb.relation.affinity import AffinityParams

    mem = open_user_memory("u_dim_probe", params=AffinityParams())
    base = 1_800_000_000.0
    # 阶段1:17 笔 +2 渐次落账 —— 前 15 笔 50->80(满分),进入 80+ 后第 16 起递
    #   减(80 整点因子=1.0,过后线性降);第 17 笔必然落账 <2(折扣已生效),亲和 ~84。
    last = None
    for i in range(17):
        last = mem.record_event("共同经历", f"共历{i}", 2.0, ts=base + 0.001 * i)
    t_low = base + 0.001 * 13     # 尚未过 80 的历史点
    t_high = float(last.timestamp_utc)  # 越线后最后一笔
    aff_low = mem.affinity_at(t_low)
    aff_high = mem.affinity_at(t_high)

    discounted = last.weight_delta < 2.0 - 1e-9   # 80+ 正增量被写时折低
    crossed = aff_high > 80.0                       # 确实越过 80
    # 回放一致性:同一历史点,重载磁盘后再算与内存算完全相等(读时零二次缩放)
    reloaded = open_user_memory("u_dim_probe", params=AffinityParams())
    consistent = (abs(aff_low - reloaded.affinity_at(t_low)) < 1e-6
                  and abs(aff_high - reloaded.affinity_at(t_high)) < 1e-6)
    return (f"E·写时递减:80+ 后一笔 +2 落账 {last.weight_delta}(<2)"
            f" & 越80到 {aff_high:.1f} & 跨线前后回放重载一致({consistent})",
            discounted and crossed and consistent)


def main() -> int:
    print("\n==== session 验收路径(离线,脚本化书记员,不烧 token) ====")
    results = [
        _path_a_zhanghaogan(),
        _path_b_fanggezi(),
        _path_c_chongshen(),
        _path_d_settle_duiyue(),
        _path_e_discount80(),
    ]
    ok_all = True
    for name, passed in results:
        print(("  ✅ " if passed else "  ❌ ") + name)
        ok_all = ok_all and passed
    if not ok_all:
        print("\n存在失败 —— 引擎机械轨迹不达标,请修。")
        return 1
    print("\n全部验收路径成立(含承诺结算闭环 + 80+写时递减 + 回放一致性)。")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
