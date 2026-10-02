# coding: utf-8
"""session —— 多轮会话引擎(记忆/关系接线的宿主)。

这是「记忆+关系系统」真正落地的地方:run_eval 是无状态单轮评测(靠 guard 严禁
接记忆),而真实的多轮对话会话交在这里 —— 每个 user_id 一条链,边走边把书记员
抽出来的事件写进 L1,好感度由读取时计算驱动,绝不写回。

设计原则(memory_architecture + 四柱架构):
- 人格常量化:角色卡(character_card / agent2)焊死 system,说什么都不改。
- 记忆注入:只写在角色卡预留的【用户记忆】区(Phase 2 口),是外挂上下文,
  不进系统提示的推理主路径 —— Agent2 的人格决定她『是谁』,记忆决定『记得什么』。
- 关系=距离指令:好感度/层级不塞裸数字进推理路径,换算成一段克制的
  『你们现在的距离/默契』自然语言描述,放进记忆区,调『怎么对你』、不调『是谁』。
- 抽取异步:回复先出,书记员在这一轮结束后续跑;慢/超时静默跳过,不阻塞主链路。
- 可注入时钟:clock(realtime)默认,replay 传可推进的假时钟,让好感度能看到
  时间衰减(避免真的等 14 天)。抽取写的 ts、好感度读的 as_of 都走这个时钟。

本模块不 import utils.api,便于无 API 的接线测试(decay/gate/l2 均可离线验证)。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from kb.memory.service import UserMemory, open_user_memory

# ---- 关系层级 -> 语气/距离的克制描述(不把裸数字给推理层) ------------------
# 键为 tier 的机器名(见 kb.relation.affinity TierResult)。值是一句『关系定位』,
# 由 engine 写进【用户记忆】区,让 Agent2 在人格不变的前提下,调『怎么对你』。
AYAME_DISTANCE_TEXTS: dict[str, str] = {
    "陌生": "你们只是初次同行,彼此还不太了解。",
    "同行": "你们已经并肩走过些日子,是彼此信任的同行者。",
    "亲近": "你们共历过不少风浪,已是能交心的亲近之人。",
    "羁绊": "你们已把彼此放进生命里,是跨越生死的羁绊。",
}

# Agent2 回复时可见的最近转写轮数上限(控制注入体积)
MAX_HISTORY_TURNS = 10
# KB 预查素材量(§四:攻略≤top_k条 / 背景≤整块) —— 意图闸门取用上限
GUIDE_TOP_K = 6
LORE_TOP_K = 2

# 前置装配段(产品线内嵌路由,拼装顺序见 _system_for_turn —— 人格卡最前→此段→记忆外挂):
# 让芽衣在回复首行自报 [闲聊]/[攻略],engine 据此分叉并剥离(标记绝不见于玩家可见流)。
# 路由器只做路由,不做导演 —— 演法归人格块,单一事实源归位(09-08 维护者改五定稿措辞)。
_ROUTING_ASSEMBLY = (
    "【本轮输出格式指令·装配层】\n"
    "请在回复的【第一行】用方括号声明本条类型:[闲聊] 或 [攻略]。\n"
    "- [攻略]=玩家需要游戏打法/配置类干货的回答。\n"
    "- [闲聊]=其余一切对话。\n"
    "第一行声明完标记后,从第二行起才是你能被对方看到的话。怎么说话,按你本来的样子来。"
)
# 距离描述作为关系信号的最低可信度(低于此不回读,保持克制)
# (数值本身只用于读距离描述,不进推理 —— 见 _closeness_text)


@dataclass
class TurnResult:
    """一轮会话的完整回报(供 CLI / replay / web 消费)。"""
    user_msg: str
    reply: str
    affinity: float = 0.0
    tier_key: str = ""
    ritual_blocked: bool = False
    event_recorded: bool = False            # 书记员是否真的写了一行 L1
    event_type: Optional[str] = None
    injected_memory: str = ""               # 本轮回调注入的记忆/关系上下文(调试用)


class SessionEngine:
    """单个用户的多轮会话引擎。
    Args:
        user_id: 宪法级隔离键 —— 一个真人/一条测试数据一条链。
        model: 对话 LLM(经 self._llm_chat 注入,默认 None=不可用,可离线测记忆)。
        clock: ()->float UTC 秒。默认实时;replay/web 测试可传可推进假时钟。
        memory: 可注入的 UserMemory;None 则按 user_id 现场装载。
        character_prompt: 角色卡(焊死的 system)。
        extractor_prompt: 书记员 prompt;None 则跳过自动抽取。
        history_turns: 注入 Agent2 上文的最远转写轮数。
    """

    def __init__(
        self,
        user_id: str,
        model: str = "deepseek-chat",
        clock: Optional[Callable[[], float]] = None,
        memory: Optional[UserMemory] = None,
        character_prompt: Optional[str] = None,
        extractor_prompt: Optional[str] = None,
        history_turns: int = MAX_HISTORY_TURNS,
        llm_chat: Optional[Callable] = None,
        llm_stream: Optional[Callable] = None,
        kb: Optional[dict] = None,
        distill_prompt: Optional[str] = None,
        # ---- 产品线 Agent3 异步质检(默认关;评测/回放/selfcheck 不配则不触发) ----
        quality_prompt: Optional[str] = None,   # agent3_async_verdict.md 全文;None=关质检
        quality_judge: Optional[Callable] = None,  # 可注入 judge(...)->Verdict;None=用 async_quality.judge_once
        quality_auto: bool = False,              # True=每轮回复后自动异步跑质检
    ):
        self.user_id = user_id
        self.model = model
        self.clock = clock or time.time
        self.memory = memory or open_user_memory(user_id)
        self.character_prompt = character_prompt
        self.extractor_prompt = extractor_prompt
        self.history_turns = history_turns
        # 两条 LLM 出口都走这里 —— 便于测试注入假实现、避免直接依赖 utils.api
        self._llm_chat = llm_chat or self._default_llm_chat
        self.llm_stream = llm_stream  # 流式出口(产品线打字机用);None=退同步 _llm_chat
        self._kb = kb  # 知识库检索器 {guide,lore};None=惰性 build_kb(离线/不配则不查)
        self._kb_log: list[str] = []  # 每轮 KB 闸门观测日志(攻略无素材入栈,冒烟观测)
        self._last_modality: Optional[str] = None  # 上轮路由结果("闲聊"/"攻略";观测/验收读)
        # ---- 产品线 Agent3 异步质检状态(会话级;quality_prompt=None 时整块不动) ----
        self.quality_prompt: Optional[str] = quality_prompt
        self.quality_judge: Optional[Callable] = quality_judge
        self.quality_auto: bool = quality_auto
        self._qc_pending_verdict = None   # 上一轮质检判决(供下一轮壳取);线程写、加锁读
        self._qc_pending_shell = ""        # 上一轮折好的固定壳文本(空=不注入)
        self._qc_polish_run = 0           # 连续修正计数(pass 清零)
        self._qc_turns_total = 0          # 质检生效轮数(仪表分母)
        self._qc_turns_shelled = 0        # 实际注入了壳的轮数(仪表分子)
        self._qc_judge_errors = 0         # judge 死亡计数器(尾巴一:非零即报警)
        self.quality_panel: list[dict] = []  # 判决落面板/日志(观测/验收读;防异步=没跑)
        self._qc_lock = threading.Lock()
        # ---- 标记中途出现计数(改四·可观测):流式下正文中途冒 [闲聊]/[攻略] 无法
        # 事后抹除,只能记日志+计数。这里按『单轮内可见流残留标记数』累加观察。
        self._marker_mid_hits = 0            # 累计残留标记数(所有轮)
        self._marker_mid_turns = 0           # 至少残留 1 个标记的轮数
        self.marker_mid_log: list[dict] = []  # 每轮一条:{turn, hits, reply_head}
        self._history: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        # ---- L2 蒸馏触发状态(会话级;非账本状态) ----
        self._total_turns = 0  # 实测对话轮数(跨 history 裁剪单调递增;常规蒸馏 cadence 用它)
        # 蒸馏是引擎职责。这些计数/信号是『会话态』,不是账本态 —— 不落 L1、不影响
        # time-travel。它们驱动『此刻要不要蒸』的运行时判定,错了只影响蒸馏时机,
        # 防抖/常规每10轮/强制超预算都会补兜,绝不污染任何持久事实源。
        self.distill_prompt: Optional[str] = distill_prompt  # L2 蒸馏 instruction(如 None 则蒸馏关闭)
        self._last_regular_distill_turn: Optional[int] = None  # 最近一次常规蒸馏的总轮号
        self._last_any_distill_turn: Optional[int] = None      # 最近一次任意蒸馏的总轮号(防抖基准)
        self._pending_distill_signals: list[str] = []          # 书记员上报的特例信号({type}s),非账本镜像
        # 蒸馏日志(供观测/验收):每次 _maybe_distill 定案追加一条
        self.distill_log: list[dict] = []

    # ---- LLM 出口(默认走 utils.api;可被测试替换) ----
    @staticmethod
    def _default_llm_chat(system: str, user: str, **kw) -> str:
        from utils.api import chat  # 延迟 import,避免离线测试也需要 API key
        return chat(system, user, **kw)

    def _main_reply(self, system: str, user_prompt: str, temperature: float = 0.8) -> str:
        """生成 Agent2 主回复主体。

        流式优先:若注入了 llm_stream(生成器逐块),聚合它的块作为完整回复
        (打字机/首字快;调用计数=1 次主回复产出,块聚合不另计)。
        否则退同步 _llm_chat(记忆/评测/旧测试的锚 —— selfcheck/replay/run_eval
        全不配 llm_stream,行为与重构前逐字相同)。
        """
        if self.llm_stream is not None:
            try:
                from utils.api import stream_to_text  # 延迟 import
                gen = self.llm_stream(system, user_prompt,
                                      model=self.model, temperature=temperature)
                return stream_to_text(gen)
            except Exception:
                # 流式失败不应拖垮主链路 —— 回退同步出口(记日志由上层决定)
                pass
        return self._llm_chat(system, user_prompt, model=self.model,
                              temperature=temperature)

    # ------------------------------------------------------------------
    # 记忆/关系上下文装配:(外挂上下文,不掺进系统推理主路径)
    # ------------------------------------------------------------------
    def _sig(self) -> dict:
        """读当前关系信号 —— 好感度等全走注入时钟(时钟一致性)。

        事件写入 ts=self.clock()、读取 as_of=self.clock()：demo/replay 里把时钟
        推到场景『现在』,衰减才看得见;否则写入(假时钟)与读取(真墙钟)混用,
        晚些写的事件会被当作『未来』整条跳过,亲和钉死在初始值。
        """
        g = self.memory.gate(ritual_completed=False, as_of_time=self.clock())
        return g.to_signal()

    def _memory_context(self) -> str:
        """渲染角色卡【用户记忆】区要注入的一段(GPT/本地都好懂)。

        内容 = L2 故事层(她在意、记得的『我们的故事』,永不衰减)
              + 当前活跃承诺(玩家还没放下的约定)
              + 你现在与对方的关系距离(克制描写,代替裸数值)
        """
        blocks: list[str] = []

        # 1) L2 故事层(若有;这是她会『不自觉地记得』的东西)
        prof = self.memory.l2()
        md = prof.to_markdown() if prof is not None else ""
        if md:
            blocks.append("【你与TA共同经历的片段】(你是真的记得这些,自然放在心上即可,不必逐条背诵):\n" + md)

        # 2) 当前活跃承诺(玩家还在意、还等着兑现的约定)
        # 单一事实源 = ledger.open_promises(as_of=引擎时钟)。已结算(兑现/违约)的
        # 自动摘掉,不会再把玩家已经放下/破掉的约定当成“还欠着”反复注入。
        # as_of_time 必须走引擎时钟(和写入共源)—— 默认墙钟会把回放前进到未来的
        # 结算误判成“未来”,承诺就不会摘,重蹈“时间权威分裂”的坑。
        proms = self.memory.ledger.open_promises(as_of_time=self.clock())
        if proms:
            # 最新承诺在账本尾部,渲染时保证最早在前(读完即自然顺序),给最近的
            lines = "\n".join(f"· {p.content}" for p in proms[-4:])
            blocks.append("【你还未兑现/仍记着的约定】:\n" + lines)

        # 3) 关系距离(读取时由好感度折算的克制描述 —— 决定你对她多亲近)
        sig = self._sig()
        tk = sig.get("tier", "")
        blocks.append("【你们如今的关系】:" + AYAME_DISTANCE_TEXTS.get(tk, AYAME_DISTANCE_TEXTS["同行"]))

        if not blocks:
            return ""
        return "\n\n".join(blocks)

    def _system_for_turn(self) -> str:
        """本轮的完整 system = 焊死的人格 + 装配段 + 外挂记忆区(人格常量绝不改)。

        拼装顺序(§二·建议4 钉死): 人格卡原文固定【最前】→ 前置『先自报标记』装配段
        → 记忆外挂区在后。任何内容都不得插入人格文本中间 —— 人格是"芽衣是谁"的锚。
        人格卡(agent2_character.md)字节不动(红线段:人格内核源文本冻结),装配段作为
        独立字符串在 code 层拼接,既满足架构又零触碰卡文件。
        """
        base = self.character_prompt or ""
        # 1) 人格段(固定最前,绝不动)
        parts = [base] if base else []
        # 2) 前置装配段(产品线内嵌路由指令;评测线不配 character 或走独立 agent1,不影响)
        if _ROUTING_ASSEMBLY:
            parts.append(_ROUTING_ASSEMBLY)
        # 3) 记忆外挂区(角色卡如有【用户记忆】节则卡里已含,不外拼;否则兜底拼末节)
        mem = self._memory_context()
        if mem and "【用户记忆】" not in base:
            parts.append("【用户记忆】\n" + mem)
        elif mem:
            # 卡内部已带【用户记忆】锚点:内容接在 base 后即可(卡内该节会与注入内容汇合)
            parts.append(mem)
        return "\n\n".join(p for p in parts if p)

    # ------------------------------------------------------------------
    # 一轮主流程
    # ------------------------------------------------------------------
    def turn(self, user_msg: str, *, reply_hook: Optional[Callable] = None,
             force_no_event: bool = False) -> TurnResult:
        """跑一轮:装配记忆 → Agent2 回复 → 书记员抽取入账。

        时序说明(纠正旧『异步』措辞):书记员抽取【同步容错】执行 —— 失败/解析错
        对主链路无影响(见 _maybe_extract 的 try/except → None),但它不在单独
        线程里跑,故本方法返回前书记员已收工。这【不是】Agent3 质检那种可折下一
        轮的异步:书记员的产出(_pending_distill_signals)由同轮 _maybe_distill
        消费,存在【同轮因果依赖】,挂线程会漏触发特例蒸馏。需要真异步时由上层
        前端决定(如 web 端把整轮放队列),不在引擎内拆。

        reply_hook: 若提供,退出该轮时把已入账事件也交给它(web 回写等用)。
        force_no_event: True 时不跑抽取(评测隔离等场景),默认 False。
        """
        user_msg = (user_msg or "").strip()
        system = self._system_for_turn()

        # --- 1. 主回复:Agent2(人格)在『记忆+关系』上下文下生成芽衣的话 ----
        hist = self._render_history()
        user_prompt = self._compose_user_prompt(hist, user_msg)
        raw_reply = self._main_reply(system, user_prompt)
        # 首行自报标记 → 解析出本条 modality + 从可见正文剥离(洞1)[告2 缺省见 _route_from_reply]
        modality = None
        visible_reply = raw_reply
        if raw_reply:
            modality, visible_reply = self._route_from_reply(raw_reply)
        reply = visible_reply
        self._last_modality = modality  # 本回路由结果(观测/验收读)

        # --- 写入转写历史(供下一步/回放;忆可见正文,不带标记) ---
        with self._lock:
            self._history.append((user_msg, reply))
            self._history = self._history[-self.history_turns:]
            self._total_turns += 1  # 单调总轮数(常规蒸馏 cadence 基准;不受 history 裁剪影响)

        # --- 2. 书记员抽取(独立、冷面;同步容错 —— 见 turn() docstring 时序说明) ---
        ev = None
        if not force_no_event and self.extractor_prompt:
            ev = self._maybe_extract(user_msg, reply)

        # --- 2.5 L2 蒸馏巡检(三触发 + 防抖 + 攒批;特例信号已由书记员收集进池) ---
        # 蒸馏关闭(distill_prompt=None,默认)则整块无操作 —— 不烧多余的 LLM。
        self._maybe_distill()

        sig = self._sig()
        return TurnResult(
            user_msg=user_msg,
            reply=reply or "(回复失败)",
            affinity=round(sig.get("affinity", 0.0), 2),
            tier_key=sig.get("tier", ""),
            ritual_blocked=bool(sig.get("ritual_blocked", False)),
            event_recorded=ev is not None,
            event_type=ev.type if ev else None,
            injected_memory=self._memory_context(),
        )

    def _render_history(self) -> str:
        """转写历史(最近 history_turns 轮)渲染成给 Agent2 的上下文。"""
        with self._lock:
            hist = list(self._history)
        if not hist:
            return ""
        lines = []
        for u, m in hist:
            lines.append(f"玩家: {u}\n芽衣: {m}")
        return "\n\n".join(lines)

    def _ensure_kb(self):
        """返回注入的 KB(仅显式配 kb 才参与预查);未注入则 None=本轮免素材区。

        KB 预查是产品线(web/chat)专属增强:记忆/评测/selfcheck 不配 kb,保持零 KB
        行为(不自动 build),避免拉低其它验收 —— 想开启由调用方 build_session/chat
        显式传 kb 即可。返回 None 时素材区为空、链路零额外开销。
        """
        return self._kb or None

    def _kb_sources_for(self, text: str) -> str:
        """预查(案A裁决):每轮把玩家原话送去 guide + lore 各查一遍,检索结果做意图闸门。

        不靠首行标记(那在生成后才到,会违背单次调用) —— 而是用『素材命中』当闸门:
        - guide 命中(玩家确实问到攻略/配队/wiki话术) -> 素材区给 V1 攻略条(≤top_k)。
        - guide 空但 lore 命中(聊到熟识的人事) -> 素材区给 V2 背景整块。
        - 都空 -> 素材区留空(纯闲聊,不硬灌)。
        素材拼成独立区(不进人格/system),由 agent2 在回复里自行决定是否取用。
        返回拼装好的素材正文段(空串=本轮无素材,调用方不必另加区)。
        """
        kb = self._ensure_kb()
        if not kb:
            return ""
        guide_hits = kb.get("guide").query(text, top_k=GUIDE_TOP_K) if kb.get("guide") else []
        lore_hits = kb.get("lore").query(text, top_k=LORE_TOP_K) if kb.get("lore") else []
        # 意图闸门:攻略素材优先;guide 空则退背景;素材都空则本轮免素材区
        if guide_hits:
            items = [g.content for g in guide_hits[: GUIDE_TOP_K]]
            head = "【你可以想起的攻略要领】(事实进正文前在这里,供你此刻参考带出,无需逐条背诵):"
        elif lore_hits:
            items = [l.content for l in lore_hits[: LORE_TOP_K]]
            head = "【你记得的、与此刻相关的过往片段】(可自然带入一两笔,不必硬提):"
        else:
            self._kb_log.append(f"no_kb_hit:: mtime={self.clock()} 攻略素材无命中(按闲聊/自由处理)")
            return ""
        body = "\n".join(f"· {it}" for it in items)
        return head + "\n" + body

    # ---- 首行标记(洞1剥离 / 洞2缺省) ----
    def _route_from_reply(self, reply: str) -> tuple[str, str]:
        """从一条回复解析首行标记并剥离;缺省(自由文本/空/emoji)记日志按[闲聊]落。

        Returns:
            (modality, 玩家可见正文):modality ∈ {"闲聊","攻略"}(解析失败落 [闲聊]);
            正文已剥掉首行标记子串(洞1:可见流 0 标记)。parse 失败会往 self._kb_log
            记一条洞2 缺省日志(含原始首行原文)。
        """
        from .routing import modality_from_first_line, strip_marker
        if not reply:
            self._kb_log.append(f"route_default:: empty reply → 按[闲聊]处理")
            return "闲聊", ""
        lines = reply.split("\n", 1)
        first_line = lines[0]
        mod = modality_from_first_line(first_line)
        if mod is None:
            # 缺省分支(洞2):不重试不阻塞,记日志含原始首行;正文原样放行(人味闲聊)
            self._kb_log.append(
                f"route_default:: first_line={first_line!r} → 按[闲聊]处理")
            self._note_mid_markers(reply)
            return "闲聊", reply
        # 洞1:剥掉首行标记子串,确认无残留才放行
        rest = lines[1] if len(lines) > 1 else ""
        visible = strip_marker(first_line)
        if rest:
            visible = visible + "\n" + rest if visible else rest
        self._note_mid_markers(visible)
        return mod, visible

    def _note_mid_markers(self, visible: str) -> int:
        """改四·可观测:记本轮可见流里残留的标记数(首行标记已被剥离,此处只数残留)。

        流式下正文中途冒标记无法事后抹除 → 记日志+计数,冒烟观察频率;高频再考虑
        正文侧缓冲。本方法只观测,不改写任何可见文本(不阻塞/不重生成)。
        """
        from .routing import count_markers
        hits = count_markers(visible)
        self.marker_mid_log.append({
            "turn": self._total_turns + 1,  # +1:_total_turns 在 _route 后被 turn() 递增
            "hits": hits,
            "reply_head": (visible or "")[:30],
        })
        if hits > 0:
            self._marker_mid_hits += hits
            self._marker_mid_turns += 1
            self._kb_log.append(
                f"marker_mid:: 本轮可见流残留 {hits} 个标记(无法事后抹除,记档观察)")
        return hits

    def qc_marker_mid_hits(self) -> int:
        """单轮内标记中途出现计数的累计器(改四仪表):可见流残留标记总数。"""
        return self._marker_mid_hits

    def qc_marker_mid_turns(self) -> int:
        """至少残留 1 个标记的轮数(改四仪表分子)。"""
        return self._marker_mid_turns

    def marker_mid_rate(self) -> float:
        """标记中途出现轮次率(残留轮数/总轮数)。冒烟观察项:常态应接近 0。"""
        total = self._total_turns
        return (self._marker_mid_turns / total) if total else 0.0

    def _compose_user_prompt(self, hist: str, user_msg: str) -> str:
        """把转写历史 + 当前言拼成一条 user prompt。

        KB 素材区(案A预查结果)也拼在【此刻】前 —— 让 agent2 在生成前已可见
        攻略/背景干货,能『有据改写不编』。素材区独立成块,不进 system(人格/记忆)。
        """
        parts = []
        if hist:
            parts.append("【之前的对话】\n" + hist)
        kb_block = self._kb_sources_for(user_msg)
        parts.append("【此刻】\n" + f"玩家: {user_msg}")
        if kb_block:
            # 素材放在 player 原话之后、紧贴生成输入,避免被长历史稀释
            parts.append("【此刻相关素材】\n" + kb_block)
        # 质检壳(产品线 • Agent3 结果进【用户侧输入流】作附注,不进 system/人格块):
        # 由调用方在上一轮回复后 quality_tick 折好;仅当质检开启且有可用壳才拼。
        with self._qc_lock:
            qc_shell = self._qc_pending_shell if self.quality_prompt else ""
            self._qc_pending_shell = ""  # 一次性:本轮消费后清空,防旧壳泄漏给下下轮
        if qc_shell:
            parts.append("【附】" + qc_shell)
        return "\n\n".join(parts)

    # -------------------------------------------------------------------
    # Agent3 异步质检(产品线;编排 doc §三)
    # 纪律:质检绝不阻塞主回复生成/绝不重生成玩家已见回复;判决落面板 + 折成
    # 下一轮固定壳。默认 quality_prompt=None=质检关,评测/回放/selfcheck 零影响。
    # 异步现实:turn() 永不内联跑 judge —— 由调用方(web 循环/demo)在回复已
    # 返回玩家后调 quality_tick(可自行挂线程/队列),或 quality_auto=True 时
    # turn() 用守护线程调度。目的:judge 不在『主回复返回路径』同步等待。
    # -------------------------------------------------------------------
    @staticmethod
    def _default_judge(prompt, player_msg, mode, reply, facts, model):
        from session.async_quality import judge_once
        return judge_once(prompt, player_msg, mode, reply, facts, model=model)

    def _qc_store(self, v) -> None:
        """落面板 + 存 pending(下一轮壳取),线程安全。"""
        from session.async_quality import inject_fragment
        with self._qc_lock:
            self.quality_panel.append({
                "verdict": getattr(v, "verdict", "pass"),
                "category": getattr(v, "category", "none"),
                "register": getattr(v, "register", "none"),
                "reason": getattr(v, "reason", ""),
                "note": getattr(v, "note", ""),
            })
            shell, advance = inject_fragment(v, self._qc_polish_run)
            self._qc_pending_shell = shell  # 空=不注入(pass/达闸/被剥光),但仍推进计数如下
            if advance:
                if getattr(v, "verdict", "pass") == "pass":
                    self._qc_polish_run = 0  # 频次闸:pass 清零
                else:
                    self._qc_polish_run += 1

    def quality_tick(self, user_msg: str, mode: str, reply: str,
                     facts: str = "", model: str = "deepseek-chat") -> Optional[dict]:
        """对『已返回玩家的本轮回复』跑一次质检(回复已定格,judge 绝不改写它)。

        调用方在回复流式推给玩家后、下一轮输入前调(可挂线程)。返回判决 dict,
        已入 self.quality_panel + 折好 self._qc_pending_shell 供下一轮壳。
        quality_prompt=None(质检关)时返回 None,不产生任何副作用。
        """
        if not self.quality_prompt:
            return None
        self._qc_turns_total += 1
        judge = self.quality_judge or self._default_judge
        try:
            v = judge(self.quality_prompt, user_msg, mode, reply, facts, model=model)
        except Exception:
            # judge 失败 → 降级 pass+note,不 pretend 已认真判
            # —— 但失败不能是静默假 pass:API key/prompt 路径错时全都会走这里,
            #    仪表上跟『一切正常』不可区分(异步打盹加强版)。记死计数器,
            #    冒烟/面板看一眼非零即报警,别让质检系统成下一个『睡了没人知』。
            with self._qc_lock:
                self._qc_judge_errors += 1
            from session.async_quality import Verdict
            v = Verdict(verdict="pass", category="none", register="none",
                        reason="", note="质检离线/本次未判", ok=False)
        self._qc_store(v)
        if self._qc_pending_shell:
            self._qc_turns_shelled += 1
        return self.quality_panel[-1]

    def qc_injection_rate(self) -> float:
        """质检壳注入率(带壳轮数/质检生效轮数)。Judge 偏松设计下常态应 <0.2。"""
        if not self._qc_turns_total:
            return 0.0
        return self._qc_turns_shelled / self._qc_turns_total

    @property
    def qc_judge_errors(self) -> int:
        """judge 死亡计数器:非零即意味着质检系统在静默降级(配置错/API key 错/路径错),
        冒烟/面板/运维看一眼即报警。0 = 质检切实跑起来了(不是只降级没报过错)。"""
        return self._qc_judge_errors


    def _maybe_extract(self, user_msg: str, reply: str):
        """对本轮跑一次书记员;有事件则经 memory 钳制入账 L1(append-only)。

        幂等/容错:抽取失败或解析失败 -> 返回 None(不进账),不抛 —— 宁漏勿滥,
        且不因书记员拖垮主链路(真正的并发异步由上层前端决定;这里保证不阻塞)。
        """
        try:
            payload = self._render_extractor_payload(user_msg, reply)
            raw = self._llm_chat(self.extractor_prompt or "", payload,
                                 model=self.model, temperature=0.0)
        except Exception:
            return None

        import json as _json
        try:
            obj = _json.loads(raw.strip())
            if not obj.get("has_event"):
                return None
            typ = obj.get("type")
            ev = self.memory.record_event(
                typ, obj.get("content", ""), obj.get("weight_delta", 0),
                ts=self.clock(),  # 走注入时钟,replay 才能看到衰减
            )
            # ---- 特例信号收集(卡点1(a)派生论) ----
            # 剧情级子判定由书记员产出时顺带报(is_plot_critical),随 content 一起到引擎。
            # 只当信号、只驱动蒸馏时机(缓存类决策),绝不驱动账本写入/weight_delta。
            if ev is not None and self.distill_prompt:
                plot = bool(obj.get("is_plot_critical", False))
                if typ == "共同经历" and plot:
                    self._push_signal(f"剧情级共同经历@{obj.get('content','')[:14]}")
                elif typ in ("情绪显著时刻", "承诺与约定", "承诺兑现", "承诺违约"):
                    self._push_signal(f"{typ}@{obj.get('content','')[:14]}")
            return ev
        except Exception:
            return None

    def _render_extractor_payload(self, user_msg: str, reply: str) -> str:
        """按 B 定稿:本条对话 + 最近5条账本 + 当前活跃承诺(方向1轻量版)。"""
        def _ev(e):
            return f"[{e.type}] {e.content} (权重 {e.weight_delta:g})"
        recent = [_ev(e) for e in self.memory.ledger.recent(5)]
        prom = [_ev(e) for e in self.memory.ledger.open_promises(as_of_time=self.clock())]
        return (
            f"【本条对话】\n玩家: {user_msg}\n芽衣: {reply}\n\n"
            f"【最近 5 条账本记录】\n"
            + ("\n".join(recent) if recent else "(无)")
            + "\n\n" + ("【当前活跃承诺与约定】\n" + "\n".join(prom)
                        if prom else "(无已入账承诺)")
        )

    # ----------------------------------------------------------------------
    # L2 蒸馏巡检 —— 三触发 + 防抖 + 双计数 + 降级,(卡点2攒批输入)见下
    # 触发规格(docs/memory_architecture §三 + L2 蒸馏规格 2026-09-08):
    #   · 常规 = 每 10 轮实测对话触发一次
    #   · 强制 = 现有 L2 画像超 500 字符上限 -> 即时压缩
    #   · 特例 = ②剧情级共同经历/③情绪显著/④承诺约定 事件入账即触发
    #   · 防抖 = 两次蒸馏间隔 >= N 轮(特例风暴时合并到轮末批量,不必每事件都蒸)
    #   · 攒批 = 每次吃的输入是『events_seen 之后全部新增事件』,非仅触发那几条
    # 本方法在 turn() 末尾调用:self.distill_prompt 为 None(未开蒸馏)则整块无操作。
    #     ----------------------------------------------------------------------
    _DEBOUNCE_N = 3  # 特例/批量蒸馏最小间隔(实测起步 3)
    # 特例触发类型:共同经历剧情级(content 里带剧情/选择信号),情绪显著,承诺兑现/违约/约定
    _SPECIAL_TYPES = frozenset({"共同经历", "情绪显著时刻", "承诺与约定",
                                "承诺兑现", "承诺违约"})

    def _push_signal(self, tag: str):
        """把一条特例触发信号塞进会话级待蒸馏池(去重)。只加信号,不碰账本。

        卡点1(a)派生论铁律:池子是『会话态』不是『账本态』。里面的信号只驱动
        蒸馏时机(缓存类决策)—— 触发错了顶多多蒸一次/少蒸一轮,防抖与 10 轮
        常规与超预算强制都会兜底;绝不用它驱动任何账本写入/weight_delta。
        """
        if tag not in self._pending_distill_signals:
            self._pending_distill_signals.append(tag)

    def _existing_l2_blocks(self) -> dict:
        """当前 L2 画像两段(供蒸馏输入 + 超预算判断);无缓存则空。"""
        try:
            from kb.memory.distill import load_l2
            prof = load_l2(self.user_id)
        except Exception:
            prof = None
        if prof is None or not prof.data:
            return {"我们的故事": "", "他是谁": ""}
        return {"我们的故事": prof.data.get("我们的故事", ""),
                "他是谁": prof.data.get("他是谁", "")}

    def _l2_over_budget(self) -> bool:
        """现有 L2 画像是否超预算(强制触发)。统一读 CHAR_BUDGET(规格单位=字符,与规格数字同单位)。"""
        try:
            from kb.memory.distill import load_l2, CHAR_BUDGET
            prof = load_l2(self.user_id)
        except Exception:
            return False
        if prof is None:
            return False
        # 单一权威口径:三段(故事/他是谁…)字符总和 > CHAR_BUDGET 即视为超预算强制重蒸。
        # (计量单位钉死:规格单位=字符,CHAR_BUDGET 与规格数字同单位、同数字;
        #  故不再逐字段另设 MAX_TOKENS*2 宽松线。)
        total = sum(len(str(v)) for v in prof.data.values() if isinstance(v, str))
        return total > CHAR_BUDGET

    def _prev_events_seen(self) -> int:
        """蒸馏游标：上次『真实 LLM 蒸馏』吃到的账本事件数(只认 distilled_events_seen)。

        注意不是 events_seen —— 后者被注入层的规则重建(service.l2 自动重刷)也写,
        若混用会把『规则回退重建过』误当成『已真实蒸过』,导致常规触发吃不到新增。
        规则重建不写 distilled_events_seen(它默认 0),故未真蒸过则从 0 全量抢回。
        """
        try:
            from kb.memory.distill import load_l2
            prof = load_l2(self.user_id)
        except Exception:
            prof = None
        return getattr(prof, "distilled_events_seen", 0) or 0

    def _distill_input_prompt(self, new_events) -> str:
        """按 distill_l2.md 输入块装:新增事件 + 现有旧画像(L2 合并重写用)。"""
        blocks = []
        if new_events:
            lines = "\n".join(
                f"- [{ev.type}] {ev.content} (权重 {ev.weight_delta:g})"
                for ev in reversed(new_events)
            )
            blocks.append(f"【自上次蒸馏以来新增的 L1 事件】(共 {len(new_events)} 条):\n{lines}")
        old = self._existing_l2_blocks()
        if old.get("我们的故事") or old.get("他是谁"):
            parts = [f"【我们的故事】\n{old['我们的故事']}" if old["我们的故事"] else ""]
            parts += [f"【他是谁】\n{old['他是谁']}" if old["他是谁"] else ""]
            blocks.append("【现有 L2 旧画像(要重写合并,不是追加)】:\n" + "\n".join(x for x in parts if x))
        return "\n\n".join(blocks)

    def _maybe_distill(self) -> Optional[dict]:
        """三触发 + 防抖 + 降级的 L2 蒸馏之主。蒸馏关闭(distill_prompt=None)则 None。

        返回本次蒸馏定案日志 dict(供观测/验收);本次未触发返回 None。
        动不到它,蒸馏就不发生 —— 违规产物一律回退并标降级,不冒充完整蒸馏。
        """
        if not self.distill_prompt:
            return None
        t = self._total_turns

        # --- 触发判定(三模,互不排他,一次批量处理) ---
        from kb.memory.distill import (
            CHAR_BUDGET, STATUS_OK, STATUS_NOT_DISTILLED,
            STATUS_BUDGET_TRUNCATED, truncate_to_budget, _split_who,
            save_distilled, DISTILL_EVERY_N_TURNS,
        )
        regular = ((t - (self._last_regular_distill_turn or 0)) >= DISTILL_EVERY_N_TURNS)
        forced = self._l2_over_budget()
        special = bool(self._pending_distill_signals)

        # --- 防抖:特例连续命中时,距上次任意蒸馏不足 N 轮则本轮跳过(等下次/攒批) ---
        debounced_special = False
        if special and self._last_any_distill_turn is not None \
                and (t - self._last_any_distill_turn) < self._DEBOUNCE_N:
            debounced_special = True  # 特例先攒着,信号不丢,下轮再验

        if not (regular or forced or (special and not debounced_special)):
            return None

        reason = []
        if regular:
            reason.append("常规(每10轮)")
        if forced:
            reason.append("强制(超预算)")
        if special and not debounced_special:
            reason.append(f"特例({len(self._pending_distill_signals)})")

        # --- 攒批输入(卡点2):events_seen 游标后全部新增事件 ---
        prev_seen = self._prev_events_seen()
        chain = getattr(self.memory, "ledger", None)
        new_events = list(chain.events[prev_seen:]) if chain is not None else []
        now_seen = len(chain.events) if chain is not None else prev_seen

        # 无新增但被触发(强制压缩历史超预算)—— 仍允许,喂旧画像去压缩
        user_prompt = self._distill_input_prompt(new_events)
        log = {"trigger": ";".join(reason) or "强制",
               "turn": t, "debounced_special": debounced_special,
               "new_events": len(new_events), "events_seen": now_seen}

        status = STATUS_OK
        dirty = False
        result_story = result_who = ""
        if new_events or forced:
            # ---- 真实 LLM 蒸馏一次 ----------------
            try:
                raw = self._llm_chat(self.distill_prompt, user_prompt or "请蒸馏新增事件。",
                                     model=self.model, temperature=0.0)
            except Exception as e:
                # 降级①:LLM 调用失败/异常/空输出 -> 不重试,落规则回退并标记未蒸馏
                log["path"] = "memory_not_distilled"
                log["error"] = str(e)[:80]
                status = STATUS_NOT_DISTILLED
            else:
                if not (raw and raw.strip()):
                    log["path"] = "memory_not_distilled"
                    log["error"] = "empty llm output"
                    status = STATUS_NOT_DISTILLED
                else:
                    parts = _split_who(raw)
                    total_len = len(parts["我们的故事"]) + len(parts["他是谁"])
                    if total_len > CHAR_BUDGET:
                        # 降级②:蒸馏成功但超预算 -> 先定向压缩一次(把超预算产物原样喂回,
                        # 只指令『压到 500 内、里程碑不丢』),仍超则句边界机械截断。
                        try:
                            c = self._llm_chat(self.distill_prompt, raw,
                                               model=self.model, temperature=0.0)
                        except Exception:
                            c = None
                        if c and c.strip():
                            cp = _split_who(c)
                            result_story = cp["我们的故事"]
                            result_who = cp["他是谁"]
                        else:
                            result_story = truncate_to_budget(raw, CHAR_BUDGET)
                            result_who = ""
                        status = STATUS_BUDGET_TRUNCATED
                    else:
                        result_story = parts["我们的故事"]
                        result_who = parts["他是谁"]
            # 真实蒸馏产物(可能带 budget_truncated)落盘
            if status == STATUS_OK or status == STATUS_BUDGET_TRUNCATED:
                try:
                    save_distilled(self.user_id,
                                   {"我们的故事": result_story, "他是谁": result_who},
                                   events_seen=now_seen, status=status)
                    dirty = True
                except Exception as e:
                    log.setdefault("path", "save_error")
                    log["error"] = str(e)[:80]

        # 降级①(memory_not_distilled):不写任何相像物冒充 L2 —— 仅本处清信号/记状态。
        if status == STATUS_NOT_DISTILLED:
            log["path"] = "memory_not_distilled"

        # ---- 记计数/清信号(会话态,提交上调度) ----
        if regular:
            self._last_regular_distill_turn = t
        if dirty or status == STATUS_NOT_DISTILLED:
            self._last_any_distill_turn = t
        self._pending_distill_signals.clear()
        log["status"] = status
        self.distill_log.append(log)
        return log

    def state(self) -> dict:
        sig = self._sig()
        return {
            "user_id": self.user_id,
            "turns": self._history_count(),
            "affinity": round(sig.get("affinity", 0.0), 2),
            "tier": sig.get("tier", ""),
            "ritual_blocked": bool(sig.get("ritual_blocked", False)),
        }

    def _history_count(self) -> int:
        with self._lock:
            return len(self._history)

    def reset(self):
        with self._lock:
            self._history = []


# ---------- 便捷工厂:从运行时环境装配完整引擎（角色卡+书记员+记忆） ----------
def build_session_engine(
    user_id: str,
    *,
    model: str = "deepseek-chat",
    clock: Optional[Callable[[], float]] = None,
    memory: Optional[UserMemory] = None,
    character_prompt_path: str = "prompts/agent2_character.md",
    extractor_prompt_path: str = "prompts/extractor_event.md",
    llm_chat: Optional[Callable] = None,
    llm_stream: Optional[Callable] = None,
    kb: Optional[dict] = None,
    enable_distill: bool = False,
    distill_prompt_path: str = "prompts/distill_l2.md",
) -> SessionEngine:
    """一键装配:读角色卡+书记员 prompt+现场装载该用户的记忆链。

    kb: 产品线预查素材库 {guide,lore}(build_kb() 产物)。默认 None = 不预查
        (记忆/评测/回放保持零 KB 行为,避免拉低其它验收);想开预查由产品线
        web/chat 显式传 kb。
    enable_distill: 默认 False=蒸馏关闭(每 10 轮/超预算/特例三触发不动作)。
        需要真实 L2 LLM 蒸馏的产品线(replay/web/chat)显式开 True —— 这样
        自检/评测/纯记忆链路不被多余的蒸馏 LLM 调用污染。
    """
    import os
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    def _read(p):
        with open(os.path.join(base, p), encoding="utf-8") as f:
            return f.read()
    return SessionEngine(
        user_id=user_id,
        model=model,
        clock=clock,
        memory=memory,
        character_prompt=_read(character_prompt_path),
        extractor_prompt=_read(extractor_prompt_path),
        llm_chat=llm_chat,
        llm_stream=llm_stream,
        kb=kb,
        distill_prompt=(_read(distill_prompt_path) if enable_distill else None),
    )
