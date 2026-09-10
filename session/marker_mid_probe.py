# coding: utf-8
"""session.marker_mid_probe —— 改四【标记中途出现】计数观察(离线 0 API)。

背景(编排 doc 改四·记档不阻塞):
  首行 [闲聊]/[攻略] 标记由 engine 剥离(洞1,玩家侧永不见)。但流式下正文中途
  再冒出的标记**无法事后抹除**(那些字符已随流发给玩家)—— 不能拦,只能记档观察。
  本探针把原『观察项』升级成最小闭环:单轮内标记出现次数可观测 + 10+ 轮模拟留档。

跑法: python -m session.marker_mid_probe     # exit 0 = 探针全过;并打印计数日志

留档输出: session/_marker_mid_archive/run.log(计数日志 + 三行结论由人填)
"""
from __future__ import annotations

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from session.engine import SessionEngine  # noqa: E402


class ScriptedLLM:
    """按脚本逐轮吐回复(可含首行合法标记 / 正文中途乱入标记 / 完全无标记)。"""
    def __init__(self, replies):
        self.replies, self.idx = list(replies), 0

    def __call__(self, system, user, **kw):
        r = self.replies[self.idx % len(self.replies)]
        self.idx += 1
        return r


def _load_dialog():
    """10+ 轮模拟对话:前 2 轮干净,第 3 轮起掺入『正文中途冒标记』的脏样本。"""
    return [
        ("今天有点累",       "[闲聊]\n累了就靠着我歇会儿,不用硬撑。"),
        ("深渊那期怎么配队?", "[攻略]\n雷队破盾稳,主 C 带双爆就行。"),
        # 脏样本①:首行标记拆分但正文中途又冒一个
        ("你记得我们约定的事吗", "[闲聊]\n当然记得。别忘了[闲聊]我们约好要一起去的。"),
        ("说点别的",         "[闲聊]\n好啊,你想聊什么我都陪。"),
        # 脏样本②:正文中途冒标记两次
        ("帮我查下这个角色",   "[攻略]\n这个角色[攻略]其实更适合副 C[攻略],别当主 C。"),
        ("嗯嗯",            "[闲聊]\n嗯,我在。"),
        ("我有点烦",         "[闲聊]\n烦就说说,我听着呢。"),
        # 脏样本③:中途乱入(全角变体)
        ("芽衣在吗",         "[闲聊]\n在的。［攻略］不过你要打法我也随时接。"),
        ("谢谢你",          "[闲聊]\n跟我还客气什么。"),
        ("晚安",            "[闲聊]\n晚安,做个好梦。"),
        ("在吗",            "[闲聊]\n在,一直。"),
        # 脏样本④:中途冒标记紧跟正文
        ("再讲讲",          "[闲聊]\n上次说到哪儿了……[闲聊]对了,那天你笑得很好看。"),
    ]


def run() -> int:
    dialog = _load_dialog()
    llm = ScriptedLLM([r for _, r in dialog])
    eng = SessionEngine(
        user_id="u_marker_mid",
        character_prompt="你是雷电芽衣。人格铁律:BE_AYAME。",
        extractor_prompt=None,        # 本探针只看标记计数,不跑书记员
        llm_chat=llm,
        quality_prompt=None,          # 质检关,隔离影响
    )

    lines = []
    first_lines = []
    def log(s=""):
        lines.append(s)
        print(s)

    log("==== 改四·标记中途出现 计数观察(离线模拟,10+ 轮) ====")
    log(f"{'轮':>2} | {'残留标记数':>6} | 玩家可见首行")
    log("-" * 60)

    for i, (user_msg, _raw) in enumerate(dialog, 1):
        r = eng.turn(user_msg)
        first_lines.append(r.reply.split("\n")[0] if r.reply else "")
        hits = eng.marker_mid_log[-1]["hits"] if eng.marker_mid_log else 0
        head = (r.reply or "").split("\n")[0][:34]
        log(f"{i:>2} | {hits:>6} | {head}")

    total_turns = eng._total_turns
    total_hits = eng.qc_marker_mid_hits()
    hit_turns = eng.qc_marker_mid_turns()
    rate = eng.marker_mid_rate()
    log("-" * 60)
    log(f"总轮数={total_turns}  残留标记总数={total_hits}  "
        f"残留轮数={hit_turns}  轮次率={rate:.2%}")
    log(f"marker_mid_log: {json.dumps(eng.marker_mid_log, ensure_ascii=False)}")

    # 留档
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(here, "_marker_mid_archive")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "run.log")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n[留档] {out_path}")

    # 断言:计数仪表成立(至少捕到脏样本;玩家可见首行不含标记的洞1不被破坏)
    assert total_turns >= 10, total_turns
    assert total_hits >= 3, total_hits          # 脏样本确实被数到
    assert hit_turns >= 3, hit_turns
    # 洞1 复验:每轮玩家可见**行首**不含成形标记(首行剥离仍完好)。
    #   注意:中途标记(改四)不在此断言范围 —— 它本来就在正文中部,无法剥离。
    from session.routing import modality_from_first_line
    for i, fl in enumerate(first_lines, 1):
        assert modality_from_first_line(fl) is None, f"轮{i} 首行漏标记: {fl!r}"
    print("[PASS] 计数仪表成立;首行剥离(洞1)未被破坏")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
