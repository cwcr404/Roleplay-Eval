# coding: utf-8
"""kb.memory —— 记忆层(跨会话持久)。

承载:user_id 隔离的 L1 事件账本(append-only)、L2 蒸馏缓存、好感度读取输出口
(time-travel 回放)。

用法(单个用户一条链):
    m = open_user_memory("user_chixu")      # 装载隔离的账本+缓存
    m.record_event("共同经历", "玩家与芽衣闯过量子之海", 2.0)   # 书记员产出后写入
    m.affinity_now()                        # 好感度(当下)
    m.affinity_at(ts)                       # time-travel 回放
    m.signal()                              # {affinity, tier, ...} 信号包

注意:本模块是关系/记忆的实现主体,遵守 memory_architecture.md —— 数值来自 relation.params,
写入只追加、读取时算好感度(禁写回),L2 只作缓存可从头重建。
"""
from __future__ import annotations

from typing import Callable, Optional

from .ledger import Ledger, open_user_ledger, LedgerEvent
from .distill import (
    L2Profile,
    load_l2,
    rebuild_l2_from_ledger,
    delete_l2,
)  # 注意:distill 在 __init__ 里已被当函数导出,故不走 `import distill as _`
from ..relation.affinity import compute_affinity, ritual_gate, tier_of, tier_index_of, TierResult
from ..relation.params import (AffinityParams, PROMISE_SETTLE_TYPES)


