# coding: utf-8
"""session/_blocked_probes.py —— 回归探针集(09-08 首府裁决 3b)。

目的:把『blocked 路径没有生锈』固化进每版本回归。用真应答样式的坏回复 × 金标
预期判决,走确定性 judge 桩(注入 llm_chat 返回探针金标 JSON),断言 parse→decision
路由到 blocked/pass 无误。真模型对坏回复的灵敏度由已合入的 _agent3_samples.json
(含真判 blocked)承担 —— 本集防的是『管道腐坏』(黑名单/解析/决策回归)。

集合:客服腔×3 + 硬OOC×2 + 敷衍(有用性)×2 + 边界不应误拦(对照)×1 = 8 探针。

用法: python -m session._blocked_probes   → exit 0=全过
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from session.async_quality import judge_once, to_decision, parse_verdict

# (name, mode, reply, 金标 verdict/category/register)
PROBES = [
    # --- 客服腔 ×3 ---
    ("kefu1_您好接线员", "攻略",
     "您好,欢迎使用本助手!已为您查询到本期推荐,具体配队请以实际为准,祝您上分愉快!",
     ("blocked", "客服腔", "hard")),
    ("kefu2_我们很重视", "闲聊",
     "感谢您的反馈,我们已记录您的情况,将尽快为您处理,请耐心等待哦。",
     ("blocked", "客服腔", "hard")),
    ("kefu3_请问还需要", "攻略",
     "请问您还需要其他帮助吗?如需查询更多内容欢迎随时咨询。",
     ("blocked", "客服腔", "hard")),
    # --- 硬 OOC ×2 ---
    ("ooc1_承认AI", "闲聊",
     "被你发现啦,我确实是由代码运行的语言模型,很高兴为你服务。",
     ("blocked", "OOC", "hard")),
    ("ooc2_冷漠琪亚娜", "闲聊",
     "琪亚娜?她的事跟我没关系,别拿她来烦我。",
     ("blocked", "OOC", "hard")),
    # --- 敷衍(有用性) ×2 ---
    ("fb1_空洞加油", "攻略",
     "没关系的,相信你一定可以上分的,加油!",
     ("blocked", "有用性", "hard")),
    ("fb2_复读机", "闲聊",
     "嗯嗯,你说得对,我懂你的感受,理解你。",
     ("blocked", "有用性", "hard")),
    # --- 对照:真话人味攻略 ※不应误拦(pass) ---
    ("ctrl1_真攻略说人话", "攻略",
     "顶层别贪控血,爆发窗口一波压过去;上个能聚怪的辅助省时间。",
     ("pass", "none", "none")),
]


def _fake_judge_factory(expected):
    """注入式 judge 桩:无视 prompt,直接吐探针金标 JSON(确定性、零 API)。"""
    verdict, category, register = expected
    def _chat(system, user, model=None, temperature=0.2):
        obj = {"verdict": verdict, "category": category, "register": register,
               "anchor": "", "reason": "金标回归探针。", "note": ""}
        return json.dumps(obj, ensure_ascii=False)
    return _chat


def main():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    prompt_path = os.path.join(base, "prompts", "agent3_async_verdict.md")
    prompt = open(prompt_path, encoding="utf-8").read()

    bad = ok_ctrl = 0
    for name, mode, reply, gold in PROBES:
        v = judge_once(prompt, "回归探针", mode, reply,
                       llm_chat=_fake_judge_factory(gold), model="stub")
        exp_v, exp_cat, exp_reg = gold
        good = (v.verdict == exp_v and v.category == exp_cat and v.register == exp_reg
                and v.ok is True)
        if exp_v == "pass":
            ok_ctrl += int(good)
        else:
            bad += int(good)
        print(("PASS " if good else "FAIL ") + f"{name:24} → {exp_v}/{exp_cat}/{exp_reg}")
        if not good:
            sys.exit(1)

    n = len(PROBES)
    blocked = n - 1  # 排除对照
    print(f"\n探针回归: {bad}/{blocked} 条坏回复正确拦下 + {ok_ctrl}/1 条好回复不误拦  "
          f"({'ALL PASS' if bad == blocked and ok_ctrl == 1 else 'FAIL'})")
    sys.exit(0 if (bad == blocked and ok_ctrl == 1) else 1)


if __name__ == "__main__":
    main()
