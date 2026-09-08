# coding: utf-8
"""async_quality 确定性自检(不烧 API):锁定异步质检判决契约。

断言:
1. parse:合法 JSON 判决 → 正确结构化;带 ```围栏/尾部废话 → 仍解析干净。
2. parse:坏 JSON(非对象/字段越界/空)→ 降级 pass + ok=False,不抛。
3. 频次闸:pass 不注入;超 POLISH_GATE_N 连续修正后不再注入(只留计数推进)。
4. reason 纯描述护栏:壳只拼 SHELL_PREFIX + reason/anchor;不掺指令尾注。
5. 决策表:blocked(hard)→ '拦(…)' / polish → '微调(…)' / pass → '通过'。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from session.async_quality import (parse_verdict, inject_fragment, to_decision,
                                   Verdict, SHELL_PREFIX, POLISH_GATE_N)

checks = []
def ok(name, cond):
    checks.append(cond)
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(1)

# 1 合法
v = parse_verdict("""{"verdict":"blocked","category":"客服腔","register":"hard","anchor":"您好,为您查询到…","reason":"客服话术模板。","note":""}""")
ok("parse:合法JSON→结构化", v.ok and v.verdict == "blocked" and v.category == "客服腔"
   and v.register == "hard" and v.anchor.startswith("您好"))

# 1b 带围栏+尾部废话
v = parse_verdict("""```json\n{"verdict":"polish","category":"OOC","register":"soft","anchor":"过于甜腻","reason":"语气偏甜。","note":""}\n``` 后续废话""")
ok("parse:围栏+尾部→仍干净", v.ok and v.verdict == "polish" and v.register == "soft"
   and v.reason == "语气偏甜。")

# 2 坏
v = parse_verdict("not json")
ok("parse:坏JSON→pass+ok=False", (not v.ok) and v.verdict == "pass")
v = parse_verdict('{"verdict":"banana"}')
ok("parse:字段越界→pass(缺省)", v.ok and v.verdict == "pass")

# 3 频次闸:pass 不注入;连续 polish 到 GATE 后停
txt, count = inject_fragment(Verdict(verdict="pass"), 0)
ok("gate:pass→无注入", txt == "" and count is False)
txt, cnt = inject_fragment(Verdict(verdict="polish", reason="语气偏正式。"), 0)
ok("gate:首轮polish→注入描述壳", txt.startswith(SHELL_PREFIX) and "语气偏正式" in txt and cnt is True)
txt, cnt = inject_fragment(Verdict(verdict="blocked", reason="承认AI。"), POLISH_GATE_N)
ok("gate:达闸→不再注入", txt == "" and cnt is True)  # 计数仍推进,但内容为空
txt, cnt = inject_fragment(Verdict(verdict="blocked", reason="承认AI。"), POLISH_GATE_N - 1)
ok("gate:未达闸边界→注入", txt != "" and cnt is True)

# 4 reason 纯描述壳
txt, _ = inject_fragment(Verdict(verdict="polish", reason="某句像接线员。"), 0)
ok("shell:只拼描述+锚", txt == SHELL_PREFIX + "某句像接线员。")

# 5 决策表
ok("decision:blocked/hard→拦", to_decision(Verdict(verdict="blocked", category="客服腔", register="hard")).startswith("拦(客服腔/hard)"))
ok("decision:polish→微调", to_decision(Verdict(verdict="polish", category="OOC", register="soft")).startswith("微调(OOC/soft)"))
ok("decision:pass→通过", to_decision(Verdict(verdict="pass")) == "通过")

print(f"\nasync_quality 自检 {'ALL PASS' if all(checks) else 'FAIL'}  ({len(checks)} 项)")
