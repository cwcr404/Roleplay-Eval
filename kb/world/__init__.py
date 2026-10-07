# coding: utf-8
"""kb/world/__init__.py —— 芽衣的世界模型（认知层）。

与 kb/memory/roles.py 的分工：
- roles.py  = 识别层（文本里出现了谁 / 别名归一 / 边界判定）
- world/    = 认知层（她认识谁、怎么称呼、关系如何、有哪些共同记忆、亲历哪些场景）
"""
from . import mei_world  # noqa: F401

__all__ = ["mei_world"]
