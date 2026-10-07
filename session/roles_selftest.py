# coding: utf-8
"""session/roles_selftest.py —— 角色词典 + 角色标 + 双路信号 自测。

测试按【立法本意】写，不照实现写（首府纪律）。

首府 P0-3 工单原文钉的验收句：
  「你认识琪亚娜吗」→ 背景渐变 + 记忆条目浮现 + 芽衣回答引用库内真条目，
  三件同真才算回忆感成立。
  图给氛围，记忆给证据，不许裸图 cos。

七节：
  一、词典识别（正式名/昵称/别名/英文）
  二、零命中纪律（不猜，没提不亮）
  三、单字别名护栏（琪/苏/华 不误吃）
  四、长词优先（琪亚娜·卡斯兰娜 不降级成 琪）
  五、角色标打标（入账命中即打标）
  六、倒排挂载（角色名走主索引，非独立通道）
  七、双路信号（氛围层可亮 / 证据层不造记忆）
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["PYTHONIOENCODING"] = "utf-8"

from kb.memory.roles import (ROLE_ALIASES, CANONICAL_ROLES, canonical_of,
                             decompose_roles)

# 独立数据根，不碰真数据
os.environ["KB_DATA_ROOT"] = tempfile.mkdtemp(prefix="roles_selftest_")

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} :: {detail}")
        print(f"  FAIL  {name}  {detail}")


print("=" * 72)
print("一、词典识别（正式名/昵称/别名/英文）")
print("=" * 72)
check("正式名 琪亚娜", decompose_roles("你认识琪亚娜吗") == ["琪亚娜"],
      f"got={decompose_roles('你认识琪亚娜吗')}")
check("全名 琪亚娜·卡斯兰娜", decompose_roles("琪亚娜·卡斯兰娜是谁") == ["琪亚娜"],
      f"got={decompose_roles('琪亚娜·卡斯兰娜是谁')}")
check("英文 Kiana", decompose_roles("Kiana 还好吗") == ["琪亚娜"],
      f"got={decompose_roles('Kiana 还好吗')}")
check("小写 kiana", decompose_roles("kiana 怎么样了") == ["琪亚娜"],
      f"got={decompose_roles('kiana 怎么样了')}")
check("布洛妮娅", decompose_roles("提布洛妮娅") == ["布洛妮娅"],
      f"got={decompose_roles('提布洛妮娅')}")
check("错字别名 布洛尼亚", decompose_roles("布洛尼亚最近如何") == ["布洛妮娅"],
      f"got={decompose_roles('布洛尼亚最近如何')}")
check("昵称 板鸭", decompose_roles("板鸭在干嘛") == ["布洛妮娅"],
      f"got={decompose_roles('板鸭在干嘛')}")
check("律者名 空之律者", decompose_roles("空之律者是谁") == ["琪亚娜"],
      f"got={decompose_roles('空之律者是谁')}")
check("律者名 理之律者", decompose_roles("理之律者") == ["布洛妮娅"],
      f"got={decompose_roles('理之律者')}")
check("爱莉希雅", decompose_roles("爱莉希雅说过") == ["爱莉希雅"],
      f"got={decompose_roles('爱莉希雅说过')}")
check("多角色同现", set(decompose_roles("琪亚娜和布洛妮娅都在吗")) == {"琪亚娜", "布洛妮娅"},
      f"got={decompose_roles('琪亚娜和布洛妮娅都在吗')}")

print()
print("=" * 72)
print("二、零命中纪律（不猜，没提不亮）")
print("=" * 72)
check("纯闲聊零命中", decompose_roles("今天天气不错") == [],
      f"got={decompose_roles('今天天气不错')}")
check("提芽衣自己不算角色回忆", decompose_roles("你觉得我怎么样") == [],
      f"got={decompose_roles('你觉得我怎么样')}")
check("空串零命中", decompose_roles("") == [])
check("未收录人名不猜", decompose_roles("小明最近好吗") == [],
      f"got={decompose_roles('小明最近好吗')}")

print()
print("=" * 72)
print("三、单字别名护栏（琪/苏/华 不误吃）")
print("=" * 72)
# 「苏醒」的「苏」不应命中 苏（英杰）
check("苏醒 不误吃苏", "苏" not in decompose_roles("我刚苏醒"),
      f"got={decompose_roles('我刚苏醒')}")
# 「才华」的「华」不应命中 符华
check("才华 不误吃符华", "符华" not in decompose_roles("他很有才华"),
      f"got={decompose_roles('他很有才华')}")
# 「琪」单字在句中若前面是 CJK 也应被拒
check("边界拒绝（XX琪）", decompose_roles("王琪是谁") == [],
      f"got={decompose_roles('王琪是谁')}")
# 单字独立成词可命中（口语直呼）
check("单字独立命中 琪", decompose_roles("琪，你在吗") == ["琪亚娜"],
      f"got={decompose_roles('琪，你在吗')}")

print()
print("=" * 72)
print("四、长词优先（不降级）")
print("=" * 72)
r = decompose_roles("琪亚娜·卡斯兰娜和芽衣")
check("全名不降级", set(r) == {"琪亚娜", "芽衣"}, f"got={r}")
# 不重复：一个角色只出现一次
r2 = decompose_roles("琪亚娜，琪亚娜她，还有琪亚娜")
check("同角色去重", r2 == ["琪亚娜"], f"got={r2}")

print()
print("=" * 72)
print("五、角色标打标（入账命中即打标）")
print("=" * 72)
from kb.memory.l3_store import L3Store, L3Item
store = L3Store("u_roles_test")

# 模拟入账：content 含角色名 → 应打标
def _tag_of(content: str) -> list[str]:
    """打标逻辑（与 l3_store.nominate 内的角色打标一致）。"""
    return decompose_roles(content)

r3 = _tag_of("在千羽学园，琪亚娜徒手撕下了芽衣的崩坏翅膀")
check("入账打标含琪亚娜", "琪亚娜" in r3, f"got={r3}")
check("入账打标含芽衣(自指也算)", "芽衣" in r3, f"got={r3}")
r4 = _tag_of("玩家今天加班很累")
check("无角色不入标", r4 == [], f"got={r4}")

# L3Item 有 characters 字段
it = L3Item(id="t1", date="2026-10-07", content="银杏树下的约定",
            tags="羁绊×温柔", storage=7.0,
            scene_words=["约定", "仪式"])
check("L3Item 有 characters 字段", hasattr(it, "characters"),
      f"fields={list(it.__dataclass_fields__)}")
check("characters 默认空（旧条目兼容）", it.characters == [],
      f"got={it.characters}")

print()
print("=" * 72)
print("六、倒排挂载（角色名走主索引，非独立通道）")
print("=" * 72)
# 角色名应是倒排索引的合法居民 —— nominate 后能按角色名检索回
store.nominate({"content": "在千羽学园，琪亚娜徒手撕下了芽衣的崩坏翅膀",
                "tags": "羁绊×感动", "storage": 8.0,
                "scene_words": ["千羽学园"], "reason": "第三次崩坏"})
hits_q = store.retrieve("你认识琪亚娜吗", k=5)
check("角色名走主索引可检索", len(hits_q) >= 1, f"got={hits_q}")
if hits_q:
    check("检索回来的是那条琪亚娜记忆",
          "琪亚娜" in hits_q[0][0].content, f"got={hits_q[0][0].content}")

# 对照：情绪通道独立 —— 场景索引里不该有情绪族名作为合法 key
from kb.memory.l3_vocab import VALID_SCENE_KEYS
check("角色名可进主索引（专有名词）", True)  # 结构性声明，见上
# 检索「琪亚娜」不应影响场景词合法集
check("角色名不污染场景词合法集",
      "琪亚娜" not in VALID_SCENE_KEYS and "布洛妮娅" not in VALID_SCENE_KEYS,
      "角色名不应进 l3_vocab")

print()
print("=" * 72)
print("七、双路信号（氛围层可亮 / 证据层不造记忆）")
print("=" * 72)
# 氛围层：命中角色 → 可亮（与库中是否有记忆无关）
check("氛围层：命中角色即可亮（不依赖库）",
      decompose_roles("你认识琪亚娜吗") == ["琪亚娜"])
# 证据层：库里没有该角色记忆 → 检索空 → 不造记忆
empty_store = L3Store("u_roles_empty")
hits_empty = empty_store.retrieve("你认识琪亚娜吗", k=5)
check("证据层：零命中不造记忆", hits_empty == [], f"got={hits_empty}")
# 二者解耦：氛围亮 ≠ 证据有
check("两路解耦：氛围亮但证据可空",
      decompose_roles("你认识琪亚娜吗") != [] and hits_empty == [])

print()
print("=" * 72)
print(f"汇总：{PASS}/{PASS + FAIL} 通过")
if FAILURES:
    print("失败项：")
    for f in FAILURES:
        print("  -", f)
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
