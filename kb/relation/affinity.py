# coding: utf-8
"""好感度纯计算核心 —— 全部是纯函数,无副作用,读取时算、禁止写回。

文档:
- 衰减:docs/memory_architecture.md §四。事件权重 = weight_delta × 0.5^(经过天数/14)。
- 量纲/升段/仪式:§五。0-100、初始50、冷淡20/熟人50/亲近80/羁绊、floor=5、
  跨段需 ritual。
- time-travel 地基:好感度 = f(事件流, 当前时间戳),给任意 as_of_time 可回放。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from .params import AffinityParams

# 秒 -> 天
_SEC_PER_DAY = 24 * 3600.0


def _utc_ts(dt: Optional[datetime]) -> Optional[float]:
    """把日期/时间戳规整成可比较的 UTC 秒(epoch)。"""
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return float(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


@dataclass(frozen=True)
class LedgerEvent:
    """L1 账本的一行(与文档行格式对齐)。

    行格式:user_id | timestamp_utc | type | content | weight_delta
    不含任何可变状态字段 —— 承诺的 open/resolved 是从事件流【查询聚合】派生的
    (见 ledger.open_promises),永不写回历史行。这就是记史的完全体:
    状态是查询的派生,改状态 = 追加事件,不改历史。
    """
    user_id: str
    timestamp_utc: float      # UTC epoch 秒(写入时统一在此转)
    type: str                 # 合法事件类型之一(见 VALID_EVENT_TYPES)
    content: str              # 第三人称描述性(禁止我/你指代);结算事件带 settled: 前缀引用原承诺
    weight_delta: float       # 已钳制+已写时打折(落账定值,读取不再二次打折)

    def to_row(self) -> str:
        # 用 | 分隔,user_id/content 禁含 '|'(写入时清洗)
        # ts 段存 str(float) 的原生无损表示(Python3 repr 往返恒等),而非 ISO 微秒
        # 串 —— ISO 只到微秒(6位),会把高精度 float 截断/舍入成不同值,导致同一
        # 结算事件在『内存查询』与『磁盘重载再查』边界(ts<=now)不一致(见 D 验收)。
        # 旧 ISO 行仍能读:解析侧做了兼容(先试 float,失败再 fromisoformat)。
        ts = str(self.timestamp_utc)
        return "|".join([
            self.user_id,
            ts,
            self.type,
            self.content.replace("|", " "),
            f"{self.weight_delta:g}",
        ])


def decay_weight(delta: float, elapsed_days: float, params: AffinityParams | None = None) -> float:
    """把一次事件在读取时刻(距写入 elapsed_days)折算成当前贡献。

    公式:effective = weight_delta × 0.5^(elapsed_days / half_life_days)
    elapsed_days <= 0 => 原值(未衰减)。
    """
    p = params or AffinityParams()
    if elapsed_days <= 0:
        return float(delta)
    return float(delta) * (0.5 ** (elapsed_days / p.half_life_days))


def compute_affinity(events: Sequence[LedgerEvent],
                     as_of_time: Optional[float] = None,
                     params: AffinityParams | None = None,
                     user_id: Optional[str] = None) -> float:
    """好感度 = 全部事件(截至 as_of_time)加权求和,再 clamp 到量纲 + floor。

    - as_of_time=None => 当下(全部事件按当前时间衰减)。
    - as_of_time 传任意历史 UTC => 回放那个时刻的好感度快照(time-travel)。
    - user_id 传入时只累计该用户事件(隔离);为 None 则假设 events 已过滤好。
    - 上限:affinity 本身 clamp 到 [floor, max]。边际 80+ 递减在【写入时】生效
      (见 priced_delta),读取这里不做任何二次缩放 —— 账本里存的是定值。

    注意:这就是文档说的「读取时计算,禁止写回」。绝不把结果写进 any 持久账本。
    """
    p = params or AffinityParams()
    now = as_of_time if as_of_time is not None else _utc_ts(datetime.now(timezone.utc))
    total = p.init_affinity
    for ev in events:
        if user_id is not None and ev.user_id != user_id:
            continue
        if ev.timestamp_utc > now:      # 未来事件(回放到过去时)不计
            continue
        eff = decay_weight(ev.weight_delta, (now - ev.timestamp_utc) / _SEC_PER_DAY, p)
        total += eff
    # 上下界收敛:floor 兜底(fresh start 也在 floor 之上),max 封顶读取量纲。
    # (注意:这里【没有】任何 80+ 边际递减 —— 那已挪到写时生效。账本里的数就是
    #  事件当时打折后的有效分量,读取只做衰减求和 = 纯时间之手的减法。
    #  保证回放一致性:同一时刻读到的是定数,不依赖读取累计总量。)
    total = min(total, p.max_affinity)
    total = max(total, p.floor)
    return round(total, 2)


def write_discount(affinity: float, params: AffinityParams | None = None) -> float:
    """80+ 边际折扣(写时生效,唯一实现 —— docs §五、§二.5 定价表)。

    折扣曲线:aff < threshold 不动(=1.0);threshold → max 线性从 1.0 降到
    diminishing_floor(=0.3);>= max 固定 0.3。用它乘以【正】增量后再落账,
    负向(惩罚)不过折。

    为什么写时:落进账本的 weight 是「这件事当时打过分后的有效分量」= 历史事实,
    time-travel 回放不因打折规则将来改动而改写历史;读取时纯求和,读多少都是定数。
    """
    p = params or AffinityParams()
    aff = float(affinity)
    if aff < p.diminishing_threshold:
        return 1.0
    span = p.max_affinity - p.diminishing_threshold
    if span <= 0:
        return p.diminishing_floor
    t = (aff - p.diminishing_threshold) / span
    t = max(0.0, min(1.0, t))          # 钳到 0..1
    return 1.0 - (1.0 - p.diminishing_floor) * t   # 1.0 -> 0.3


def priced_delta(type_: str, suggested: float, affinity: float,
                 params: AffinityParams | None = None) -> float:
    """记账层包装:价格权限(clamp) + 正增量写时递减(80+),产出最终落账 weight。

    步骤:1) 按类型夹回定价区间;2) 若是正增量,套 80+ 写时折扣;
          3) 负向(含 -4 违约)不过折 —— 痛不因亲密而减免。
    结果就是写进账本那行的有效分量(此后纯阅读,不再二次打折)。
    """
    from .params import clamp_weight
    w = clamp_weight(type_, float(suggested))
    if w > 0 and affinity >= (params or AffinityParams()).diminishing_threshold:
        w *= write_discount(affinity, params)
    return round(w, 4)


def tier_index_of(affinity: float, params: AffinityParams | None = None) -> int:
    """返回段位索引:0冷淡 / 1熟人 / 2亲近 / 3羁绊(由 breakpoints 决定)。"""
    p = params or AffinityParams()
    idx = 0
    for bp in p.tier_breakpoints:
        if affinity >= bp:
            idx += 1
    return idx


def tier_of(affinity: float, params: AffinityParams | None = None) -> str:
    """返回段位名。"""
    return TIER_NAMES[tier_index_of(affinity, params)]


TIER_NAMES: tuple[str, ...] = ("冷淡", "熟人", "亲近", "羁绊")


@dataclass(frozen=True)
class TierResult:
    """一次升段判定的结果。"""
    affinity: float
    current_tier_idx: int
    current_tier: str
    target_reached: bool        # 数值已够冲到下一段吗?
    ritual_done: bool           # 用户账本 ritual_completed 标记是否为真
    allowed_tier_idx: int       # 系统实际放行的段位(受 ritual 门槛约束)
    allowed_tier: str
    blocked_by_ritual: bool     # 数值够但 ritual 未完成(=被门槛拦下)

    def to_signal(self) -> dict:
        return {
            "affinity": self.affinity,
            "tier": self.allowed_tier,     # 放行段位
            "ritual_blocked": self.blocked_by_ritual,
        }


def ritual_gate(affinity: float,
                current_tier_idx: int,
                ritual_completed: bool = False,
                params: AffinityParams | None = None) -> TierResult:
    """跨段门槛判定(数值到位只是资格,仪式完成才升段 —— 文档 §五)。

    - 段位只增不放行(不会因 affinity 跌回从羁绊掉到冷淡 —— tier 是身份层,
      由角色卡/故事兜底;此处只做「最多能到哪段」)。
    - 数值已达下一段 breakpoint,且该跨段需要 ritual 而 ritual 未完成
      => 允许段位停留在当前段(即便数值已超)。
    - 数值未跨段 => 无门槛问题,放行当前段。
    """
    p = params or AffinityParams()
    idx = tier_index_of(affinity, p)
    ritual_blocked = False
    if ritual_completed:
        allowed = idx
    else:
        allowed = idx
        # 若想升到的段位在 ritual_required_tiers 里,且 ritual 未完成 -> 按住不升
        if idx in p.ritual_required_tiers:
            # 停在同一段的下一个允许段 = 当前 idx-1(未做仪式,只到上一级)
            allowed = idx - 1
            ritual_blocked = True
    return TierResult(
        affinity=affinity,
        current_tier_idx=current_tier_idx,
        current_tier=TIER_NAMES[current_tier_idx],
        target_reached=(idx > current_tier_idx),
        ritual_done=ritual_completed,
        allowed_tier_idx=allowed,
        allowed_tier=TIER_NAMES[allowed],
        blocked_by_ritual=ritual_blocked,
    )


def build_signal(result: TierResult) -> dict:
    """导出给三 Agent 的信号包(不进推理路径,只作外挂上下文)。"""
    return {
        "affinity": result.affinity,
        "tier": result.allowed_tier,
        "ritual_blocked": result.blocked_by_ritual,
    }
