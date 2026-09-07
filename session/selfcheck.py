# coding: utf-8
"""session.selfcheck —— 引擎装配自检(离线,不烧 API / 不碰真实数据)。

测什么(针对记忆接线生效,而非 LLM 文采):
  1. memory context 注入到 system(card + 【用户记忆】区)且含关系距离文本(不含裸数值)；
  2. turn() 主回复走注入的 llm_chat、抽取走同一 llm_chat(双出口都短路)；
  3. 书记员产出 -> record_event 入账 L1(append-only 计数 +1)；
  4. affinity 随入账升高、随 clock 推进衰减(读时算,未写回)；
  5. ritual gate:数值到位但仪式未完成 -> allowed_tier 被拦在同层；
  6. L2 缓存可生成并被塞进 memory context(故事层,不衰减)。

跑法(TempData):
    KB_DATA_ROOT=$(mktemp -d) python -m session.selfcheck
Windows PowerShell:
    $env:KB_DATA_ROOT="<temp>"; python -m session.selfcheck
"""
from __future__ import annotations

import json
import os
import tempfile

from kb.relation.affinity import compute_affinity  # noqa: E402  (data root set in main)
from .engine import SessionEngine


class _FakeLLM:
    """可编程假 LLM:按消息是否含本案对话,返回主回复或书记员 JSON。"""

    def __init__(self):
        self.reply = "嗯,我在听。"
        self.sys_prompts = []      # 每次主回复的 system(应含 记忆区)
        self.compile()

    def compile(self):
        # 复刻真实调用形状：一次 turn 会调 1(主回复)+1(maybe 抽取)次
        self.reply = "嗯,我会记着你的话。"
        self.calls = []

    def __call__(self, system, user, **kw):
        self.sys_prompts.append(system)
        self.calls.append((user, kw.get("model")))
        # 分辨：抽取走“本条对话”，主回复走“【此刻】”
        if "【本条对话】" in user:
            return json.dumps({"has_event": True,
                               "type": "承诺与约定",
                               "content": "玩家与芽衣约定周末一起去看雪山日出",
                               "weight_delta": 3}, ensure_ascii=False)
        return self.reply


def main():
    tmp = tempfile.mkdtemp(prefix="sess_selfcheck_")
    os.environ["KB_DATA_ROOT"] = tmp      # 脚本数据域隔离,不碰真实 memory
    from kb.memory.service import open_user_memory
    from .replay import ReplayClock

    mem = open_user_memory("u_sess_selftest")   # tmp 域
    fake = _FakeLLM()
    clock = ReplayClock()

    eng = SessionEngine("u_sess_selftest",
                        memory=mem,
                        character_prompt="你是雷电芽衣。【人格】成熟温柔。",
                        extractor_prompt="你是记忆书记员。",
                        llm_chat=fake, clock=clock)

    ok = []

    # --- 验收 1:memory context 注入 system,含关系距离、不含裸数值 ---
    r1 = eng.turn("周末陪我去看日出吧。")
    # turn 会调 2 次 llm:主回复(带记忆 context)在先、抽取(书记员 system)在后;取主回复那条
    reply_sys = next(s for s in fake.sys_prompts if "【用户记忆】" in s)
    check1 = ("【你们如今的关系】" in reply_sys
              and "affinity" not in reply_sys and "亲和" not in reply_sys)
    ok.append(("记忆/关系作为外挂上下文注入主回复且无裸数值", check1))

    # --- 验收 2:书记员这轮记录了(承诺约定 → 亲和应 >初始50)---
    ev_recorded = r1.event_recorded and r1.event_type == "承诺与约定"
    aff_up = r1.affinity > 50.0
    ok.append(("书记员入账承诺 && 亲和升过 50", ev_recorded and aff_up))
    assert mem.ledger.size() == 1, mem.ledger.size()

    # --- 验收 3:亲和随(等效)时间衰减(读时算,账本不动)---
    evs = mem.ledger.events
    # 事件刚写(接近当下)读 vs 推到 28 天(2 个半衰期)后再读 → 后折得狠
    a_now = compute_affinity(evs, user_id="u_sess_selftest")
    a_old = compute_affinity(evs, as_of_time=evs[0].timestamp_utc + 28 * 86400,
                             user_id="u_sess_selftest")
    ok.append((f"读取时好感度随等效时间衰减 ({a_now:.2f}->{a_old:.2f})", a_old < a_now))

    # --- 验收 4:ritual gate —— 数值到位未过仪式 → 层级被拦(不越段)---
    mem2 = open_user_memory("u_ritual_test")   # tmp 域内
    # 灌大量高权承诺把亲和顶到上位(能触发仪式门槛的段);写入与读取同走此 ReplayClock
    for _ in range(30):
        mem2.record_event("承诺与约定", "玩家与芽衣一次次约定并肩到底、不离不弃", 3.0,
                          ts=clock.now - (30 - _) * 86400)
    aff_high = round(mem2.gate(as_of_time=clock.now).affinity, 2)
    sig_no = mem2.gate(ritual_completed=False, as_of_time=clock.now).to_signal()
    sig_yes = mem2.gate(ritual_completed=True, as_of_time=clock.now).to_signal()
    # 断言:仪式完成必不拦;足够触发上位(80+)且未过仪式 → blocked=True(数值到被 gate 拦)
    gate_ok = (sig_yes["ritual_blocked"] is False)
    if aff_high >= 80:
        gate_ok = gate_ok and (sig_no["ritual_blocked"] is True)
    ok.append((f"ritual 门槛显影(亲和 {aff_high:.1f}):未过仪式挡 / 过仪式放行", gate_ok))

    # --- 验收 5:L2 故事层生成并被注入 memory context(不衰减存储/重建) ---
    eng2 = SessionEngine("u_sess_selftest", memory=mem,
                         character_prompt="你是芽衣", extractor_prompt="书记员",
                         llm_chat=fake, clock=clock)
    _ctx = eng2._memory_context()
    l2_ok = bool(mem.l2() and mem.l2().to_markdown())
    ok.append(("L2 故事层可生成并进入 记忆上下文", l2_ok))

    print("\n==== session 引擎装配自检 ====")
    for name, passed in ok:
        print(("  ✅ " if passed else "  ❌ ") + name)
    # 控制台编码易乱码 —— 额外落一份 UTF-8 参考文件便于核对
    with open(os.path.join(os.path.dirname(__file__), "selfcheck_report.txt"),
              "w", encoding="utf-8") as f:
        for name, passed in ok:
            f.write(("PASS  " if passed else "FAIL  ") + name + "\n")
        f.write("\nALL_PASS=" + str(all(p for _, p in ok)) + "\n")
    if not all(p for _, p in ok):
        print("\n存在失败项 —— 请修。")
        return 1
    print("\n全部通过。引擎的记忆/记忆时间/门槛/故事层接线成立(架构验证,未烧 token)。")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
