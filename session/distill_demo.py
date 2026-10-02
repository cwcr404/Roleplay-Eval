# coding: utf-8
"""L2 真实蒸馏链路 demo —— 供维护者过目(本地可跑,不烧真 token)。

覆盖:
  场景 A:成功路径 —— 特例触发 -> 真实 LLM 蒸馏 -> save_distilled(ok)
  场景 B:降级①路径 —— distill LLM 抛错 -> memory_not_distilled(不冒充 L2)
  场景 C:常规(每10轮)触发 + events_seen 游标攒批(仅吃新增)

法子:用路由型假 LLM,按 system(prompt)区分 抽取/蒸馏/主回复 三个出口,
   蒸馏出口返回 distill_l2.md 规格的两段文本。全程 tmp 数据域,不动真实 memory。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from session.replay import ReplayClock  # noqa: E402

DISTILL_MARK = "记忆蒸馏官"  # distill_l2.md 内部特征;假 LLM 用它路由蒸馏出口


class _RoutingFake:
    """路由三个出口,各自返回;可开关任一出口抛错来测降级。"""
    def __init__(self, main_reply="芽衣:我在,你说。", distill_text=None,
                 raise_distill=False, extract=None):
        self.main_reply = main_reply
        self.distill_text = distill_text
        self.raise_distill = raise_distill
        self.extract = extract or {"has_event": False}
        self.sys_seen = []

    def __call__(self, system, user, **kw):
        self.sys_seen.append(system[:30])
        if DISTILL_MARK in system:
            if self.raise_distill:
                raise RuntimeError("LLM 蒸馏出口炸了(模拟真实外部错误)")
            return self.distill_text or ""
        if "本条对话" in user:  # 书记员抽取出口
            had = self.extract.get("has_event")
            if not had:
                return json.dumps({"has_event": False}, ensure_ascii=False)
            return json.dumps(self.extract, ensure_ascii=False)
        return self.main_reply


def _new_env(prefix):
    tmp = tempfile.mkdtemp(prefix=prefix)
    os.environ["KB_DATA_ROOT"] = tmp
    from kb.memory.service import open_user_memory
    from session.engine import SessionEngine
    # 读 distill_l2.md 作为蒸馏 prompt(产品线同样读这份)
    base = os.path.join(tmp, "..", "..", "roleplay-eval")  # fallback;真实路径下方覆盖
    dpath = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "prompts/distill_l2.md")
    with open(dpath, encoding="utf-8") as f:
        distill = f.read()
    mem = open_user_memory(f"u_{prefix}")
    clock = ReplayClock()
    return tmp, mem, clock, distill


def build(prefix, distill_text, raise_distill=False, extract=None):
    tmp, mem, clock, distill = _new_env(prefix)
    from session.engine import SessionEngine
    fake = _RoutingFake(distill_text=distill_text, raise_distill=raise_distill,
                        extract=extract)
    eng = SessionEngine(f"u_{prefix}", memory=mem,
                        character_prompt="你是雷电芽衣。成熟温柔的大姐姐。",
                        extractor_prompt="你是记忆书记员,输出 JSON。",
                        distill_prompt=distill, llm_chat=fake, clock=clock)
    return eng, mem


GOOD_DISTILL = ("【我们的故事】\n"
                "那次我们一起走过量子之海,他在岔道口选了留下殿后那条路,"
                "说好等我。早些时候我们还约了周末去看雪山日出。\n"
                "---WHO---\n"
                "【他是谁·参照注】\n"
                "他会为了并肩的人留下来;我们约好去雪山看日出。")


def scenario_A():
    """成功路径:一条剧情级/承诺事件 -> 特例立刻触发真实蒸馏 -> ok 落盘。"""
    print("\n======== 场景 A:成功路径(特例立刻触发 -> ok) ========")
    eng, mem = build("A", GOOD_DISTILL,
                     extract={"has_event": True, "type": "承诺与约定", "content":
                              "玩家与芽衣约定周末一起去看雪山日出", "weight_delta": 3,
                              "is_plot_critical": True})
    r = eng.turn("周末牵我去看日出好不好?")
    print(f"turn 回复片段: {r.reply[:20]}...")
    prof = mem.l2()
    assert prof is not None
    data = prof.data
    print(f"[L1 入账] events_seen(账本)={mem.ledger.size()}")
    print(f"[distill_log 末条] {eng.distill_log[-1]}")
    print(f"[L2 缓存] distill_status={prof.distill_status} events_seen={prof.events_seen}")
    print(f"[L2 我们的故事] {data.get('我们的故事','')[:50]}...")
    print(f"[L2 他是谁] {data.get('他是谁','')[:40]}...")
    # 断言: 真实蒸馏成功、非规则回退、故事+他是谁两段都在、光标吃满
    assert prof.distill_status == "ok", prof.distill_status
    assert not prof.rebuilt_from_l1
    assert "雪山" in data.get("我们的故事", "")
    assert data.get("他是谁", "")
    assert prof.events_seen == mem.ledger.size()
    print("✅ 场景 A 通过:ok 落盘,两段齐,游标吃满,非冒充规则回退")
    return eng


def scenario_B():
    """降级①:distill LLM 抛错 -> memory_not_distilled,不写相像物冒充 L2。"""
    print("\n======== 场景 B:降级路径(distill LLM 抛错 -> memory_not_distilled) ========")
    eng, mem = build("B", GOOD_DISTILL, raise_distill=True,
                     extract={"has_event": True, "type": "情绪显著时刻", "content":
                              "玩家在剧情高潮向芽衣坦白一直以来的孤独",
                              "weight_delta": 2, "is_plot_critical": True})
    r = eng.turn("我其实一直是一个人撑到现在……")
    prof = mem.l2()
    log = eng.distill_log[-1]
    print(f"[distill_log 末条] {log}")
    print(f"[L2 缓存现状] distill_status={getattr(prof,'distill_status','N/A')} "
          f"rebuilt={getattr(prof,'rebuilt_from_l1',None)}")
    # 断言:没有假装蒸馏成功 —— 状态标记不是 ok,也绝不把半吊子当完整画像注入
    assert prof is None or prof.distill_status != "ok", prof
    assert log.get("path") == "memory_not_distilled", log
    # 降级标记不进芽衣可见注入文本(系统结构化状态,不是剧情素材)
    ctx = eng._memory_context()
    assert "memory_not_distilled" not in ctx, ctx
    print("✅ 场景 B 通过:标记未蒸馏,不冒充;降级标记不泄进注入区")
    return eng


def scenario_C():
    """常规(每10轮):足够轮次后吃 events_seen 之后全部新增(攒批,非仅触发那条)。

    用『非特例类型』积累(玩家身份/偏好不入特例池,不会提前蒸馏),
    验证专用 常规每10轮 触发时把 events_seen 之后的全部新增一次吃满。
    """
    print("\n======== 场景 C:常规触发(每10轮) + events_seen 攒批 ========")
    eng, mem = build("C", GOOD_DISTILL)
    fake = eng._llm_chat
    # 头 7 轮各塞一条『玩家身份与偏好』(非特例类型,不触发立即蒸馏,只攒账本)
    for i in range(7):
        fake.extract = {"has_event": True, "type": "玩家身份与偏好",
                        "content": f"玩家偏好事物{i}", "weight_delta": 1,
                        "is_plot_critical": False}
        eng.turn(f"第{i}轮:我平时喜欢{i}。")
    assert not any("特例" in str(l.get("trigger")) for l in eng.distill_log), \
        "非特例类型不应触发立即蒸馏"
    # 补足到第 11 轮(专职看常规触发器,不塞新事件)
    for i in range(7, 11):
        fake.extract = {"has_event": False}
        eng.turn(f"第{i}轮:今日天气不错。")
    log = eng.distill_log[-1]
    prof = mem.l2()
    print(f"[distill_log 末条] trigger={log.get('trigger')} new_events={log.get('new_events')}")
    print(f"[L2] events_seen={getattr(prof,'events_seen',None)} (账本应={mem.ledger.size()})")
    # 断言:常规在 >=10 轮触发;吃的是 events_seen 游标后全部新增(>=该批次事件数)
    assert "常规" in log.get("trigger", ""), log
    assert log.get("new_events", 0) >= 7, log
    assert getattr(prof, "events_seen", 0) == mem.ledger.size()
    print("✅ 场景 C 通过:常规<每10轮>触发,攒批输入吃满 events_seen 后的全部新增")
    return eng


if __name__ == "__main__":
    scenario_A()
    scenario_B()
    scenario_C()
    print("\n==== L2 真实蒸馏链路 demo 全部通过 ====")
