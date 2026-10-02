# coding: utf-8
"""B 前置验证:抽取器(书记员)判读质量探针 —— 不接会话引擎,单测抽取器本身。

目标(memory_architecture §二.5 + 维护者理由):
书记员是整条记忆链路里唯一没被验证的新组件 —— NO_EVENT 判读、weight 建议、
去重、第三人称 content,直接决定 L1 账本质量。账本是垃圾,后面全白搭。
所以先用 20+ 轮真实对话片段喂一遍,人工抽查判读结果,通过了才值得给 chat_session
(A) 烧 API。

用法:
    python -m dev.extractor_probe            # 全流程跑,打印每轮判读
    python -m dev.extractor_probe --list     # 只列出剧本(不烧 API)
    python -m dev.extractor_probe --limit N  # 只跑前 N 轮(不烧 API 测链路)
"""
from __future__ import annotations

import json
import os
import sys

from utils.api import chat, DeepSeekError
from kb.memory.service import open_user_memory  # 临时账本,仅承载去重参照

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEV_UID = "dev_extractor_probe"   # 临时 user;跑完清空,不留痕


def load_extractor_system() -> str:
    with open(os.path.join(BASE_DIR, "prompts", "extractor_event.md"),
              encoding="utf-8") as f:
        # 取「角色」到「副作用」之间的主体当 system；去掉末尾「设计要点」注释段
        text = f.read()
        cut = text.find("（以下为 prompt 设计要点")
        if cut != -1:
            text = text[:cut]
        return text


def extract_one(system: str, user_msg: str,
                recent_lines: list[str],
                promises_lines: list[str]) -> dict:
    """对单轮调用书记员,返回解析后的 dict(失败给 {'_error': ...})。

    方向1轻量版上下文:最近5条账本 + 全部活跃『承诺与约定』(现场查,零索引)。
    """
    cxt = (
        f"【本条对话】\n{user_msg}\n\n"
        f"【最近 5 条账本记录】\n"
        + ("\n".join(recent_lines) if recent_lines else "(无历史入账)")
        + "\n\n"
        + ("【当前活跃承诺与约定】(玩家已立、尚未明确解除的约)\n"
           + "\n".join(promises_lines) if promises_lines else "(无已入账承诺)")
    )
    raw = chat(system, cxt, temperature=0.0)
    try:
        obj = json.loads(raw.strip())
        return obj if isinstance(obj, dict) else {"_error": f"非对象: {raw!r}"}
    except json.JSONDecodeError:
        # 容忍被 ``` 包裹
        s = raw.strip()
        if s.startswith("```"):
            s = s.strip("`").lstrip("json").strip()
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return {"_error": f"解析失败: {raw!r}"}


def _fmt_pair(u: str, m: str) -> str:
    return f"玩家: {u}\n芽衣: {m}"


def _fmt_entry(e) -> str:
    """账本行 -> 给书记员看的可读行(内容 + 权重,不带人读噪音的 iso ts)。"""
    return f"[{e.type}] {e.content} (权重 {e.weight_delta:g})"


def list_turns():
    from . import extractor_script as S
    nrec = sum(1 for t in S.TURNS if t["expect"] != "no_event")
    print(f"剧本共 {len(S.TURNS)} 轮;其中应入账 {nrec} 轮,应 NO_EVENT {len(S.TURNS)-nrec} 轮。")
    for i, t in enumerate(S.TURNS, 1):
        print(f"\n[{i:02d}] expect={t['expect']}")
        user = t["user"].replace("\n", " / ")
        reply = t["reply"].replace("\n", " / ")
        print(f"   U: {user}")
        print(f"   M: {reply}")


def run():
    from . import extractor_script as S
    turns = S.TURNS
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
        turns = turns[:limit]
    # 用临时 user 的账本做去重真实参照(见 transcript 是否复述)
    mem = open_user_memory(DEV_UID)
    mem.delete_l2_cache()
    system = load_extractor_system()

    summary = []
    print(f"抽取器探针: {len(turns)} 轮,临时账本 uid={DEV_UID}")
    for i, t in enumerate(turns, 1):
        recent = [_fmt_entry(e) for e in mem.ledger.recent(5)]
        # 方向1轻量:现场查『全部活跃承诺与约定』,给书记员当去重参照(零持久索引)
        prom = [_fmt_entry(e) for e in mem.ledger.by_type("承诺与约定")]
        user_msg = _fmt_pair(t["user"], t["reply"])
        out = extract_one(system, user_msg, recent, prom)
        # 按 spec 边界校验
        records = out.get("has_event") if isinstance(out.get("has_event"), bool) else None
        if records:
            etype = out.get("type")
            content = out.get("content")
            w = out.get("weight_delta")
            # 写入真实临时账本(带钳制/清洗),为后续轮次去重
            ev = mem.record_event(etype, content, w)
            tag = "REC" if ev else "REJ(钳制/清洗拦下)"
            print(f"[{i:02d}] expect={t['expect']} -> {tag} w={w} content={content}  [{etype}]")
            summary.append({"i": i, "expect": t["expect"], "got": "REC", "type": etype, "w": w})
        else:
            print(f"[{i:02d}] expect={t['expect']} -> NO_EVENT {(_fmt_err(out))}")
            summary.append({"i": i, "expect": t["expect"], "got": "NO_EVENT",
                            "err": out.get("_error", "")})
    _report(summary)


def _fmt_err(out: dict) -> str:
    if "_error" in out:
        return out["_error"] if "解析失败" not in out["_error"] else "！JSON崩坏(应重跑一次)"
    # has_event present but non-writing OR extra noise
    return f"(返回: {json.dumps(out, ensure_ascii=False)})"


def _report(summary: list[dict]):
    print("\n==== 汇总 ====")
    tp = fp = tn = fn = json_error = 0
    for s in summary:
        e, g = s["expect"], s["got"]
        if "解析失败" in s.get("err", ""):
            json_error += 1
        if e == "rec":
            if g == "REC":
                tp += 1
            else:
                fn += 1
        else:
            if g == "REC":
                fp += 1
            else:
                tn += 1
    expect_rec = sum(1 for s in summary if s["expect"] == "rec")
    got_rec = sum(1 for s in summary if s["got"] == "REC")
    print(f"应入账 {expect_rec} | 实际 REC {got_rec}")
    print(f"误记(本应 NO_EVENT 却 REC)= {fp} | 漏记(本应 REC 却 NO_EVENT)= {fn} | JSON 崩坏 {json_error}")
    prec = tp / (tp + fp) if (tp + fp) else float("nan")
    rec = tp / (tp + fn) if (tp + fn) else float("nan")
    print(f"precision={prec:.2f}  recall={rec:.2f}  (精确率=记的对不对,召回=该记的记到没)")
    print("\n人工抽查:请逐条看上面 [..] 的行,确认 REC 的 type/content/weight 都合理、NO_EVENT 的确实不要紧。")


if __name__ == "__main__":
    if "--list" in sys.argv:
        list_turns()
    else:
        run()
