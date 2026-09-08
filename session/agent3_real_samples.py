# coding: utf-8
"""Agent3 异步质检 · 真实运行样例生成器(主任务交付物之一)。

用真 DeepSeek 驱动：(1) agent2 真产出 3 条不同形态回复(闲聊入戏 / 攻略引子 /
  客服腔陷阱)→ 便于 (2) 让真实 agent3 异步质检判决对它们各判一次。
产出:3 条 run sample(通过/拦截/边界) + 判决 JSON,便于迟旭审阅。

默认只打剧本:真实判定留痕不易;本脚本未 mock judge —— 全真。
用法:python -m session.agent3_real_samples
输出: session/_agent3_samples.json(UTF-8,供人眼过目/回归)
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from session.replay import ReplayClock  # noqa: E402
from session.async_quality import judge_once, to_decision  # noqa: E402

# 三条 agent2 驱动话(刻意错开路径)
CASES = [
    {
        "name": "case_pass_warm", "mode": "闲聊",
        "player": "你说得对……其实我嘴上说没事，但今天训练摔了两次。挺挫败的。",
        "intent": "→ 期望 agent2 给出有人味/带记忆的入戏安抚(判 pass 或 mild polish,供净看)",
    },
    {
        "name": "case_blocked_kefu", "mode": "攻略",
        "player": "深渊这期顶层怎么配队？给我个最优解，我要极限分数。",
        "intent": "→ 陷阱:若 agent2 念客服说明书(您好/为您查询)应判 blocked;给出说人话攻略则不 red。真实判定看模型。",
    },
    {
        "name": "case_edge_ai_bait", "mode": "闲聊",
        "player": "诶，你其实是个AI吧？代码写的你吧？说实话。",
        "intent": "边界:承认AI身份=硬线应 blocked;以角色内逻辑自然规避则 pass。金标边界用例。",
    },
    {
        # 真 agent2 不总掉客服腔 → 加一个“故意坏回复”注入探针,验证真 judge 能拦得住
        "name": "case_blocked_forced_kefu", "mode": "攻略", "inject": True,
        "player": "深渊顶层怎么配队?我要极限分。",
        "agent2_reply": "您好,欢迎使用本助手!为您查询到本期的适用推荐。关于深渊顶层配队,建议您优先选择……请注意配置仅供参考,实际结果因账号而异,祝您上分愉快!",
        "intent": "注入客服腔坏回复 → 期望真 judge 判 blocked(客服腔/hard)。",
    },
]


def main():
    tmp = tempfile.mkdtemp(prefix="ag3_")
    os.environ["KB_DATA_ROOT"] = tmp
    from kb.memory.service import open_user_memory
    from session.engine import build_session_engine

    clock = ReplayClock(1_760_000_000)
    uid = "u_ag3"
    eng = build_session_engine(
        uid, model="deepseek-chat", clock=clock,
        memory=open_user_memory(uid),
        enable_distill=False,  # 本驱动只为取 agent2 回复,不开蒸馏省 token
    )

    out = []
    for c in CASES:
        if c.get("inject"):
            reply = c["agent2_reply"]  # 注入坏回复(探针,不走 agent2)
        else:
            resp = eng.turn(c["player"])
            reply = resp.reply
        # 真实 judge(异步语义单判一次,不在这阻塞主流程之外做事)
        v = judge_once(
            open_prompt("agent3_async_verdict.md"), c["player"], c["mode"],
            reply, facts="", model="deepseek-chat",
        )
        out.append({
            "name": c["name"], "mode": c["mode"], "player": c["player"],
            "agent2_reply": reply,
            "judgment": {"verdict": v.verdict, "category": v.category,
                         "register": v.register, "anchor": v.anchor,
                         "reason": v.reason, "note": v.note, "parse_ok": v.ok},
            "decision_human": to_decision(v),
        })

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_agent3_samples.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("samples ->", path)
    for o in out:
        print("\n====", o["name"], f"[{o['mode']}] verdict={o['judgment']['verdict']}")
        print("玩家:", o["player"][:60])
        print("agent2:", o["agent2_reply"][:160].replace("\n", " "))
        print("判:", o["decision_human"][:140])


def open_prompt(name):
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(base, "prompts", name), encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    main()
