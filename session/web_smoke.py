# coding: utf-8
"""session/web_smoke —— §九 最后一格:真 DeepSeek 流式 + 质量判决真 web 冒烟(有限次)。

烧真 token(主回复×N + judge×N + 首字计时×2);每次跑是有界的一次性脚本,非巡检。
三个看点(维护者点名)逐一让它浮出来:
  ① 首块延迟 闲聊 vs 攻略【分开计时】(攻略允许更高,但要有数) —— 直接对
     utils.api.stream_chat 生成器量 next() 到达时点(引擎聚合就会丢首块时点)。
  ② 质检判决【真出现在面板】(不是只在日志) —— 每轮回复后 quality_tick,
     断言 panel 有该轮判落 + 打印 verdict/category/register/anchor/reason。
  ③ 注入后下一轮有无『过度矫正』味(防渐进讨好闸) —— 若某轮 judge 给了
     polish/blocked → 下轮回套固定壳注入;打印下轮回复供人眼对过度矫正。

兼报警门:qc_judge_errors 非零即报警(尾巴一:静默死亡可观测性)。

用法(项目根,真 .env):
  python -m session.web_smoke --turns 2      # 闲聊/攻略各起的有界轮数(默认2,勿开大)
环境:PYTHONIOENCODING=utf-8。退出码:0=see详情,且 qc_judge_errors==0 才真正 0。
"""
from __future__ import annotations

import argparse
import os
import time

base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(base, rel), encoding="utf-8") as f:
        return f.read()


def _first_token_lat(turn_user: str, char_sys: str) -> float:
    """对一条『闲聊/攻略』级的 user 输入量 model 的首块到达时点(生成器 next)。"""
    from utils.api import stream_chat
    gen = stream_chat(char_sys, turn_user, temperature=0.8)
    t0 = time.perf_counter()
    got_first = False
    for _ch in gen:  # 读到全量以让连接正常闭合(首块时点已在首次迭代内记)
        if not got_first:
            got_first = True
            t_first = time.perf_counter()
    # 若全程无块(理论上不):返回总耗时兜底
    return (time.perf_counter() - t0) if not got_first else (t_first - t0)


def _build():
    from session.engine import SessionEngine
    prompt = _read("prompts/agent2_character.md")
    quality = _read("prompts/agent3_async_verdict.md")
    eng = SessionEngine(
        user_id="u_web_smoke",              # 刮擦用户,不碰真人的 L1/对话链
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        character_prompt=prompt,
        extractor_prompt=None,              # 挤奶:不算书记员,冒烟只盯质检面板
        llm_chat=None,                      # 用真默认 utils.api.chat(同步兜底/judge 底层同源)
        llm_stream=None,                     # 下面覆为真 stream_chat
        distill_prompt=None,                # 不算 L2,控 token
    )
    # 真流式出口(产品线打字机通道):引擎聚合完成为完整可见回复,首块计时在外部单独量。
    from utils.api import stream_chat
    eng.llm_stream = stream_chat
    # 质检层开启:judge 走真默认(utils.api.chat);每轮回复后由本驱动手动 quality_tick
    # (web 的 /turn 冒烟口径) —— turn() 主回复路径永不内联 judge,防同步化回退红灯。
    eng.quality_prompt = quality
    eng.quality_judge = None                 # None -> _default_judge -> 真 DeepSeek
    return eng


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=2)
    a = ap.parse_args()
    turns = max(1, min(int(a.turns), 80))     # 有界上限,防手滑烧爆

    char_sys = _read("prompts/agent2_character.md")

    # ---- 看点① 首块延迟:闲聊 / 攻略 各一条,分开计时 ----
    print("== 看点① 首块延迟(真实流式 next) ==")
    casual_t = _first_token_lat("今天有点下雨,待在家有点闷。", char_sys)
    guide_t = _first_token_lat("深渊 12 层这期怎么配雷队?给点干货。", char_sys)
    print(f"  闲聊: {casual_t*1000:.0f} ms")
    print(f"  攻略: {guide_t*1000:.0f} ms")
    print(f"  (攻略允许更高是结构性取舍;本机网络抖动另计)")

    # ---- 看点②③:真引擎多轮,每轮回复后 quality_tick 落面板 ----
    print(f"\n== 看点②③ 真引擎 {turns} 轮 + 每轮 quality 判落面板 ==")
    eng = _build()
    script_messages = [
        "我刚做完一单评测,累得不想说话。",     # 闲聊
        "记忆战场这期雪狼小队的输出轴,能不能给个说人话的攻略?",  # 攻略
        "下班路上捡到一只小猫,跟了一路,现在它蹲我脚边不走了。",   # 闲聊
    ]
    run_msgs = script_messages * ((turns + 2) // 3)  # 循环,够 turns 轮
    run_msgs = run_msgs[:turns]
    for i, msg in enumerate(run_msgs):
        t0 = time.perf_counter()
        r = eng.turn(msg)                       # 真实流式出口 + 路由剥离标记
        turn_ms = (time.perf_counter() - t0) * 1000
        mode = eng._last_modality or "?"
        # 看点②核心:web 的 /turn 冒烟口径 —— 回复已定格后调 quality_tick
        eng.quality_tick(msg, mode, r.reply, facts="", model=eng.model)
        p = eng.quality_panel[-1]
        print(f"\n#{i+1} [{mode}] {turn_ms:.0f}ms 用户: {msg[:22]}…")
        print(f"   芽衣: {r.reply[:120]}{'…' if len(r.reply) > 120 else ''}")
        print(f"   [面板] {p['verdict']:7s}/{p['category']}/{p['register']}  reason={p['reason'][:80]!r} note={p['note'][:40]!r}")

    # ---- 收尾观测:注入率 / 死亡报警门 / 面板条数 ----
    print("\n== 收尾观测 ==")
    print(f"  质检生效轮数 _qc_turns_total = {eng._qc_turns_total}")
    print(f"  带壳轮数   _qc_turns_shelled = {eng._qc_turns_shelled}")
    print(f"  注入率 qc_injection_rate      = {eng.qc_injection_rate():.2f}  (常态应 <0.2)")
    print(f"  死亡报警门 qc_judge_errors    = {eng.qc_judge_errors}  (非零=静默降级!请看面板note)")
    print(f"  quality_panel 条数            = {len(eng.quality_panel)}")
    for i, p in enumerate(eng.quality_panel):
        print(f"    [{i}] {p['verdict']:7s} {p['category']}/{p['register']} "
              f"note={p['note'][:50]!r}")
    errs = eng.qc_judge_errors
    if errs:
        print("\n[!] qc_judge_errors 非零 —— 质检系统疑似在静默降级(配置错/路径错),需查!")
    return 1 if errs else 0


if __name__ == "__main__":
    raise SystemExit(main())
