# coding: utf-8
"""chat_session —— 记忆/关系会话引擎 顶层入口(单引擎,双前端)。

架构:
    session/engine.py      核心引擎(SessionEngine)—— 引擎本身只有一个,喂什么
                           user_id、走哪只时钟、用哪套前端,都由使用方决定。
    前端(都只消费 SessionEngine):
      chat               CLI 手动聊(终端)
      replay             replay 剧本回放(可注入时钟,见灰恋衰减验收)
      web                stdlib 单页 demo(不引第三方依赖,仅演示/内网用)

记忆/关系真正在此落地 —— run_eval 是有状态评测禁地,真实多轮会话在这条链上:
每次说话 → 装配记忆上下文进角色卡【用户记忆】区 → Agent2 生成芽衣的话 →
书记员(独立)把值得记的写进 L1 账本 → 好感度读取时算,距离由关系描述折算,
绝不把裸数值塞进推理路径。

用法:
    python chat_session.py chat   [--user u_name]
    python chat_session.py replay scripts/replay_accept.jsonl [--noextract]
    python chat_session.py web    [--port 8765] [--user u_name]
"""
from __future__ import annotations

import argparse
import os
import sys

from session.engine import build_session_engine, SessionEngine


def _engine(user_id: str, clock=None, extract_ok=True) -> SessionEngine:
    eng = build_session_engine(user_id, clock=clock)
    return eng


# ---------------- CLI 手动聊（前端之一） ----------------
def cmd_chat(args) -> int:
    from session.engine import build_session_engine
    from kb.retriever import build_kb  # 产品线 CLI:真 KB 预查(0908第二块案A,维护者今日拍板)
    kb = build_kb(retriever_mode=os.getenv("KB_MODE", "plain"))
    eng = build_session_engine(args.user, kb=kb)
    print(f"芽衣会话已开 (user={args.user} · KB已接) | 输入 bye 退出, /reset 清空历史\n")
    try:
        while True:
            try:
                msg = input("你> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见。")
                return 0
            if not msg:
                continue
            if msg.lower() in ("bye", "exit", "退出"):
                print("芽衣> 路上小心。")
                return 0
            if msg == "/reset":
                eng.reset()
                print("芽衣> (这段就当风带走了,我们重新认识。)")
                continue
            r = eng.turn(msg)
            print(f"\n芽衣> {r.reply}\n")
            print(f"      [亲和 {r.affinity} · {r.tier_key}"
                  + (" · ⚠仪式未过" if r.ritual_blocked else "") + "]")
    except Exception as e:  # noqa: BLE001
        print(f"[会话异常] {e}")
        return 1


# ---------------- replay 回放（前端之二） ----------------
def cmd_replay(args) -> int:
    from session.replay import load_replay, run_replay, narrate
    eng = build_session_engine(args.user)
    turns = load_replay(args.script)
    _, log = run_replay(eng, turns, extract=not args.noextract)
    narrate(log)
    print("\n[最终状态]", end=" ")
    import json
    print(json.dumps(eng.state(), ensure_ascii=False))
    return 0


# ---------------- stdlib web demo（前端之三,单用户演示） ----------------
def cmd_web(args) -> int:
    from session.web import run_web
    eng = build_session_engine(args.user)
    run_web(eng, host=args.host, port=args.port)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="chat_session",
                                description="芽衣 记忆/关系会话引擎")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("chat", help="CLI 手动聊")
    pc.set_defaults(func=cmd_chat)

    pr = sub.add_parser("replay", help="剧本回放(可注入时钟)")
    pr.add_argument("script")
    pr.add_argument("--noextract", action="store_true",
                    help="只回放对话,不跑书记员抽取(省 token)")
    pr.set_defaults(func=cmd_replay)

    pw = sub.add_parser("web", help="stdlib demo web")
    pw.add_argument("--host", default="127.0.0.1")
    pw.add_argument("--port", type=int, default=8765)
    pw.set_defaults(func=cmd_web)

    for p_ in (pc, pr, pw):
        p_.add_argument("--user", default=os.getenv("AYAME_USER_ID", "u_chixu"))

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
