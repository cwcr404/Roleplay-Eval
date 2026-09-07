# coding: utf-8
"""routing_probe —— 0908第二块(案A·预查+内嵌路由+KB分叉)验收桩(离线,0 API)。

对应 doc 步骤2 验收 / 迟旭拍板的案A三级断言:
  1. 组装层   prompt 含 guide/lore 命中素材(预查每轮做,检索=意图闸门)
  2. 正文     fake 回复引用了素材干货
  3. 可见流   [闲聊]/[攻略] 标记绝不出现在玩家可见正文(history/返回值 0 命中)
外加洞2 缺省: 首行无合法标记(自由文本/空)→ 按 [闲聊] 落、记日志、不阻塞;
无 KB 注入 → 纯退化零 KB 行为(不记 no_kb_hit,不崩)。

用法: python -m session.routing_probe   # exit 0 / 1(有断言失败会 assert 抛)
      python dev/regress_all.py          # 已并入第 9 项 routing
"""
from __future__ import annotations

from session.engine import SessionEngine
from session.routing import CHAT_MODALITY, GUIDE_MODALITY


class _Hit:
    def __init__(self, content: str):
        self.content = content


class _FakeRetriever:
    """满足 kb.retriever 的 query 契约(record 调用供断言)。"""
    def __init__(self, hits):
        self._hits = hits
        self.last_query = None
        self.last_top_k = None

    def query(self, text, *, top_k, domain=None, as_of_time=None):
        self.last_query = text
        self.last_top_k = top_k
        return self._hits[:top_k]


def _run() -> int:
    ok = 0

    def chk(name, cond):
        nonlocal ok
        ok += 1
        assert cond, name

    # ---- 路径1:攻略意图 -> guide 命中素材灌进 prompt + 首行标记剥离 ----
    guide = _FakeRetriever([_Hit("打 Boss 先破盾:火伤破火盾,雷伤破雷盾,破盾后 5 秒伤害翻倍。")])
    captured = []
    def fake_llm(system, user, **kw):
        captured.append(user)
        return "[攻略]\n破这个先用雷属性打盾,盾一破就盯着伤害翻倍那几秒打,别贪刀。"
    eng = SessionEngine(user_id="stub-kb", character_prompt="你是雷电芽衣。",
                        extractor_prompt=None, llm_chat=fake_llm,
                        kb={"guide": guide, "lore": None})
    r = eng.turn("打这个boss怎么打?BOSS有个盾")
    up = captured[0]
    chk("prompt含guide素材区(组装层)", "破盾" in up and "【此刻相关素材】" in up)
    chk("guide.query被真触发(意图闸门)", guide.last_query == "打这个boss怎么打?BOSS有个盾")
    chk("guide.top_k<=6", guide.last_top_k <= 6)
    chk("正文含素材干货(案A正文断言)", ("破盾" in r.reply or "翻倍" in r.reply))
    chk("可见流无[攻略]标记(洞1)", "[攻略]" not in r.reply)
    chk("可见流无[闲聊]标记", "[闲聊]" not in r.reply)
    chk("首行=攻略正文非标记", r.reply.split("\n")[0].startswith("破这个"))
    chk("路由判为攻略", eng._last_modality == GUIDE_MODALITY)
    chk("history存剥离正文0标记", eng._history[0][1] == r.reply and "[攻略]" not in eng._history[0][1])

    # ---- 路径2:闲聊意图 -> guide/lore 均无命中 -> 素材留空,按闲聊原样放行 ----
    def fake_llm2(system, user, **kw):
        captured2.append(user)
        return "嗯,今天怎么想起问这个了。"
    captured2 = []
    eng2 = SessionEngine(user_id="stub-chat", character_prompt="你是雷电芽衣。",
                         extractor_prompt=None, llm_chat=fake_llm2,
                         kb={"guide": _FakeRetriever([]), "lore": _FakeRetriever([])})
    r2 = eng2.turn("我就是随便问问嘛")
    chk("闲聊素材未强灌(无素材区)", "【此刻相关素材】" not in captured2[0])
    chk("闲聊缺省按闲聊", eng2._last_modality == CHAT_MODALITY)
    chk("闲聊正文原样放行", r2.reply == "嗯,今天怎么想起问这个了。")
    chk("无素材记 no_kb_hit 日志(洞2观测)", any("no_kb_hit" in x for x in eng2._kb_log))

    # ---- 路径3:攻略标记但无 KB 注入 -> 纯退化,不崩、不记日志 ----
    def fake_llm3(system, user, **kw):
        return "[攻略]\n这个我不确定,但硬要说的话先试雷队吧。"
    eng3 = SessionEngine(user_id="stub-nokb", character_prompt="你是雷电芽衣。",
                         extractor_prompt=None, llm_chat=fake_llm3, kb=None)
    r3 = eng3.turn("深渊这期怎么配队")
    chk("无KB纯退化不崩", r3.reply.startswith("这个我不确定"))
    chk("攻略标记仍被剥", "[攻略]" not in r3.reply)
    chk("无KB场景不记no_kb_hit(素材空≠攻略缺素材)", not any("no_kb_hit" in x for x in eng3._kb_log))

    print(f"routing_probe: {ok} 断言全过")
    return 0


def _main() -> int:
    try:
        return _run()
    except AssertionError as e:
        print("routing_probe FAIL @", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
