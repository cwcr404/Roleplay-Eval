"""kb 检索层自检 —— 不调 API,纯本地验证解析与检索逻辑。

用法:
    python -m kb.selfcheck   (或 python kb/selfcheck.py)
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

from kb.retriever import build_kb  # noqa: E402


def main() -> int:
    kb = build_kb("plain")
    guide = kb["guide"]
    lore = kb["lore"]

    print(f"[kb] V1 攻略条目数: {guide.size}")
    print(f"[kb] V2 背景块数:   {lore.size}")
    assert guide.size > 0, "V1 没解析出任何条目"
    assert lore.size > 0, "V2 没解析出任何块"

    # 1) V1: 雷之律者 - 定位
    q1 = "雷之律者是什么属性什么定位"
    res = guide.query(q1)
    print("\n=== V1 检索: %r ===" % q1)
    for r in res:
        print(f"  · [{r.key}] ({r.domain}) len={len(r.content)}")
    assert res, "V1 雷之律者 无命中"

    # 2) V1: 武器/圣痕系
    q2 = "真理之钥是谁的武器"
    res2 = guide.query(q2)
    print("\n=== V1 检索: %r ===" % q2)
    for r in res2:
        print(f"  · [{r.key}] ({r.domain})")
    assert any("真理之钥" in r.content for r in res2)

    # 3) V2: 琪亚娜相关背景(整块)
    q3 = "你和琪亚娜最开始是怎么认识的"
    lore3 = lore.query(q3)
    print("\n=== V2 检索: %r (块数=%d) ===" % (q3, len(lore3)))
    for r in lore3:
        print(f"  · [{r.key}] ({r.domain}) len={len(r.content)}")
    assert lore3 and any("千羽学园" in r.content for r in lore3), "V2 琪亚娜缘分点未命中"

    # 4) V2: 往世乐土
    q4 = "往世乐土里你见到了谁 学到了什么"
    lore4 = lore.query(q4)
    print("\n=== V2 检索: %r (块数=%d) ===" % (q4, len(lore4)))
    for r in lore4:
        print(f"  · [{r.key}] ({r.domain}) len={len(r.content)}")

    print("\n[OK] kb 自检全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
