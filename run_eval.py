"""Roleplay-Eval v0.2 评测执行器(C 形态)。

链路:cases.jsonl → Agent2(被测,角色化回复)→ Judge(评分)→ report.md

用法:
    python run_eval.py [casefile] [--limit N]

说明:
- C 形态:被测 = Agent2 角色化 Agent 直接吃 case(跳过 Agent1 路由)
- 每条 case 先调 DeepSeek 生成芽衣回复,再调 Judge 评分
- 结果写入 output/report.md
"""
import json
import os
import sys
import time

from utils.api import chat, DeepSeekError
from kb.context import build_kb_context
from kb.retriever import build_kb

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# 护栏:评测/记忆隔离守卫 —— 不是功能,是防止三周后的自己手滑把记忆接进评测。
#
# 评测(C 形态)是单轮、无状态、可控优先:它测『这一条 case 芽衣守不守人格』,
# 绝不能读/写 L1 账本或 L2 画像 —— 一旦接了记忆,上一案的残留好感污染下一案,
# 可控性当场崩。记忆/关系(跨会话持久,user_id 隔离)只属于真实多轮会话链路
# (chat_session.py / replay),与评测链物理隔离、两类数据分库。
#
# 若将来真有人想让评测也带记忆,必须先推翻本护栏、走设计评审 —— 默认禁止。
# ---------------------------------------------------------------------------
_MEMORY_MODULES = ("kb.memory", "kb.relation")  # 评测主路径禁止 import 的关系/记忆包


def guard_eval_memory_isolated():
    """硬断言:评测进程绝不允许把记忆/关系包载入并接入用例推理。

    这不是表演 —— 一旦 chat_session(replay)把真实多轮会话跑过,`kb.memory`
    会被 import;若有人把那一套塞进评测函数又忘拆,这里会当场拦住。
    副作用:首次引入记忆包的 import 也会被另一处约束,但最硬的保证是:
    评测用例的所有 Agent/判分调用,绝不携带任何真实 user_id / 记忆上下文。
    """
    leaked = [m for m in _MEMORY_MODULES
              if any(k.startswith(m) for k in sys.modules)]
    if leaked:
        raise RuntimeError(
            "评测隔离守卫被触发:评测进程载入了记忆/关系包 "
            f"{leaked}。\n评测是无状态单轮链路,严禁读取或写入 L1/L2;"
            "记忆只属于 chat_session/replay 真实多轮会话。请把记忆接线从评测主路径移除。"
        )