class UserMemory:
    """某 user 的完整记忆+关系入口(隔离:一个 user_id 一条链)。"""

    def __init__(self, user_id: str, params: Optional[AffinityParams] = None):
        self.user_id = user_id
        self.params = params or AffinityParams()
        # 定价参数单一事实源:写入(append 判 80+ 折扣)与读取(compute_affinity)
        # 必须共用同一份 params —— 整套链路只认 self.params,绝不默认两套打架。
        self.ledger: Ledger = open_user_ledger(user_id, params=self.params)

    # ---- 写:L1 追加(append-only) ----
    def record_event(self, etype: str, content: str,
                     weight_delta: float, ts=None) -> Optional[LedgerEvent]:
        """写入一条新事件(经钳制、落盘 append-only)。ts 默认当下。

        单一写口 + 路由:若 etype ∈ 承诺兑现/违约,这是【结算】不是新增长事件 ——
        改走 ledger.settle_promise(content_ref=content),把结算事件按 settled: 协议
        追加、并把被结算的那条原型承诺从 open_promises() 活跃集自动摘除(查询派生)。
        content 在此被当作『定位原承诺的引用片段』(全文或子串皆可);找不到可结算
        的 open 承诺则本笔不落(返回 None),宁缺毋滥、不记悬空结算。
        """
        if etype in PROMISE_SETTLE_TYPES:
            return self.ledger.settle_promise(outcome_type=etype, content_ref=content,
                                              ts=ts)
        return self.ledger.append(etype, content, weight_delta, ts=ts)

    # ---- 读:好感度(time-travel 地基,读取时算,绝写回) ----
    def affinity_now(self) -> float:
        return compute_affinity(self.ledger.events, params=self.params,
                                user_id=self.user_id)

    def affinity_at(self, as_of_time) -> float:
        """回放任意历史时间点的好感度快照(验收标准 2)。"""
        return compute_affinity(self.ledger.events, as_of_time=float(as_of_time),
                                params=self.params, user_id=self.user_id)

    # ---- 读:升段门槛(数值到位 vs ritual) ----
    def gate(self, ritual_completed: bool = False,
             as_of_time=None) -> TierResult:
        aff = (self.affinity_at(as_of_time) if as_of_time is not None
               else self.affinity_now())
        cur = tier_index_of(aff, self.params)
        return ritual_gate(aff, cur, ritual_completed=ritual_completed,
                           params=self.params)

    # ---- 信号包(外挂,不进推理路径) ----
    def signal(self, ritual_completed: bool = False) -> dict:
        g = self.gate(ritual_completed=ritual_completed)
        return {
            "affinity": g.affinity,
            "tier": g.allowed_tier,
            "ritual_blocked": g.blocked_by_ritual,
        }

    # ---- L2 蒸馏缓存(服务层出口) ----
    def l2(self, distill_fn: Optional[Callable[[str], str]] = None) -> Optional[L2Profile]:
        """取 L2 故事层。缓存缺 / 过期(有新事件在其后写入)则重建。

        新鲜度策略:规则回退重建零 LLM 成本,故只要发现缓存落后于账本就现场重刷;
        真正调 LLM 的深度蒸馏仍按 cadence(每 N 轮/显式 rebuild_l2)控制 —— 这里
        的自动重刷总是用确定性规则回退,保证 demo 里故事层立刻跟上新事件、又不烧钱。

        【守护真画像(0908 校准捉到的污染)】规则回退重建只在 无缓存 / 或现有缓存本就是
        规则重建产物 时才现场执行。若现有缓存是【真实 LLM 蒸馏产物】(distilled_events_seen>0,
        status ok/budget_truncated),即使它因新事件而显“过期”,读路径也**绝不**用确定性规则
        文本去覆盖它 —— 宁让它旧一拍(注入看到晚一点的画像),等引擎 cadence(常规每10轮/
        强制/特例)的真实深度蒸馏去追平。原因:规则重建是伪 L2(退化的类型拼接,非叙事),
        一旦盖掉真实 LLM 画像是污染事实源;这与‘降级不得冒充真实产物’同源 —— 方向相反但同一
        条底线:真实产物不能被确定性回退顶掉。
        """
        prof = load_l2(self.user_id)
        if prof is not None and self._l2_fresh(prof):
            return prof
        # 缓存过期:读路径不造假 —— 真蒸馏缓存保底不动
        if prof is not None and prof.distilled_events_seen > 0:
            return prof
        # 缺缓存 / 现有缓存本就是规则重建(非真蒸馏):现场确定性重刷(容灾/demo 出口)
        return rebuild_l2_from_ledger(self.user_id, self.ledger.events, distill_fn=distill_fn)

    def _l2_fresh(self, prof: L2Profile) -> bool:
        """缓存是否已覆盖账本里全部事件。

        用『蒸馏时见过的事件数』比时间戳稳 —— 时间戳受引擎时钟与真实时钟
        混用影响(曾因此把空缓存误判为新鲜)。
        """
        return self.ledger.size() <= prof.events_seen


    # ---- L3 注入路径(找用分离:找在本地零 LLM) ----
    def _l3_store(self):
        """懒加载 L3 store(隔离由 user_id 保证)。"""
        if not hasattr(self, "_l3_store_cache"):
            from .l3_store import L3Store
            self._l3_store_cache = L3Store(self.user_id)
        return self._l3_store_cache

    def l3_hits(self, query: str, *, k: int = 3, now=None,
                evoke: bool = False):
        """找:本地词法检索 L3 → list[Hit](含命中媒介/是否降级)。

        evoke=True 时对命中的条目唤起(被动应答,不耗主动配额)。
        零 LLM:检索全在本地词法层完成。
        """
        from .bundle import Hit
        import time as _t
        now = now if now is not None else _t.time()
        store = self._l3_store()
        hits = []
        for it, cnt in store.retrieve(query, k=k, now=now):
            faded = it.in_refractory(now)
            if evoke and not faded:
                store.evoke(it.id, now=now, active=False)   # 被动:不耗配额
            hits.append(Hit(item=it, medium=f"命中{cnt}词", faded=faded))
        return hits

    def injection(self, query: str = "", *, k: int = 3, now=None) -> str:
        """注入出口(与 l2() 同出口):关系块 + 记忆块。

        返回可注入文本;结构化状态(degraded/status)不进文本。
        """
        from .bundle import build_bundle
        hits = self.l3_hits(query, k=k, now=now) if query else []
        return build_bundle(self.l2(), hits).text


    def rebuild_l2(self, distill_fn: Optional[Callable[[str], str]] = None) -> L2Profile:
        """删除后重建 L2(容灾验收标准 3)。"""
        delete_l2(self.user_id)
        return rebuild_l2_from_ledger(self.user_id, self.ledger.events,
                                      distill_fn=distill_fn)

    def delete_l2_cache(self) -> bool:
        return delete_l2(self.user_id)


def open_user_memory(user_id: str, params: Optional[AffinityParams] = None) -> UserMemory:
    """一键为用户搭记忆链路(账本自动加载)。隔离由 user_id 保证。"""
    return UserMemory(user_id, params=params)
