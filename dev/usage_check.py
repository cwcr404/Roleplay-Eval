# coding: utf-8
"""账目校验器（首府 2026-10-07 裁定 (a) 第 3 条）：改完 utils 后单跑 1 轮，
拿捕获数与控制台增量对账。对得上，(a) 才算过验收。

用法：
  1) 看 DeepSeek 控制台今日用量（记录 prompt/completion/cache 三数）
  2) 跑本脚本 → 打出捕获账
  3) 对比：捕获增量 == 控制台增量 → 通过

本脚本只跑 1 轮（不重跑 15 轮 —— 那场账目意义是验「零断点」，已验完）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["PYTHONIOENCODING"] = "utf-8"

# 记账落盘，便于逐轮核对
USAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "_usage_ledger.jsonl")
if os.path.exists(USAGE_FILE):
    os.remove(USAGE_FILE)
os.environ["DEEPSEEK_USAGE_JSONL"] = USAGE_FILE
os.environ["KB_DATA_ROOT"] = tempfile.mkdtemp(prefix="usage_check_")


def main() -> int:
    import utils.api as A
    from session.engine import build_session_engine

    A.usage_reset()
    eng = build_session_engine(f"u_usage_{int(time.time())}", enable_distill=False)

    print("=" * 70)
    print("账目校验器 · 单跑 1 轮")
    print("=" * 70)
    t0 = time.time()
    r = eng.turn("今天好累，不想说话")
    print(f"玩家: 今天好累，不想说话")
    print(f"芽衣: {r.reply}")
    print(f"耗时: {time.time() - t0:.1f}s")

    snap = A.usage_snapshot()
    print("\n捕获账（进程内）:")
    print(json.dumps(snap, ensure_ascii=False, indent=2))

    print("\n逐次调用（落盘 jsonl）:")
    if os.path.exists(USAGE_FILE):
        with open(USAGE_FILE, encoding="utf-8") as f:
            for line in f:
                print("  " + line.strip())
    else:
        print("  (无 —— 记账未落盘!)")

    print("\n" + "=" * 70)
    print("对账指引：本脚本增量 vs DeepSeek 控制台增量")
    print("  一致 → (a) 过验收，手工倒推销项")
    print("  不一致 → 记账有漏，回来查")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
