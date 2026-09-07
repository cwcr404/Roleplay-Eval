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
# 人格内核文本不在此 —— 只约束输出形状,不改『芽衣是谁』。
_ROUTING_ASSEMBLY = (
    "【本轮输出格式指令·装配层】\n"
    "请在回复的【第一行】用方括号声明本条类型:[闲聊] 或 [攻略]。\n"
    "- [闲聊]:像平常一样自然回应、延续谈兴,把自己放在当下的相处里。\n"
    "- [攻略]:对方在问打法/养成/配置等实际怎么玩 —— 用【此刻相关素材】里的事实,以你\n"
    "  自己的口吻讲成能照做的建议(克制与温度都要有),别照本宣科念条;素材没提的就别硬造。\n"
    "第一行声明完标记后,从第二行起才是你能被对方看到的话。这条类型声明只是让系统知道\n"
    "你走了哪条路,不要把它讲给对方听。"
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
        self._history: list[tuple[str, str]] = []
        self._lock = threading.Lock()

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
        """跑一轮:装配记忆 → Agent2 回复 → (异步)书记员抽取入账。

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

        # --- 2. 书记员抽取(独立、冷面;不阻塞上面回复) ---
        ev = None
        if not force_no_event and self.extractor_prompt:
            ev = self._maybe_extract(user_msg, reply)

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
            return "闲聊", reply
        # 洞1:剥掉首行标记子串,确认无残留才放行
        rest = lines[1] if len(lines) > 1 else ""
        visible = strip_marker(first_line)
        if rest:
            visible = visible + "\n" + rest if visible else rest
        return mod, visible

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
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # 书记员(异步抽取入账)—— 详见 prompts/extractor_event.md + B 验证
    # ------------------------------------------------------------------
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
            ev = self.memory.record_event(
                obj.get("type"), obj.get("content", ""), obj.get("weight_delta", 0),
                ts=self.clock(),  # 走注入时钟,replay 才能看到衰减
            )
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

    # ---- 状态出口 ----
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
) -> SessionEngine:
    """一键装配:读角色卡+书记员 prompt+现场装载该用户的记忆链。

    kb: 产品线预查素材库 {guide,lore}(build_kb() 产物)。默认 None = 不预查
        (记忆/评测/回放保持零 KB 行为,避免拉低其它验收);想开预查由产品线
        web/chat 显式传 kb。
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
    )
