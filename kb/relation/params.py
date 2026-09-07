# coding: utf-8
"""关系/记忆 可调运营参数 —— 单一事实源(single source of truth)。

文档:docs/memory_architecture.md
- §七.4 要求:半衰期、五类权重增量、80+ 递减斜率全部留成「可调参数」,
  等真实测试数据回流后校准。首版值为运营拍板占位,不是定论。
- 所有实现只 import 这份参数,禁止在逻辑里硬编码数值。
- 这是「模型只有建议权/芽衣只有读取权,定价权在运营参数」的落点。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

# ---------------------------------------------------------------------------
# 权重钳制范围(文档 §二.5 第4条 —— 「模型只能建议,不能定价」)
# weight_delta 建议值超出 [min, max] 一律夹回边界。
# ---------------------------------------------------------------------------
WEIGHT_CLAMP: dict[str, tuple[float, float]] = {
    "玩家身份与偏好": (1.0, 1.0),    # +1,遇到才记,不允许更高/更低
    "共同经历": (1.0, 2.0),          # +1~2,剧情级取 2
    "情绪显著时刻": (-3.0, 3.0),     # ±1~3,负面可为负
    "承诺与约定": (3.0, 3.0),        # +3,上限封顶
    "显式记忆指令": (2.0, 2.0),      # +2,无条件
    # 承诺生命周期结算(补承诺的「结局」记账——各记一笔,不改原承诺历史行):
    "承诺兑现": (2.0, 2.0),          # +2,信任的复利(结算事件;s.引用原承诺,原行不动)
    "承诺违约": (-4.0, -4.0),        # -4,必须 > +3 的溢价(许诺到鸽之间好感虚高,
                                     #   破约的伤不是钱是信任 —— 惩罚要正方向赔)
}
# 五类事件 + 承诺生命周期结算事件;校验 type 不在其中即视为非法(拒收)
VALID_EVENT_TYPES: tuple[str, ...] = tuple(WEIGHT_CLAMP.keys())

# ---- 承诺生命周期:结算事件类型 + 活跃承诺的单一事实源 ------------------
# 兑现/违约不是去标某行 resolved(那会改历史、污染 time-travel),而是照定价表
# 各记一笔结算事件,其 content = 'settled: ' + 原承诺全文(见 ledger.SETTLED_PREFIX);
# 承诺是否活跃由 open_promises() 从事件流查询聚合派生 —— 状态是查询的派生,
# 改状态 = 追加事件,永远不改历史行。这是记史的完全体。
PROMISE_SETTLE_TYPES: frozenset[str] = frozenset({"承诺兑现", "承诺违约"})
PROMISE_TYPE: str = "承诺与约定"

# ---- 80+ 边际递减(写时生效,唯一实现;docs §五) ----------------------------
# 折扣形状:aff < 80 不动;80 → 100 线性从 1.0 降到 0.3;≥100 固定 0.3。
# 只作用于【正】增量(增益越亲密越难涨);负向(惩罚)不过折 —— 你总能hurt,
# -4 违约在任何段都落在账上,符合「痛不因亲密而减免」的情绪语义。
DIMINISH_LOW: float = 1.0            # aff<80 的折扣 = 1(无递减)
DIMINISH_FLOOR_AT_CEIL: float = 0.3  # aff>=100 固定折扣(涨不堵死,靠衰减回开)


@dataclass
class AffinityParams:
    """运营参数(全部可调)。semitone 常量 = 初始拍板,待真实数据校准。"""

    # --- 量纲(文档 §五) ---
    min_affinity: float = 0.0      # 量纲下界
    max_affinity: float = 100.0    # 量纲上界(读取封顶;边际靠衰减回开,永不堵死)
    init_affinity: float = 50.0    # 冷启动/新用户初始好感
    floor: float = 5.0             # floor=5:衰减再狠不破 5(人格由角色卡兜底)

    # --- 关系衰减(文档 §四) ---
    half_life_days: float = 14.0   # 单一指数半衰期(天),初始拍板,待校准
    # 各段位阈值(文档 §五):冷淡/熟人/亲近/羁绊
    tier_breakpoints: tuple[float, float, float] = (20.0, 50.0, 80.0)
    # 80+ 边际递减(写时生效):aff>=此值后新【正】事件有效增量被折扣
    diminishing_threshold: float = 80.0
    diminishing_floor: float = DIMINISH_FLOOR_AT_CEIL  # 无量纲上折扣下限(≥80 到 max 线性降到此)

    # --- 升段仪式(文档 §五) ---
    ritual_required_tiers: tuple[int, ...] = (3,)  # 段位索引>=3(亲近->羁绊,索引从0:冷淡0/熟人1/亲近2/羁绊3)


def clamp_weight(type_: str, suggested: float) -> float:
    """把书记员建议的 weight_delta 夹回该类型的钳制范围(价格权限,文档 §二.5 第4条)。

    这是【价格权限】,只管把这个类型压到它的定价区间;80+ 递减是另一层(边际
    效应,写时生效),在 clamp 之后、落账之前由记账层套上。
    """
    lo, hi = WEIGHT_CLAMP[type_]
    if suggested < lo:
        return lo
    if suggested > hi:
        return hi
    return suggested
