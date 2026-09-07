# coding: utf-8
"""kb.relation —— 关系层(温度层)。

承载:好感度纯计算(指数半衰期衰减)、段位判定、ritual 升段门槛、信号包导出。
原则:**读取时计算,禁止写回**(time-travel 地基);关系层外挂,不进推理路径。
文档:docs/memory_architecture.md §四 / §五。

一切数值来自 kb.relation.params,本包不含硬编码运营值。
"""
from .params import (
    AffinityParams,
    WEIGHT_CLAMP,
    VALID_EVENT_TYPES,
    clamp_weight,
)
from .affinity import (
    compute_affinity,
    decay_weight,
    tier_of,
    tier_index_of,
    TierResult,
    ritual_gate,
    build_signal,
)

__all__ = [
    "AffinityParams",
    "WEIGHT_CLAMP",
    "VALID_EVENT_TYPES",
    "clamp_weight",
    "compute_affinity",
    "decay_weight",
    "tier_of",
    "tier_index_of",
    "TierResult",
    "ritual_gate",
    "build_signal",
]
