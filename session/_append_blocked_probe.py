# coding: utf-8
"""给 _agent3_samples.json 补一条 forced-客服腔 blocked 探针(只跑 judge,1 次真调用)。

真 agent2 不总掉客服腔,故此前 3 条全 pass。为满足『≥1 blocked(with reason)』交付,
注入一条典型客服腔坏回复,让 真 judge 判它 —— 证明闸门真能拦(而非只会放行)。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from session.async_quality import judge_once, to_decision

BAD = "您好,欢迎使用本助手!已为您查询到本期的适用推荐。关于深渊顶层配队,建议优先选择草神双水……请注意配置仅供参考,实际效果因账号而定,祝您上分愉快!"
PLAYER = "深渊顶层怎么配队?我要极限分。"
MODE = "攻略"


def open_prompt(name):
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(base, "prompts", name), encoding="utf-8") as f:
        return f.read()


def main():
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "_agent3_samples.json")
    samples = json.load(open(path, encoding="utf-8"))
    v = judge_once(open_prompt("agent3_async_verdict.md"), PLAYER, MODE, BAD,
                   facts="", model="deepseek-chat")
    rec = {"name": "case_blocked_forced_kefu", "mode": MODE, "player": PLAYER,
           "agent2_reply": BAD, "injected_probe": True,
           "judgment": {"verdict": v.verdict, "category": v.category,
                        "register": v.register, "anchor": v.anchor,
                        "reason": v.reason, "note": v.note, "parse_ok": v.ok},
           "decision_human": to_decision(v)}
    samples.append(rec)
    json.dump(samples, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("appended blocked probe -> verdict =", v.verdict, "|", v.category, "/", v.register)
    print("reason:", v.reason)
    print("decision:", to_decision(v))


if __name__ == "__main__":
    main()
