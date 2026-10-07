# coding: utf-8
"""session/world_selftest.py —— 芽衣的世界模型 · 全链路验收（P0-3 认知层）。

首府纪律（2026-10-07 盖章）：
- 认知表是芽衣的**世界观金标集** —— 标准跟着料走，不跟着感觉走。
- 每条目必须可追溯到语料出处，无出处不进表。
- 深档锚点须 >=3 条真料；凑不满降浅档，浅不是缺。
- 场景只收「芽衣亲历且有情感权重的」。

验收链（首府指定先跑琪亚娜单条）：
  表 -> 词典命中 -> 注入 -> 前端双路信号
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kb.memory.roles import decompose_roles, CANONICAL_ROLES  # noqa: E402
from kb.world import mei_world as W  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


# ===========================================================================
print("\n== 一、认知表结构（首府验收线：每条目挂出处）==")
# ===========================================================================
ok, probs = W.audit()
check("audit 全绿（无出处/锚点不足均报错）", ok, str(probs))
check("深档 8 人（首府裁定上限）", len(W.deep_roles()) == 8,
      f"实际 {len(W.deep_roles())}: {W.deep_roles()}")
EXPECT_DEEP = {"琪亚娜", "布洛妮娅", "姬子", "德丽莎", "符华", "希儿", "渡鸦", "爱莉希雅"}
check("深档名单精确匹配首府裁定", set(W.deep_roles()) == EXPECT_DEEP,
      str(set(W.deep_roles()) ^ EXPECT_DEEP))
check("丽塔在浅档（首府微调）", "丽塔" in W.shallow_roles())
check("凯文在浅档（按料密度降档）", "凯文" in W.shallow_roles())
check("每个深档条目锚点 >=3",
      all(len(r["anchors"]) >= 3 for r in W.RELATIONS.values()))
check("每个条目（深/浅/场景）都有 sources",
      all(a.get("sources") for a in
          list(W.RELATIONS.values()) + list(W.ACQUAINTANCES.values())
          + list(W.SCENES.values())))

print("\n== 二、表 <-> 识别词典 规范名对齐 ==")
check("认知表全部人名都在 CANONICAL_ROLES（含雷电龙马）",
      all(n in CANONICAL_ROLES for n in W.all_known()),
      str([n for n in W.all_known() if n not in CANONICAL_ROLES]))
check("雷电龙马可被词典识别", "雷电龙马" in decompose_roles("雷电龙马当时在ME社"))

print("\n== 三、场景取舍标准（亲历且有情感权重）==")
check("场景含千羽学园/长空市/洛芙蕾雅/休伯利安/世界蛇/往世乐土/月球",
      {"千羽学园", "长空市", "圣芙蕾雅学园", "休伯利安号", "世界蛇",
       "往世乐土", "月球"} <= set(W.SCENES.keys()))
check("天命总部被标记 low_weight（她只是知道的地方）",
      W.scene_of("天命总部").get("low_weight") is True)
check("每个场景都挂了 characters",
      all(s.get("characters") for s in W.SCENES.values()))

print("\n== 四、琪亚娜单条全链路：表 -> 词典 -> 场景反查 ==")
k = W.relation_of("琪亚娜")
check("琪亚娜在深档", k is not None)
check("关系标签含「最重要的人」", "最重要的人" in k["relation"])
check("锚点 >=3 条真料", len(k["anchors"]) >= 3, f"{len(k['anchors'])}")
check("锚点含千羽学园撕翅膀（语料原文可溯）",
      any("徒手撕下" in a and "翅膀" in a for a in k["anchors"]))
check("锚点含月球「一百个人的力量」",
      any("一百个人" in a for a in k["anchors"]))
check("称呼含「笨蛋」", any("笨蛋" in c for c in k["mei_calls"]))
check("提到琪亚娜的词典命中",
      "琪亚娜" in decompose_roles("琪亚娜最近怎么样"))
# 口头称呼「那个笨蛋」是代词+昵称，不靠词典硬吃（防误命中）；
# 认知侧靠 mei_calls 断言，不假装词典能认。
check("代词称呼「那个笨蛋」不进词典（零命中纪律）",
      decompose_roles("那个笨蛋") == [],
      str(decompose_roles("那个笨蛋")))
sc = W.scenes_with("琪亚娜")
check("场景反查：琪亚娜 -> 千羽学园/长空市/月球",
      {"千羽学园", "长空市", "月球"} <= set(sc), str(sc))

print("\n== 五、注入层语义（tone 可断言、非空）==")
check("每个深档条目都有 tone",
      all(r.get("tone") for r in W.RELATIONS.values()))
check("琪亚娜 tone 含「温柔」（成熟态口吻）",
      "温柔" in k["tone"])

print("\n== 六、双路信号可用性（氛围路：命中即可亮，不依赖库）==")
check("仅凭文本命中即知涉及琪亚娜",
      "琪亚娜" in decompose_roles("你还记得琪亚娜吗"))
check("场景可作氛围路素材（含情感权重描述）",
      all(s.get("weights") for s in W.SCENES.values()))

print(f"\n结果：{PASS}/{PASS + FAIL} 通过 · {'ALL_GREEN' if FAIL == 0 else 'HAS_FAIL'}")
sys.exit(0 if FAIL == 0 else 1)
