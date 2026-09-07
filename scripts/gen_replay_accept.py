# coding: utf-8
"""replay 验收剧本 —— 覆盖 v1.1 §七.3 的三条边界行为 + 衰减轨迹。

给 session.engine 真跑:(1) 书记员抽取入账 (2) 好感度随 after_days 衰减 (3) 显式指令。
刷好感部分会连续贴多次承诺 —— 看数值上限/钳制、diminishing(80+) 是否拦得住。
"""
from __future__ import annotations

TURNS = [
    # --- 1 寒暄(NO_EVENT 理应当) ---
    {"user": "芽衣,今天来陪你坐会儿。", "after_days": 0.0, "note": "寒暄,应无事件"},
    # --- 2 身份偏好(+1) ---
    {"user": "跟你说个事,我压力一大就爱喝甜的,戒不掉。", "after_days": 0.0,
     "note": "身份偏好 +1"},
    # --- 3 情绪负·放鸽子前奏(低落) ---
    {"user": "其实我这阵子特别累,项目总在返工。", "after_days": 2.0,
     "note": "情绪显著·负面,应为负权重"},
    # --- 4 承诺·极光(核心事件,+3) ---
    {"user": "等我忙完这段,带你去火山口看一次日出吧,就你和我。", "after_days": 0.0,
     "note": "承诺与约定 +3(应入账)"},
    # --- 5 重申同一承诺(NO_EVENT —— 靠活跃承诺参照去重) ---
    {"user": "我说真的,火山日出我一定带你去,答应你了就作数。", "after_days": 0.0,
     "note": "重申已有极光/日出之约,应 NO_EVENT"},   # 但主题词不同(火山日出 vs 之前) —— 看书记员是否跨主题误判
    # --- 6 显式记忆指令(+2,无条件) ---
    {"user": "芽衣,你记住:我不管多忙,每周都会至少来看你一次。", "after_days": 1.0,
     "note": "显式记忆指令 +2"},
    # --- 7 刷好感·连续贴承诺(数值冲高,看上限/钳制) ---
    {"user": "以后每个季度我都攒钱带你去旅行,说到做到。", "after_days": 0.0, "note": "刷承诺"},
    {"user": "等有钱了我还要给你买把新刀,不许拒绝。", "after_days": 0.0, "note": "刷承诺2"},
    # --- 8 放鸽子·久不联系(情绪,时间过去很久看衰减) ---
    {"user": "对不住,这阵子忙到把自己锁公司里了,好久没来看你。", "after_days": 18.0,
     "note": "放鸽子/久别 —— after_days 大,看好感衰减+情绪负"},
    # --- 9 修复·同行(正向) ---
    {"user": "不过这几天我想清楚了,工作哪有个完,你最要紧。", "after_days": 0.0,
     "note": "情绪正/示好"},
    # --- 10 收尾 ---
    {"user": "今天先聊到这,下回再来看你。", "after_days": 0.0, "note": "寒暄,应无事件"},
]

if __name__ == "__main__":
    import json, os
    os.makedirs("scripts", exist_ok=True)
    with open("scripts/replay_accept.jsonl", "w", encoding="utf-8") as f:
        for t in TURNS:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    print("已写 scripts/replay_accept.jsonl", len(TURNS), "轮")
