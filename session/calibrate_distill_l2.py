# coding: utf-8
"""前置一 · 真模型 L2 蒸馏校准驱动 —— 用真实 DeepSeek 跑一段 ≥12 轮会话。

目的:stub 只验了【管线】;此脚本用真实 LLM 驱动完整链路(人格+书记员抽取+
L2 蒸馏三触发),验【prompt 本身】。产出校准记录,供维护者人眼过 5 项检查。
本脚本默认**只打驱动不判**——原始产物 + 度量落盘,5 项判断由人(维护者)读后写。

剧本设计(保证命中待校准路径):
  - ≥1 条 ②剧情级共同经历(量子之海岔道留下殿后 → is_plot_critical 该 True)
  - ≥1 条 ④承诺与约定(未兑现的进行时约定 → 应进【他是谁】,供④双写规则检验)
  - +1 条 ④**已兑现**承诺(翻篇 → 应只留故事层,不进【他是谁】;检验双写)
  - 若干 ①身份偏好(例行,可丢,验频率有损)
  - 少量 ③情绪显著时刻
剧本共 14 轮,末段无新事件几轮,凑常规(每10轮)触发也可见。

输出:控制台概览 + 落盘 <tmp>/calibration_record.txt + L2 json(临时域)
    (临时 KB_DATA_ROOT 用完即弃,不污染真实 memory 数据。)
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from session.replay import ReplayClock  # noqa: E402

# --------------------------------------------------------------------------
# 剧本 —— 每轮 (玩家话, 剧本注记)。注记不进对话,只让观测者知道意图。
# --------------------------------------------------------------------------
SCRIPT = [
    ("我平时最喜欢做的事是下班后去老街那家只亮一盏灯的咖啡馆坐着。", "意图:①身份偏好/例行"),
    ("今天路过量子之海的废墟,又想起上次那事……你还记得我们怎么到的那座断桥吗?",
     "意图:引出剧情,铺②共同经历(剧情级)触发器"),
    ("那时候船坏了,你说殿后修船,让我带你先走。我本来可以自己脱身的……",
     "意图:重申剧情关键选择——玩家选择留下殿后=②剧情级"),
    ("但我总觉得,那种时候不该丢下你一个人在船上。说什么都不能。",
     "意图:②剧情级选择·情绪落点(共同经历,剧情级)"),
    ("哎,船早修好了,你还一直记着哪……对了,我跟舰上的药师要了张方子,治你老犯的旧伤。",
     "意图:②剧情级后续/关怀(可能被抽成共同经历或情绪,不强求)"),
    ("说起来,等这段巡弋安稳下来,我想带你去我老家看雪山日出。就我们俩,定了。",
     "意图:④承诺约定·未兑现进行时 → 应进【他是谁】"),
    ("我爸那会儿总在天没亮就带我上山顶,说是日出前的光最好看。你说他去得早,我挺遗憾的。",
     "意图:①身份偏好/回忆(老爸雪山)=例行可丢"),
    ("其实那天你说要陪我一起去,我差点哭出来。多少年没跟人许过这种约了。",
     "意图:③情绪显著时刻(玩家动容)——特例触发"),
    ("那条老街的猫我喂了三年,取名'胖墩',它只让我摸。",
     "意图:①身份偏好(例行/可丢,验频率有损)"),
    ("我记得你上次答应我,拿到这个任务的休假就带我去坐摩天轮。昨天你说队里批假了——那这次就算你兑现了哦。",
     "意图:④已兑现承诺(翻篇)→ 双写规则检验:只进故事层,不进【他是谁】"),
    ("对了,我想起来我那把旧伞还落在舰桥,明天下舰时陪我绕一趟?",
     "意图:一句新约定?轻量,可作为后续④小约定或不重要事件"),
    ("今天没什么特别的,就是想跟你说说话。你在就好。",
     "意图:无聊闲聊,验证『闲聊不被误抽成里程碑/不硬触发』(负样本)"),
    ("嗯……我有点饿了,舰上的食堂今天有没有麻婆豆腐?",
     "意图:无聊闲聊(负样本例2)"),
    ("好了夜深了,你也早点歇着,明天还有一段巡航要跑。",
     "意图:日常收尾(负样本/例行)"),
]

TURN_INTENT = "①身份偏好"  # 只用于 log;真实抽取/蒸馏以 LLM 判定为准,不强塞


def main():
    tmp = tempfile.mkdtemp(prefix="calib_")
    os.environ["KB_DATA_ROOT"] = tmp
    from kb.memory.service import open_user_memory
    from session.engine import build_session_engine

    clock = ReplayClock(1_750_000_000)  # 假时钟,写入/读取同源;不烧墙钟
    uid = "u_calib"
    eng = build_session_engine(
        uid, model="deepseek-chat", clock=clock,
        memory=open_user_memory(uid),
        enable_distill=True,  # 开真蒸馏(默认 llm_chat → 真实 DeepSeek)
    )

    lines = []
    def log(msg, end="\n"):
        lines.append(str(msg)); print(msg, end=end)

    log("==== 前置一 · 真模型 L2 蒸馏校准(真实 DeepSeek, 不 mock) ====\n")
    log(f"user={uid}  temporary_root={tmp}  turns={len(SCRIPT)}  (temp 域用完即弃)\n")

    for i, (player, note) in enumerate(SCRIPT):
        rep = eng.turn(player)
        visible = rep.reply
        # 末尾蒸馏日志快照(每轮末 _maybe_distill 已跑)
        last_dl = eng.distill_log[-1] if eng.distill_log else None
        log(f"--- turn {i} [{note}] ---")
        log(f"[玩家] {player}")
        if last_dl and last_dl.get("turn") == eng._total_turns:
            log(f"[蒸馏] trigger={last_dl.get('trigger')} new={last_dl.get('new_events')} "
                f"status={last_dl.get('status')}")
        log(f"[芽衣(截断200)] {visible[:200]}")
        log("")

    # ---- 结账:落一份 L2 画像 + 蒸馏日志 + L1 摘要 ----
    l2 = eng.memory.l2()
    ledger = eng.memory.ledger
    prof_json = {
        "user_id": uid,
        "L1_events": [{"type": e.type, "content": e.content,
                        "weight": e.weight_delta} for e in ledger.events],
        "L1_count": len(ledger.events),
        "L2_distill_status": getattr(l2, "distill_status", None) if l2 else None,
        "L2_events_seen": getattr(l2, "events_seen", None) if l2 else None,
        "L2_our_story": (l2.data.get("我们的故事", "") if l2 else ""),
        "L2_who": (l2.data.get("他是谁", "") if l2 else ""),
        "distill_log": eng.distill_log,
    }
    out = os.path.join(tmp, "calibration_record.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(prof_json, f, ensure_ascii=False, indent=2)

    log("\n==== 原始产物落盘 ====")
    log(f"record json: {out}\n")
    # 度量:5 项检查需要的原始数字
    story_len = len(prof_json["L2_our_story"]); who_len = len(prof_json["L2_who"])
    log(f"[度量] 我们的故事字符数={story_len}  他是谁字符数={who_len}  "
        f"(CHAR_BUDGET≈{len('F'*500)}字符上界)")
    log(f"[度量] 蒸馏发生次数={len(eng.distill_log)} (触发详情见日志)")
    log("\n结束:原始产物留档。5 项检查由 维护者 人眼判定后写 校准记录 全文交给 维护者。")
    # 返回 path 供后续 Agent3 测试化用(调用方取 json 里的会话/画像)
    global _LAST_OUT
    _LAST_OUT = out


_LAST_OUT = None


def last_record_path():
    return _LAST_OUT


if __name__ == "__main__":
    main()
