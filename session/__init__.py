# coding: utf-8
"""session —— 多轮会话引擎包(记忆/关系接线的宿主)。

engine     SessionEngine(人格焊死 + 记忆注入 + 关系距离 + 书记员异步入账)
replay     replay 剧本回放 + 可注入时钟(好感度衰减验收)
web        stdlib 单页 demo 前端(不引第三方)

区别于 run_eval:评测是无状态单轮(严禁接记忆);这里是真实多轮会话的唯一宿主。
"""
from .engine import SessionEngine, build_session_engine, TurnResult

__all__ = ["SessionEngine", "build_session_engine", "TurnResult"]
