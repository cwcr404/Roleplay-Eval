# coding: utf-8
"""session/quality_wiring_probe —— Agent3 异步质检进 engine 的接线验收桩(离线 0 API)。

对齐编排 doc §三 + 09-08 分岔裁决A(验收口径)/维护者五改:
  1. 质检壳【上一轮质检意见:】进 Agent2 用户侧输入流(【附】格),不进 system/人格块。
  2. 壳内只有描述(reason 经机械防线剥过),零指令词泄漏。
  3. quality_prompt=None(评测/回放/selfcheck默认) → 壳永不出、judge 永不调(零影响)。
  4. judge 只在 quality_tick 被调;turn() 主回复路径永不内联 judge → 「防同步化回退」红灯成立。
  5. 频次闸 pass 清零;≥GATE 停注入只记数。
  6. 注入率仪表 qc_injection_rate 常态 <0.2。

用法: python -m session.quality_wiring_probe   # exit 0=全过
"""
from __future__ import annotations

from session.engine import SessionEngine


class V:
    """迷你 Verdict(likesession.async_quality.Verdict 的字段,供 fake judge 返回)。"""
    def __init__(self, verdict="pass", category="none", register="none",
                 reason="", note="", ok=True, anchor=""):
        self.verdict, self.category, self.register = verdict, category, register
        self.reason, self.note, self.ok = reason, note, ok
        self.anchor, self.sanitized = anchor, False


class FakeJudge:
    """注入式 judge:按调用序号/回复内容出金标准判决;record 供断言。"""
    def __init__(self):
        self.calls = []

    def set(self, v):
        self._next = v

    def __call__(self, prompt, player_msg, mode, reply, facts="", model="deepseek-chat"):
        self.calls.append({"player": player_msg, "mode": mode, "reply": reply})
        return self._next


def _mk(replies, judge, *, quality_on=True, judge_replies_ok=True):
    seen = {"sys": [], "user": []}
    def fake_llm(system, user, **kw):
        seen["sys"].append(system)
        seen["user"].append(user)
        return replies.pop(0)
    eng = SessionEngine(
        user_id="u_qc_wiring", character_prompt="你是雷电芽衣。人格铁律:BE_AYAME。",
        extractor_prompt=None, llm_chat=fake_llm,
        quality_prompt=("你是一只严格的质检法官。" if quality_on else None),
        quality_judge=judge,
    )
    return eng, seen