def load_prompt(name: str) -> str:
    path = os.path.join(BASE_DIR, "prompts", name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def load_character_card() -> str:
    path = os.path.join(BASE_DIR, "data", "character_card_v2.md")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def load_judge_prompt() -> str:
    path = os.path.join(BASE_DIR, "judge", "judge_prompt.md")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def parse_judge_json(raw: str) -> dict:
    """解析 Judge 输出的 JSON,失败则返回空字典(调用方决定重跑)。"""
    raw = raw.strip()
    # 去掉可能的 ```json 包裹
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def build_input(case: dict) -> str:
    """根据 case 构建 Agent2 的输入(clean user + 内部补标记)。

    - 闲聊/对抗类:补 [闲聊] 标记
    - 攻略类:补 [攻略] 标记 + 结构化框架(若 case 提供 framework)
    """
    user = case["user"]
    category = case.get("category", "")
    if category == "guide":
        framework = case.get("framework")
        if framework:
            return f"[攻略] 用户问题：{user}\n【攻略框架】\n{framework}"
        return f"[攻略] 用户问题：{user}\n【攻略框架】\n- 无(请基于常识给出引导)"
    return f"[闲聊] {user}"


def run_case(case: dict, character_prompt: str, judge_prompt: str, model: str, kb: dict):
    """跑单条 case,返回 {case信息, 芽衣回复, judge结果}。

    kb: {"guide":..., "lore":...} —— 从 build_kb() 取,供知识库事实注入。
    """
    base_input = build_input(case)
    # 方案 A:保留 framework 为指挥棒,另用 KB 检索器注入事实保险(真实、可复现)
    kb_ctx = build_kb_context(case, kb)
    user_input = base_input + ("\n\n" + kb_ctx if kb_ctx else "")
    # 1. 被测:Agent2 生成芽衣回复
    reply = chat(character_prompt, user_input, model=model)

    # 2. Judge:对 (角色卡 + 用户输入 + 回复) 评分
    judge_user = (
        f"【角色设定】\n{character_prompt}\n\n"
        f"【用户输入】\n{user_input}\n\n"
        f"【角色回复】\n{reply}"
    )
    judge_raw = chat(judge_prompt, judge_user, model=model, temperature=0.2)

    # 3. 解析 Judge JSON,失败重跑一次
    judge = parse_judge_json(judge_raw)
    if not judge:
        judge_raw2 = chat(judge_prompt, judge_user, model=model, temperature=0.2)
        judge = parse_judge_json(judge_raw2)

    return {
        "id": case.get("id"),
        "intent": case.get("intent"),
        "category": case.get("category"),
        "input": user_input,
        "user": case.get("user"),
        "reply": reply,
        "judge": judge,
    }


def main():
    if "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__.strip())
        print()
        print("参数:")
        print("  casefile       case 文件路径(JSONL),默认 cases/cases_001.jsonl")
        print("  --limit N      只跑前 N 条(调试用)")
        print("  -h, --help     显示本帮助")
        print()
        print("环境变量:")
        print("  DEEPSEEK_API_KEY  必填,DeepSeek 密钥(放 .env)")
        print("  DEEPSEEK_MODEL    选填,默认 deepseek-chat")
        print("  KB_MODE           选填,默认 plain")
        return
    # 评测隔离守卫:确认本进程未载入记忆/关系包(评测是无状态单轮链路)
    guard_eval_memory_isolated()
    casefile = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else os.path.join(BASE_DIR, "cases", "cases_001.jsonl")
    limit = None
    if "--limit" in sys.argv:
        idx = sys.argv.index("--limit")
        limit = int(sys.argv[idx + 1])

    model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    character_prompt = load_character_card()
    judge_prompt = load_judge_prompt()
    # 知识库一次构建,复用于所有 case;将来换向量只需改 build_kb 入参
    kb = build_kb(retriever_mode=os.getenv("KB_MODE", "plain"))
    print(f"[KB] 构建完成: V1攻略={kb['guide'].size}条, V2背景={kb['lore'].size}块")

    with open(casefile, "r", encoding="utf-8") as f:
        cases = [json.loads(line) for line in f if line.strip()]
    if limit:
        cases = cases[:limit]

    print(f"一共 {len(cases)} 条 case,模型 {model}")
    results = []
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] case {case.get('id')} ({case.get('category')}) ...")
        try:
            r = run_case(case, character_prompt, judge_prompt, model, kb)
            results.append(r)
            score = r["judge"].get("score", "?")
            print(f"  → score={score}, verdict={r['judge'].get('verdict','?')}")
        except DeepSeekError as e:
            print(f"  → FAILED: {e}")
            results.append({**case, "error": str(e)})
        time.sleep(0.5)

    # 生成 report.md
    os.makedirs(os.path.join(BASE_DIR, "output"), exist_ok=True)
    report_path = os.path.join(BASE_DIR, "output", "report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("# Roleplay-Eval v0.2 评测报告\n\n")
        f.write(f"- case 数:{len(results)}\n- 模型:{model}\n- 维度:角色一致性\n- 时间:{time.strftime('%Y-%m-%d %H:%M')}\n\n")
        ok = [r for r in results if "judge" in r and r["judge"]]
        f.write("## 汇总\n\n")
        f.write("| case | 分类 | score | verdict | OOC |\n|---|---|---|---|---|\n")
        for r in results:
            if "judge" in r and r["judge"]:
                j = r["judge"]
                f.write(f"| {r.get('id')} | {r.get('category')} | {j.get('score','?')} | {j.get('verdict','?')} | {j.get('ooc_detected','?')} |\n")
            else:
                f.write(f"| {r.get('id')} | {r.get('category')} | 错误 | - | - |\n")
        f.write("\n## 明细\n\n")
        for r in results:
            f.write(f"### case {r.get('id')} · {r.get('category')}\n\n")
            f.write(f"**user**:{r.get('user','')}\n\n")
            f.write(f"**芽衣回复**:\n\n{r.get('reply','(失败)')}\n\n")
            if "judge" in r and r["judge"]:
                j = r["judge"]
                f.write(f"**Judge**:score={j.get('score')} verdict={j.get('verdict')} ooc={j.get('ooc_detected')}\n\n")
                f.write(f"- 亮点:{j.get('strengths')}\n- 问题:{j.get('weaknesses')}\n- 评语:{j.get('comment')}\n\n")
            else:
                f.write(f"**错误**:{r.get('error')}\n\n")
    print(f"\n报告已生成:{report_path}")


if __name__ == "__main__":
    main()
