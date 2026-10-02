# coding: utf-8
"""kb.memory —— 记忆层(跨会话持久,user_id 宪法级隔离)。

文档:docs/memory_architecture.md
- §二 L1 事件账本:append-only、行格式、五类、防投毒(第三人称描述性)。
- §二.5 事件抽取器:独立书记员(本包只负责存 / 去重 / 读,抽取 prompt 另在
  prompts/extractor_event.md,由运行时调 LLM 异步执行)。
- §三 L2 蒸馏缓存:可从头 L1 重建(容灾)、永不衰减。
- §六 UTC 单列 + user_id 第一天就有、测试与真实物理隔离。

落盘:data/memory/(KB 之下与 sources 同级)。只存个人环境,不进公司系统。
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from ..relation.affinity import LedgerEvent, compute_affinity
from ..relation.params import (clamp_weight, VALID_EVENT_TYPES, AffinityParams,
                               PROMISE_SETTLE_TYPES, PROMISE_TYPE)

# 结算事件的 content 前缀:定死引用协议,consume by open_promises() 聚合。
# 格式:settled: <原承诺content>(无缝实现『状态=查询派生,改状态=追加事件』)。
SETTLED_PREFIX: str = "settled: "

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 数据根:可用环境变量 KB_DATA_ROOT 覆盖(自检/多环境用),默认 roleplay-eval/data
_DEFAULT_DATA = os.path.join(_BASE, "data")
DATA_MEMORY_DIR = os.path.join(_DEFAULT_DATA, "memory")
LEDGER_FILENAME = "l1_events.jsonl"

def _data_root() -> str:
    root = os.environ.get("KB_DATA_ROOT") or _DEFAULT_DATA
    return root

def _memory_dir() -> str:
    d = os.path.join(_data_root(), "memory")
    os.makedirs(d, exist_ok=True)
    return d


def _now_utc_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _data_dir() -> str:
    return _memory_dir()


@dataclass
class Ledger:
    """某用户的 L1 事件账本(append-only)。线程安全(简单锁)。"""

    user_id: str
    _params: AffinityParams = None       # type: ignore[assignment]
    _events: list[LedgerEvent] = None    # type: ignore[assignment]
    _lock: threading.Lock = None         # type: ignore[assignment]
    _file: str = None                    # type: ignore[assignment]

    def __post_init__(self):
        if self._params is None:
            self._params = AffinityParams()
        if self._events is None:
            self._events = []
        if self._lock is None:
            self._lock = threading.Lock()
        self._file = os.path.join(_data_dir(), f"l1_{self.user_id}.jsonl")

    # -- 读(全量,喂给 compute_affinity / 蒸馏) ------
    @property
    def events(self) -> list[LedgerEvent]:
        with self._lock:
            return list(self._events)

    def load(self) -> "Ledger":
        """从磁盘载入(若文件存在)。容灾:坏行跳过。"""
        if os.path.exists(self._file):
            with open(self._file, encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line:
                        continue
                    ev = _parse_row(line, self.user_id)
                    if ev is not None:
                        self._events.append(ev)
        # 断言已有同一文件的加载不会重复:每次新建实例是干净的
        return self

    # -- 写(append-only):永不修改历史行 ------------
    def append(self, etype: str, content: str,
               weight_delta: float,
               ts: float | None = None,
               price: bool = True) -> Optional[LedgerEvent]:
        """新增一行事件(append-only)。返回规范化后的行;非法输入返回 None。

        Args:
            etype: 合法事件类型之一(见 VALID_EVENT_TYPES;含承诺兑现/违约)。
            content: 第三人称描述性,禁止我/你指代(调用方负责清洗)。
            weight_delta: 书记员建议值(会先按类型夹回钳制范围,再对正增量套 80+ 写时折扣)。
            ts: 写入时间(默认 now)。price 阶按『此 ts 时刻、不含本事件』的存量好感度判定。
            price: True 则执行完整定价(类型钳制 + 80+ 写时递减);False 则原样落账
                (低级测试/还原用 —— 正常情况下恒 True,定价只此一处,避免二次打折)。

        注:运行时去重不追求完美 —— 偶发的同主题复述(如玩家重申同一约)在
        14 天半衰期下会被自然刷新/衰减,伤害有界且被仪式门槛兜住(demo 阶段
        不做事件关联精度,留 v2.0)。真正去重在抽取器侧靠「上下文给足」:
        见 prompts/extractor_event.md + service 装配(最近5条 + 活跃承诺)。
        """
        if etype not in VALID_EVENT_TYPES:
            return None
        if not content or not content.strip():
            return None
        if ts is None:
            ts = _now_utc_ts()
        ts = float(ts)
        # 定价:类型钳制(价格权限) + 正增量 80+ 写时折扣 —— 账本存定值,读取不再打折
        if price:
            from ..relation.affinity import priced_delta
            aff_before = compute_affinity(self.events, as_of_time=ts,
                                          params=self._params, user_id=self.user_id)
            w = priced_delta(etype, float(weight_delta), aff_before, self._params)
        else:
            from ..relation.params import clamp_weight as _cw
            w = _cw(etype, float(weight_delta))
        ev = LedgerEvent(user_id=self.user_id, timestamp_utc=ts,
                         type=etype, content=content.strip(), weight_delta=w)
        with self._lock:
            self._events.append(ev)
            _append_row(self._file, ev)   # 落盘即 append-only
        return ev

    def size(self) -> int:
        return len(self.events)

    # -- 去重辅助(供抽取上游判定是否已入账) ------
    def recent(self, n: int = 5) -> list[LedgerEvent]:
        """最近 n 条(供书记员做『只记新事实/复述不入账』判断)。"""
        evs = self.events
        return evs[-n:]

    def by_type(self, etype: str) -> list[LedgerEvent]:
        """某类型全部事件,最早在前(零持久索引 —— 现场查表,几百行零成本)。"""
        return [ev for ev in self._events if ev.type == etype]

    def open_promises(self, as_of_time: float | None = None) -> list[LedgerEvent]:
        """截至 as_of_time 仍【活跃】的承诺(from_query 聚合派生,最早在前)。

        单一事实源:不做第二份活跃索引,书记员的「当前活跃承诺参照」直接调它。
        定义:一条承诺行 P(承诺与约定)在 t 仍 open,iff
          - P.ts <= t(已立约);且
          - 不存在结算事件 S ∈ 承诺兑现/承诺违约,S.ts ∈ (P.ts, t],
            S.content == 'settled: ' + P.content(该承诺已被后续兑现/违约结算)。
        可回放:as_of_time 传任意历史 t => 只看到那之前的状态,天然正确、零额外机制
        (绝不用 status 字段 / 不改历史行 —— 那是会污染 time-travel 的 false 事实源)。
        """
        now = as_of_time if as_of_time is not None else _now_utc_ts()
        promises = [ev for ev in self._events
                    if ev.type == PROMISE_TYPE and ev.timestamp_utc <= now]
        settled_refs = {
            ev.content for ev in self._events
            if ev.type in PROMISE_SETTLE_TYPES
            and ev.timestamp_utc <= now
            and ev.content.startswith(SETTLED_PREFIX)
        }
        return [p for p in promises
                if (SETTLED_PREFIX + p.content) not in settled_refs]

    def settle_promise(self, outcome_type: str, content_ref: str = "",
                       ts: float | None = None) -> Optional[LedgerEvent]:
        """承诺结算入账(兑现/违约):追加一条带 settled 引用的结算事件,不改任何历史行。

        这是『状态=查询派生』的操作端 —— 结算不是去把某行标 resolved,而是
        append 一条 承诺兑现(+2)/承诺违约(-4,照钳制表)事件,其 content 规范成
        SETTLED_PREFIX + 原承诺全文。open_promises() 随后自动把被引用的承诺
        从活跃集里摘掉 —— 原行逐字节不变,time-travel 回放白送正确。

        Args:
            outcome_type: 承诺兑现 或 承诺违约(须 ∈ PROMISE_SETTLE_TYPES)。
            content_ref: 定位原承诺——接受原承诺全文(子串即可命中),留空取最新 open。
            ts: 结算入账时间(默认 now)。
        Returns:
            追加成功的结算事件;若没有可结算的 open 承诺(引用/落空)则 None,
            此时【什么都不入账】—— 宁可不记也不记一笔悬空结算(保持不变量干净)。
        """
        if outcome_type not in PROMISE_SETTLE_TYPES:
            return None
        # 时间权威:筛选候选承诺必须与本条结算入账同源 —— 用调用方传入的 ts
        # (可空,空则回退墙钟)。若这里硬用墙钟,replay 推进到未来再结算时,
        # 未来立的约在真实 now 下不可见 → 结算落空返回 None(时间权威分裂)。
        cands = self.open_promises(as_of_time=ts if ts is not None else _now_utc_ts())
        if not cands:
            return None
        hit = None
        if content_ref:
            hit = next((p for p in reversed(cands)
                        if content_ref in p.content or p.content in content_ref), None)
        target = hit or cands[-1]
        return self.append(outcome_type,
                           SETTLED_PREFIX + target.content,
                           {  # 钳制表定价(+2/-4)由 append 内部 priced_delta 统一处理
                               "承诺兑现": 2.0, "承诺违约": -4.0,
                           }[outcome_type],
                           ts=ts)


# ---- 磁盘 IO(行格式:user_id|timestamp_utc|type|content|weight_delta)----

def _append_row(filepath: str, ev: LedgerEvent) -> None:
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(ev.to_row() + "\n")


def _parse_row(raw: str, expect_user: str) -> Optional[LedgerEvent]:
    """把一行文本解析回 LedgerEvent;格式坏/用户不符则 None。

    行格式严格 5 段:user_id|timestamp_utc|type|content|weight_delta。
    承诺的 open/结算状态是查询聚合派生的,绝不写在历史行里(keep append-only 纯),
    故行里没有 status 字段(= 亡掉 5/6 段兼容的温床;账本空,零迁移成本)。
    """
    parts = raw.split("|")
    if len(parts) != 5:
        return None
    user, ts_iso, etype, content, wstr = parts
    if user != expect_user:
        return None
    if etype not in VALID_EVENT_TYPES:
        return None
    try:
        # ts 段兼容两种格式:新格式 = str(float) 原生无损文本(直接 float());
        # 旧格式 = ISO 微秒串(round-trip 有 2e-7 级误差,仅用于读历史旧行)。
        try:
            ts = float(parts[1])
        except ValueError:
            ts = datetime.fromisoformat(ts_iso).timestamp()
        w = float(wstr)
    except ValueError:
        return None
    return LedgerEvent(user_id=user, timestamp_utc=ts, type=etype,
                       content=content, weight_delta=w)


def open_user_ledger(user_id: str,
                     params: Optional[AffinityParams] = None) -> Ledger:
    """打开某用户的 L1 账本(不存在则新建)。user_id 即隔离键。

    params 是【写时定价】用的权威参数(钳制+80+递减都靠它读 affinity 判折扣)。
    必须与调用方读取时用的 params 同一份 —— 定价单一事实源,拒绝两套参数打架。
    """
    return Ledger(user_id=user_id, _params=params).load()