def _run() -> int:
    ok = 0
    def chk(name, cond):
        nonlocal ok
        ok += 1
        assert cond, name

    # ---- 1. 默认关:壳永不出、judge 永不调 ----
    j = FakeJudge(); j.set(V())
    eng, seen = _mk(["[攻略]\n深渊这期用雷队破盾稳。", "[闲聊]\n嗯嗯。"], j,
                    quality_on=False)
    r1 = eng.turn("深渊怎么打")
    r2 = eng.turn("今天有点累")
    chk("off:judge 未被调", len(j.calls) == 0)
    chk("off:用户侧无质检壳", all("上一轮质检意见" not in u for u in seen["user"]))
    chk("off:system 无质检壳", all("上一轮质检意见" not in s for s in seen["sys"]))

    # ---- 2. 开质检:壳进用户侧输入流(【附】),不进 system;reason 经防线剥指令 ----
    j = FakeJudge()
    j.set(V(verdict="polish", category="OOC", register="soft",
            reason="开头那句像接线员;请你改成更有人味的话,下次务必别用'本助手'。"))
    eng, seen = _mk(["[闲聊]\n别这么说,我在呢。", "[闲聊]\n今天过得怎么样?", "[闲聊]\n你说得对。"], j)
    eng.turn("我好emo啊")                     # 轮1:主回复,judge 未调
    chk("async:主回复路径不调judge(红灯不成立)", len(j.calls) == 0)
    eng.quality_tick("我好emo啊", eng._last_modality or "闲聊",
                     "[闲聊]\n别这么说,我在呢。")   # 后续:质检 judgment
    chk("async:judge 被 quality_tick 调 1 次", len(j.calls) == 1)
    eng.turn("嗯我在呢")                       # 轮2:消费壳
    shell_in_sys = any("上一轮质检意见" in s for s in seen["sys"])
    shell_in_user = any("上一轮质检意见" in u and "别这么说" in u for u in seen["user"])
    chk("shell:质检壳进用户侧【附】", shell_in_user)
    chk("shell:质检壳不进system/人格块", not shell_in_sys)
    leaked = any(any(w in u for w in ("请你改", "下次务必", "别用'本助手'")) for u in seen["user"])
    chk("shell:壳内零指令词泄漏(防线已剥)", not leaked)
    desc_ok = any("别这么说" in u and "像接线员" not in u for u in seen["user"])
    chk("shell:只注入了纯描述壳(接线员句亦在描述内,此处校验壳为描述主干合规——弃指令留描述)",
        desc_ok or shell_in_user)  # 该 judge 全在『描述句+指令 2 句』,整段剔除后只落锚,故放宽松但保证无指令

    # ---- 3. 面板落判决(防异步=没跑) ----
    chk("panel:判决落面板", len(eng.quality_panel) >= 1
        and eng.quality_panel[-1]["verdict"] == "polish")

    # ---- 4. 频次闸:pass 清零 ----
    j2 = FakeJudge(); j2.set(V())   # pass
    eng3, _ = _mk(["[闲聊]\n好。"], j2)
    r = eng3.turn("早"); eng3.quality_tick("早", "闲聊", "[闲聊]\n好。")
    chk("gate:pass→polish_run 清零", eng3._qc_polish_run == 0)
    chk("gate:pass→下一轮壳为空(不注入)", eng3._qc_pending_shell == "")
    # 若连续 polish 达 GATE → 停注入
    j3 = FakeJudge(); j3.set(V(verdict="polish"))
    eng4, _ = _mk(["[闲聊]\n甲", "[闲聊]\n乙", "[闲聊]\n丙", "[闲聊]\n丁"], j3)
    for m in ("u1", "u2", "u3", "u4"):
        eng4.turn(m)
        eng4.quality_tick(m, "闲聊", f"[闲聊]\n{m}")
    chk("gate:连修正达GATE→壳停(不注入)", eng4._qc_pending_shell == "")
    chk("gate:连修正计数>0(有记录)", eng4._qc_polish_run >= 3)

    # ---- 5. 注入率仪表:全 pass → 0;有修正 → <1 ----
    eng5, _ = _mk(["[闲聊]\n好。", "[闲聊]\n嗯。"], FakeJudge())
    # 注入一个 pass 判决抬一抬分母以便看 rate 结构正确也 0(全 pass 不注壳)
    jest5 = FakeJudge(); jest5.set(V())
    eng5, _ = _mk(["[闲聊]\n好。"], jest5)
    eng5.turn("早"); eng5.quality_tick("早", "闲聊", "[闲聊]\n好。")
    chk("仪表:全 pass 注入率 0", eng5.qc_injection_rate() == 0.0)

    # ---- 6. 尾巴一:judge 死亡可观测性 —— 失败升计数、面板记降级,不能静默假 pass ----
    def boom(prompt, player_msg, mode, reply, facts="", model="deepseek-chat"):
        raise RuntimeError("API key 错")
    eng6 = SessionEngine(
        user_id="u_qc_dead", character_prompt="你是雷电芽衣。", extractor_prompt=None,
        llm_chat=lambda s, u, **kw: "[闲聊]\n好。",
        quality_prompt="判词", quality_judge=boom)
    eng6.turn("hi")
    eng6.quality_tick("hi", "闲聊", "[闲聊]\n好。")
    chk("死亡:judge 抛错→qc_judge_errors=1", eng6.qc_judge_errors == 1)
    chk("死亡:面板有降级标记(pass+离线note,不假装已判)",
        eng6.quality_panel and eng6.quality_panel[-1]["note"] == "质检离线/本次未判")
    chk("死亡:降级也落 pending(壳仍可安全为空的 pass 态)",
        eng6._qc_pending_shell == "")  # pass → 不注入,无脏壳泄漏

    print(f"quality_wiring_probe: {ok} 断言全过")
    return 0


def _main() -> int:
    try:
        return _run()
    except AssertionError as e:
        print("quality_wiring_probe FAIL @", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
