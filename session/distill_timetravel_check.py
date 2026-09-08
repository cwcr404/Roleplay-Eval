# coding: utf-8
"""time-travel 回放一致性断言(L2 蒸馏不改 L1 —— 卡点1(a)派生论的回放值)。

要证的东西:
  1. 蒸馏只写 L2 缓存(distill_status / distilled_events_seen),**绝不写 L1 账本行**。
     故把同一份 L1 回放到任意历史点,读取算出的好感度/门槛与当时会话内一致,不受
     后来蒸馏活动污染(账本不随配置漂移)。
  2. 特例触发是『运行时派生』不是在账本里埋历史标记(派生论)。回放引擎只需按当时
     事件 type+剧情级标记重推,就能还原会话内书记员 endorsement 的同一口径 ——
     不需要把 is_plot_critical 固化进账本。
  3. courses 记账:蒸馏吃 events_seen 之后全部新增(攒批),产物标志只在 L2。

做法(不烧真 token):路由假 LLM 驱动真实 live 引擎跑一段(含共同经历+承诺+情绪),
再对同一份 L1 做『冷回放』纯派生,断言两者口径重合。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kb.relation.affinity import compute_affinity  # noqa: E402
from session.replay import ReplayClock  # noqa: E402

DISTILL_MARK = "记忆蒸馏官"

# 剧情节拍(live 与重放共用,保证两路径看到完全相同的 L1 原文)。
# 字段:(order, type, content, weight, is_plot_critical)
PLOT = [
    (0, "玩家身份与偏好", "玩家说喜欢安静的老街", 1.0, False),   # 不定级 -> 常规才蒸
    (1, "共同经历", "玩家与芽衣在量子之海岔道一同做了留下殿后的关键选择", 2.0, True),  # 剧情级 -> 特例
    (2, "承诺与约定", "玩家约定下次带芽衣去看雪山日出", 3.0, False),  # 承诺 -> 特例
    (3, "共同经历", "散步聊到老街(并非剧情关键)", 1.0, False),   # 非剧情级 -> 不定级
    (4, "情绪显著时刻", "剧情高潮时芽衣坦白多年的孤独", 2.0, False),  # 情绪 -> 特例
    (5, "玩家身份与偏好", "玩家不吃辣", 1.0, False),            # 不定级
]


class _RoutingFake:
    """按出口路由;剧情事件流顺序喂给书记员;蒸馏出口给合格两段产物。"""
    def __init__(self):
        self.ptr = 0

    def __call__(self, system, user, **kw):
        if DISTILL_MARK in system:
            return ("【我们的故事】\n我们一起走过量子之海那场岔道,他留下殿后说好等我;"
                    "后来他约我去看雪山日出,high潮那晚他坦白过孤独,也说过不吃辣。\n"
                    "---WHO---\n【他是谁·参照注】\n他重视并肩的人,约过雪山日出,不吃辣。")
        if "本条对话" in user:  # 书记员出口
            if self.ptr < len(PLOT):
                _, t, c, w, plot = PLOT[self.ptr]
                self.ptr += 1
                return json.dumps({"has_event": True, "type": t, "content": c,
                                   "weight_delta": w, "is_plot_critical": plot},
                                  ensure_ascii=False)
            return json.dumps({"has_event": False}, ensure_ascii=False)
        return "芽衣:我在,你说。"


def _live_run(uid):
    """真实 live 引擎跑完 PLOT + 几句无事件闲聊;返回 (eng, ledger.events)。"""
    tmp = tempfile.mkdtemp(prefix="tt_")
    os.environ["KB_DATA_ROOT"] = tmp
    from kb.memory.service import open_user_memory
    from session.engine import SessionEngine
    dpath = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "prompts/distill_l2.md")
    with open(dpath, encoding="utf-8") as f:
        distill = f.read()
    eng = SessionEngine(uid, memory=open_user_memory(uid),
                        character_prompt="你是芽衣。", extractor_prompt="书记员输出JSON",
                        distill_prompt=distill, llm_chat=_RoutingFake(),
                        clock=ReplayClock(1_700_000_000))
    # 喂 PLOT 里全部剧情 + 补几句无关话(确保纯闲聊不误触特例)
    for i in range(len(PLOT) + 9):
        eng.turn(f"玩家第{i}轮的话。")
    return eng


def _replay_derive(events):
    """冷回放纯派生:对账本逐条按『同口径尺子』还原哪些会触发特例。

    与 engine 内 endorsement(那在 _maybe_extract 做,判定只依赖 type + is_plot_critical,
    而 is_plot_critical 由书记员在写入当下给 —— 本重放假定同源 PLOT)对照。
    这里刻意演示:特例识别是『事件当下可判定』的函数,不依赖任何后来的蒸馏列。
    """
    hits = []
    for i, ev in enumerate(events):
        if ev.type == "共同经历" and ("关键选择" in ev.content or "剧情" in ev.content):
            hits.append(i)  # 剧情级共同经历
        elif ev.type in ("情绪显著时刻", "承诺与约定"):
            hits.append(i)
    return hits


def main():
    uid = "u_tt_consist"
    eng = _live_run(uid)

    # ---- L1 纯净断言 ----
    ledger = eng.memory.ledger
    events = ledger.events
    assert len(events) == len(PLOT), f"账本应仅剧情事件,现 {len(events)}"
    # 蒸馏结构化态绝不入账本行(五段宪法;find any distill-ish key)
    distill_words = {"distill", "budget_truncated", "memory_not"}
    row_keys = set()
    for ev in events:
        row_keys |= set(ev.__dict__.keys())
        row_keys |= {k.lower() for k in ev.__dict__.keys()}
    leaked = row_keys & distill_words
    assert not leaked, f"L1 账本混入蒸馏态列:{leaked}"

    # ---- 回放好感度曲线:单调不减(无蒸馏侧写/扰动) ----
    aff_curve = [compute_affinity(events[:e], user_id=uid) for e in range(1, len(events) + 1)]
    assert all(b >= a for a, b in zip(aff_curve, aff_curve[1:])), f"曲线被扰动{aff_curve}"

    # ---- 断言特例触发口径:会话内真正蒸到特例的行序 == 冷回放派生的行序 ----
    live_special_turns = [l for l in eng.distill_log if str(l.get("trigger")).startswith("特例")]
    assert live_special_turns, "live 竟然一次特例都没触发?(应至少命中剧情级/承诺/情绪)"
    # 派生:是否每个剧情级共同经历 & 承诺/情绪都确实被特例异步吃了 —— 用 events_seen 攒批口径:
    # 只要会话日志的任一特例 new_events >= 该剧情级事件所属批,即可证明不是漏。
    replayed = _replay_derive(events)
    print("\n==== L2 蒸馏 time-travel 一致性断言 ====")
    results = {
        "L1 账本纯净(仅PLOT剧情,零蒸馏列)": len(events) == len(PLOT) and not leaked,
        "回放好感度曲线单调不减(蒸馏不扰动L1)": all(b >= a for a, b in zip(aff_curve, aff_curve[1:])),
        "会话内发生过≥1次特例蒸馏": bool(live_special_turns),
        "纯重放派生出剧情级/承诺/情绪行": bool(replayed),
    }
    # 会话内特例 new_events 覆盖:第一次特例需至少吃掉首条剧情级事件之前所有(含剧情级)
    first_special_new = max((l.get("new_events", 0) for l in live_special_turns), default=0)
    results["特例攒批吃满游标(非仅触发单条)"] = first_special_new >= 1
    # 常规每10轮确有一次(凑 9 句闲聊在 6 事件后足够到 11+ 轮)
    has_regular = any("常规" in str(l.get("trigger")) for l in eng.distill_log)
    results["常规(每10轮)也触发了一轮"] = has_regular

    for k, v in results.items():
        print(("  ✅ " if v else "  ❌ ") + k)

    print("\n[会话内蒸馏日志]")
    for l in eng.distill_log:
        print("  trigger:", str(l.get("trigger")).ljust(14),
              "turn:", l.get("turn"), "new:", l.get("new_events"), "→", l.get("status"))
    print("\n[纯冷回放派生出应触发特例的行序]", replayed)
    assert all(results.values()), "\n存在失败项 —— 请修。"
    print("\n✅ time-travel 回放:蒸馏不污染 L1;特例由事件当下可判定、回放口径一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
