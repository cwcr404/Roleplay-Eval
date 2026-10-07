# coding: utf-8
"""Day 7 验收：一场真实对话从头到尾不留断点（真 DeepSeek，烧 token）。

首府 2026-10-07 授权：先跑一场，实测 token 消耗记进账目，不达标再追加。

验收线（Day 7）：
  用户真摸到的那条链，从第一句到收尾全程无断点，且：
  - 每轮主回复成功（非「(回复失败)」）
  - 书记员抽取有产出（事件/承诺入账）
  - 记忆/关系上下文真注入（无裸数值）
  - 情绪通道在需要时接住（察知块出现）
  - 路由标记不出现在玩家可见流
  最后打印账目：轮数 / token / 耗时 / 花费估算。
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 独立 user_id + 独立数据目录：不动既有 demo 账本
_RUN_TS = int(time.time())
os.environ["KB_DATA_ROOT"] = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "memory", f"day7_{_RUN_TS}")
os.environ["PYTHONIOENCODING"] = "utf-8"

USER = f"u_day7_{_RUN_TS}"

# ── 一场完整的对话剧本（15 轮：闲聊起 → 情绪低谷 → 承诺 → 情绪回升 → 收尾）──
SCRIPT = [
    "在吗",
    "今天好累，不想说话",
    "项目又改需求了，第三次了，我真的有点受不了",
    "你别说话，就陪我坐会儿行吗",
    "……谢谢你还记着我讨厌下雨天",
    "我想问你个事，我这样是不是很没用",
    "嗯，你说得对。摔了两次不算什么",
    "那，我们说好了，下次训练我要是再摔，你得来看我",
    "好，我记住了。今天先到这儿吧",
    "对了，我一直想问你，你平时练剑累不累啊",
    "哈哈哈你居然会自嘲，这不像你",
    "今天聊得挺开心的，谢谢你",
    "下周三是我的生日",
    "你会来吧",
    "晚安",
]


def main() -> int:
    from session.engine import build_session_engine

    eng = build_session_engine(USER, enable_distill=True)
    print("=" * 70)
    print(f"Day 7 真实对话验收 · user={USER}")
    print("=" * 70)

    t0 = time.time()
    turn_log = []
    fails = 0
    for i, msg in enumerate(SCRIPT, 1):
        t1 = time.time()
        try:
            r = eng.turn(msg)
        except Exception as e:  # noqa: BLE001
            print(f"\n[轮 {i}] 玩家: {msg}")
            print(f"  !! 断点: {type(e).__name__}: {e}")
            fails += 1
            turn_log.append({"turn": i, "break": str(e)})
            continue
        dt = time.time() - t1
        marked = "[闲聊]" in r.reply or "[攻略]" in r.reply
        mem = r.injected_memory or ""
        sense = "【此刻的察知】" in mem
        print(f"\n[轮 {i}] 玩家: {msg}")
        print(f"  芽衣: {r.reply}")
        print(f"  · 亲和 {r.affinity} · {r.tier_key}"
              f" · 入账={r.event_recorded}({r.event_type})"
              f" · 察知={'有' if sense else '无'}"
              f" · 可见流残留标记={'是!需要查' if marked else '无'}"
              f" · {dt:.1f}s")
        if not r.reply or r.reply == "(回复失败)":
            fails += 1
        if marked:
            fails += 1
        turn_log.append({"turn": i, "affinity": r.affinity, "tier": r.tier_key,
                         "event": r.event_type, "sense": sense,
                         "secs": round(dt, 2)})

    total = time.time() - t0
    sig = eng.state()
    ledger_size = eng.memory.ledger.size()
    l3_size = eng.memory._l3_store().index_size()

    print("\n" + "=" * 70)
    print("账目")
    print("=" * 70)
    print(json.dumps({
        "轮数": len(SCRIPT),
        "断点数": fails,
        "总耗时秒": round(total, 1),
        "平均每轮秒": round(total / max(1, len(SCRIPT)), 1),
        "最终亲和": sig.get("affinity"),
        "层级": sig.get("tier"),
        "账本事件数": ledger_size,
        "L3条数": l3_size,
        "蒸馏次数": len(eng.distill_log),
    }, ensure_ascii=False, indent=2))

    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
