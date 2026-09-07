# coding: utf-8
"""session.replay —— replay 剧本回放 + 可注入时钟(前端之二,与 engine 解耦)。

前情:好感度是「读取时计算、随时间衰减」的纯函数 —— 真实世界里你要等 14 天才看到
衰减;demo 不能等。解决:SessionEngine 接受一个可注入的 clock(()->UTC 秒)。

本模块提供:
- ReplayClock:可推进的假时钟(从 base 起步,支持 step_days / 绝对推进)。
- ReplayRecord:一行动作 [user消息(必) + after_days(自上一轮过去多少天,选)]。
- run_replay():按顺序喂给引擎,记录每条(回复/好感度/层级/事件),返回日志。
  引擎内部 agent 回复、书记员抽取、好感度读取全部走这只时钟 → 衰减显形。

剧本约定:records 为 list[dict],dict 字段:
    user: str            本轮玩家说的话
    after_days: float     距上一轮过去了多少天(缺省 0.0 = 连续)
    note: str (选)        记录时附注,只写日志,不进对话
真事件流由剧本按《v1.1 §七.3》造:刷好感/放鸽子/显式指令等边界行为都要覆盖。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .engine import SessionEngine

_ONEDAY = 86400.0


class ReplayClock:
    """可推进假时钟。engine.clock 应指到它 —— 它才是衰减/时间线的唯一主人。"""

    def __init__(self, base_utc: Optional[float] = None):
        self._now = base_utc if base_utc is not None else time.time()

    def __call__(self) -> float:
        return self._now

    def advance_seconds(self, secs: float) -> float:
        self._now += secs
        return self._now

    def advance_days(self, days: float) -> float:
        return self.advance_seconds(days * _ONEDAY)

    @property
    def now(self) -> float:
        return self._now


@dataclass
class ReplayTurn:
    user: str
    after_days: float = 0.0
    note: str = ""


@dataclass
class ReplayLogEntry:
    """单轮回放日志(可整段交给 web/测试做断言)。"""
    seq: int
    user: str
    reply: str
    affinity_after: float
    tier_after: str
    ritual_blocked: bool
    event_recorded: bool
    event_type: Optional[str]
    clock_utc: float
    note: str = ""


def load_replay(path: str) -> list[ReplayTurn]:
    """从 .jsonl / .json 载入剧本。jsonl 逐行;json 直接取 list[dict]。"""
    turns: list[ReplayTurn] = []
    with open(path, encoding="utf-8") as f:
        data = f.read()
    if path.endswith(".json"):
        objs = json.loads(data)
    else:
        objs = [json.loads(l) for l in data.splitlines() if l.strip()]
    for o in objs:
        turns.append(ReplayTurn(
            user=str(o.get("user", "")),
            after_days=float(o.get("after_days", 0.0)),
            note=str(o.get("note", "")),
        ))
    return turns


def run_replay(engine: SessionEngine,
               turns: list[ReplayTurn],
               clock: Optional[ReplayClock] = None,
               extract: bool = True) -> tuple[ReplayClock, list[ReplayLogEntry]]:
    """按顺序回放:每轮先把假时钟推过 after_days,再让引擎跑一轮。

    Returns:
        (clock, log):clock 最终停在剧本末尾;log 每条 = 一轮完整状态。
    """
    clock = clock or ReplayClock()

    # 覆写引擎的时钟:让它的 抽取写 ts / 好感度读 as_of 全部走回放时钟
    engine.clock = clock

    log: list[ReplayLogEntry] = []
    for i, t in enumerate(turns, 1):
        if i > 1:  # 第一轮直接从 base 开始;其后每轮先推进
            clock.advance_days(t.after_days)
        # 剧情落在这只时钟的『现在』
        before_ts = clock.now
        res = engine.turn(t.user, force_no_event=not extract)
        log.append(ReplayLogEntry(
            seq=i,
            user=t.user,
            reply=res.reply,
            affinity_after=res.affinity,
            tier_after=res.tier_key,
            ritual_blocked=res.ritual_blocked,
            event_recorded=res.event_recorded,
            event_type=res.event_type,
            clock_utc=before_ts,
            note=t.note,
        ))
    return clock, log


def narrate(log: list[ReplayLogEntry]) -> None:
    """把回放日志打成可读报告(CLI / 记录用)。"""
    for e in log:
        tag = f"[事件:{e.event_type}]" if e.event_recorded else "[NO_EVENT]"
        line = (f"[{e.seq:02d}] 亲和 {e.affinity_after:6.2f} | 层级 {e.tier_after:>4}"
                f" | {tag}")
        if e.ritual_blocked:
            line += " | ⚠仪式门槛未过(值到段被拦)"
        print(line)
        if e.note:
            print(f"      └（剧本注:{e.note}）")
        print(f"  玩家: {e.user[:36]}")
        print(f"  芽衣: {e.reply[:120]}{'…' if len(e.reply) > 120 else ''}")


if __name__ == "__main__":
    # 例:python -m session.replay <剧本.jsonl> [--noextract]
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if len(sys.argv) < 2:
        print("用法: python -m session.replay <剧本.jsonl> [--noextract]")
        sys.exit(1)
    script = load_replay(sys.argv[1])
    from .engine import build_session_engine
    uid = os.getenv("AYAME_USER_ID", "u_replay_demo")
    eng = build_session_engine(uid)
    clock, log = run_replay(eng, script, extract="--noextract" not in sys.argv)
    narrate(log)
    print("\n[最终状态]", json.dumps(eng.state(), ensure_ascii=False))
