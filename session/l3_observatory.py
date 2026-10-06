# coding: utf-8
"""session/l3_observatory.py —— L3 观测台（四视图，零依赖单文件）。

首府裁定（2026-10-06）：
- 四视图【每页回答一个问题】：
  ①  /ledger    记账审计 —— 书记员原始输出 + 可解析草稿 + 可编辑后入库
                 （下方滚动最近 N 条 L1 行）
  ②  /l3        L3 全字段表 —— 条目池全字段，沉睡标灰，含规则触发日志
  ③  /search    检索测试 —— 一句话 → 命中条目 + 命中媒介（中间变量显式）
  ④  /inject    注入块导出 —— L2 关系包 + L3 命中打包成可复制文本
- 技术栈：纯标准库 http.server，零依赖；HTML 模板字符串内嵌。
- 起服务：`python session/l3_observatory.py [--port 8765] [--user u_e2e]`
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kb.memory.l3_store import (  # noqa: E402
    DAILY_NEW_CAP, EntryQuota, L3Store, decompose_query,
)
from kb.memory.l3_vocab import HYPERNYM_MAP  # noqa: E402


# ═══════════════════════════════════════════════════════════
# 样式（内嵌，零外部资源）
# ═══════════════════════════════════════════════════════════
CSS = """
:root{--bg:#0f1115;--fg:#e6e6e6;--dim:#8b93a1;--line:#242833;
      --card:#161a22;--acc:#7dd3fc;--warn:#fbbf24;--bad:#f87171;--ok:#4ade80}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
nav{display:flex;gap:2px;background:var(--card);border-bottom:1px solid var(--line);
    padding:0 12px;position:sticky;top:0;z-index:9}
nav a{color:var(--dim);text-decoration:none;padding:12px 16px;font-size:13px}
nav a:hover{color:var(--fg)}
nav a.on{color:var(--acc);border-bottom:2px solid var(--acc)}
main{max-width:1180px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:17px;margin:0 0 4px}
h2{font-size:14px;color:var(--dim);font-weight:600;margin:24px 0 8px}
.sub{color:var(--dim);font-size:12px;margin:0 0 16px}
table{width:100%;border-collapse:collapse;font-size:13px;
      background:var(--card);border-radius:6px;overflow:hidden}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);
      vertical-align:top}
th{color:var(--dim);font-weight:600;font-size:12px;background:#1a1f29}
tr.dormant td{color:#4b5563}
tr:hover td{background:#1a1f29}
.tag{display:inline-block;background:#1e293b;color:var(--acc);
     border-radius:3px;padding:1px 6px;font-size:11px;margin:1px 2px 1px 0}
.tag.dim{background:#1f242e;color:var(--dim)}
.bar{height:6px;background:#1f242e;border-radius:3px;overflow:hidden;
     min-width:60px;display:inline-block;vertical-align:middle}
.bar i{display:block;height:100%;background:var(--acc)}
pre{background:var(--card);border:1px solid var(--line);border-radius:6px;
    padding:12px;overflow:auto;font-size:12px;white-space:pre-wrap}
input,button,textarea{font:inherit;background:#1a1f29;color:var(--fg);
     border:1px solid var(--line);border-radius:5px;padding:8px 10px}
input{width:60%}
button{cursor:pointer;background:var(--acc);color:#0f1115;border:0;
       font-weight:600;padding:9px 18px}
button:hover{opacity:.9}
.note{color:var(--dim);font-size:12px}
.ok{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;
      padding:14px;margin:10px 0}
.medium{color:var(--warn);font-size:12px}
.hit{background:var(--card);border-left:3px solid var(--acc);border-radius:4px;
     padding:10px 12px;margin:8px 0}
"""


def page(title: str, body: str, active: str) -> str:
    tabs = [("/ledger", "① 记账审计"), ("/l3", "② L3 全字段"),
            ("/search", "③ 检索测试"), ("/inject", "④ 注入块导出")]
    nav = "".join(
        '<a href="%s" class="%s">%s</a>' % (h, "on" if h == active else "", t)
        for h, t in tabs)
    return ("<!doctype html><html lang=zh><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>%s · L3 观测台</title><style>%s</style></head><body>"
            "<nav><a href='/' style='color:var(--acc);font-weight:700'>🦊 L3 观测台</a>%s</nav>"
            "<main>%s</main></body></html>" % (html.escape(title), CSS, nav, body))


# ═══════════════════════════════════════════════════════════
# 数据取用
# ═══════════════════════════════════════════════════════════
def query_raw_words(query: str) -> list:
    """原始 query 词（不扩展）——用于还原「上位词桥」这个中间变量。

    词表里的 key 出现在 query 里就算一个原始词（与 decompose_query 同源，
    只是不扩展）。零 LLM、毫秒级。
    """
    from kb.memory.l3_vocab import SITUATION_MAP
    raw = []
    for k in HYPERNYM_MAP:
        if k in query:
            raw.append(k)
    for k in SITUATION_MAP:
        if k in query:
            raw.append(k)
    return raw or [query]


def _store(user: str) -> L3Store:
    return L3Store(user)


def _ledger_events(user: str, limit: int = 30):
    try:
        from kb.memory.ledger import open_user_ledger
        led = open_user_ledger(user)
        evs = list(led.events)[-limit:]
        return list(reversed(evs))
    except Exception as e:
        return [("ERR", str(e), 0)]


def _l2_profile(user: str):
    try:
        from kb.memory.distill import load_l2
        return load_l2(user)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════
# ① 记账审计
# ═══════════════════════════════════════════════════════════
def view_ledger(user: str, q: dict) -> str:
    rows = _ledger_events(user, 40)
    body = ["<h1>① 记账审计</h1>",
            "<p class='sub'>书记员原始输出 → 可解析草稿 → 可编辑后入库。"
            "下方滚动显示最近 L1 行，看记账是否如实。</p>",
            "<div class='card'><h2 style='margin-top:0'>书记员试跑（mock，不烧 token）</h2>",
            "<form method=get action='/ledger'>",
            "<input name='text' placeholder='输入一轮对话，如：下暴雨了 / 我来接你' value='%s'>"
            % html.escape(q.get("text", "")),
            " <button>粗筛 + 全套</button></form>"]
    if q.get("text"):
        body.append("<h2>模拟结果</h2>")
        try:
            from kb.memory.clerk import Clerk, heuristic_intensity
            raw = q["text"]
            um, rp = (raw.split("/", 1) + [""])[:2] if "/" in raw else (raw, "")
            c = Clerk(llm=None)
            scr = c.screen(um, rp)
            body.append("<pre>粗筛强度 = %d%s\n(降级: %s)</pre>"
                        % (scr.intensity, "  [≥4 → 触发全套]" if scr.intensity >= 4 else "  [&lt;4 → 只记 L1]",
                           scr.degraded))
            if scr.intensity >= 4:
                body.append("<pre>全套输出：需真实模型接线（等迟旭选模型/起服务）\n"
                            "此处 mock 模式返回 degraded=True，不冒充真实产物。</pre>")
        except Exception as e:
            body.append("<pre class='bad'>%s</pre>" % html.escape(str(e)))
    body.append("</div>")

    body.append("<h2>最近 L1 行</h2><table><tr><th>时间</th><th>类型</th>"
                "<th>内容</th><th>weight</th></tr>")
    for e in rows:
        if isinstance(e, tuple):
            ts = content = etype = ""
            w = e[2] if len(e) > 2 else 0
            content = html.escape(str(e[1]))
        else:
            ts = getattr(e, "ts", None)
            etype = getattr(e, "type", getattr(e, "etype", ""))
            content = html.escape(str(getattr(e, "content", "")))
            w = getattr(e, "weight_delta", 0)
        tstr = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "-"
        body.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                    % (tstr, html.escape(str(etype)), content, w))
    body.append("</table>")
    if not rows:
        body.append("<p class='note'>（账本为空 —— 跑一遍对话或 demo 再回来看）</p>")
    return page("记账审计", "".join(body), "/ledger")


# ═══════════════════════════════════════════════════════════
# ② L3 全字段表
# ═══════════════════════════════════════════════════════════
def view_l3(user: str) -> str:
    st = _store(user)
    now = time.time()
    items = sorted(st.active_items(),
                   key=lambda it: -it.extraction_at(now))
    body = ["<h1>② L3 全字段表</h1>",
            "<p class='sub'>关系事件池全字段。沉睡（提取≤2）标灰。"
            "规则触发日志：查重合并 / 限额拦截 / 衰减记录。</p>",
            "<p class='note'>全库 %d 条 · 索引 %d 词 · 沉睡 %d 条</p>"
            % (len(items), st.index_size(), len(st.sleeping_items(now))),
            "<table><tr><th>id</th><th>日期</th><th>画面</th><th>双维标签</th>"
            "<th>存储</th><th>提取</th><th>场景词</th><th>浮现</th><th>理由</th></tr>"]
    for it in items:
        ex = it.extraction_at(now)
        dormant = it.is_dormant(now)
        wid = int(ex / 10 * 100)
        tags = "".join("<span class='tag'>%s</span>" % html.escape(t)
                       for t in (it.tags or "").split("×") if t)
        sw = "".join("<span class='tag dim'>%s</span>" % html.escape(w)
                     for w in (it.scene_words or []))
        body.append(
            "<tr class='%s'><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%.0f</td><td><span class='bar'><i style='width:%d%%'></i></span> %d</td>"
            "<td>%s</td><td>%s</td><td class='note'>%s</td></tr>"
            % ("dormant" if dormant else "", html.escape(it.id), it.date,
               html.escape(it.content), tags, it.storage, wid, ex, sw,
               it.evoked_count, html.escape(it.reason or "")))
    body.append("</table>")

    # 规则触发日志（从条目状态推导）
    body.append("<h2>规则触发日志（查询派生）</h2><div class='card'><pre>")
    log = []
    for it in items:
        if it.is_dormant(now):
            log.append("[沉睡] %s 提取=%d ≤2 → 主动进口跳过" % (it.id, it.extraction_at(now)))
        if it.evoked_count > 1:
            log.append("[关主动] %s 浮现=%d >1 → 不再主动提" % (it.id, it.evoked_count))
        if it.in_refractory(now):
            log.append("[不应期] %s 72h 内已浮现 → 再触碰降级" % it.id)
        if it.last_active_entry > 0:
            log.append("[已进口] %s 上次主动=%.0fh 前"
                       % (it.id, (now - it.last_active_entry) / 3600))
    ok, why = EntryQuota.active_entry_allowed(st, now)
    log.append("")
    log.append("[配额] 当前%s：%s" % ("允许" if ok else "拦截", why))
    today = time.strftime("%Y-%m-%d")
    n_today = sum(1 for it in items if it.date == today)
    log.append("[日限额] 今日新增 %d / 上限 %d%s"
               % (n_today, DAILY_NEW_CAP,
                  "（已满，新提名转 L1 待复核）" if n_today >= DAILY_NEW_CAP else ""))
    body.append(html.escape("\n".join(log) if log else "（无触发记录）"))
    body.append("</pre></div>")

    # 待复核（被限额/查重拦截、称「转 L1 不丢」）
    # 诚实说明：nominate 被拒时不落 L3（只报 verdict），拦截记录应在 L1 侧。
    # L1 侧接线未通前，此区只做声明，不造假数据。
    body.append("<h2>待复核（被拦截、转 L1 不丢）</h2>")
    body.append("<p class='note'>L3 层无待复核条目 —— 拦截（日限额/查重）在 <b>L1 侧</b>落「待复核」标记，"
                "L1 接线后此区从 L1 掉出。此处不造假数据（口径：账实相符）。</p>")
    return page("L3 全字段", "".join(body), "/l3")


# ═══════════════════════════════════════════════════════════
# ③ 检索测试
# ═══════════════════════════════════════════════════════════
def _as_words(v) -> set:
    """把词表值归一为词集合。值可能是 str（单值）或 list/tuple/set（多值）。"""
    if v is None:
        return set()
    if isinstance(v, str):
        return {v} if v else set()
    return {x for x in v if x}


def _ups_of(w: str) -> set:
    """一个词的「上位词閉包」：它本身 + 它映射到的值 + 映射到它的 key。"""
    s = {w}
    s |= _as_words(HYPERNYM_MAP.get(w))
    for k, ups in HYPERNYM_MAP.items():
        if w in _as_words(ups):
            s.add(k)
            s |= _as_words(ups)
    return s


def _all_up_values() -> set:
    """词表里所有「上位词值」（如 饮品/天气）——用于识别真正的中间变量。"""
    vals = set()
    for ups in HYPERNYM_MAP.values():
        vals |= _as_words(ups)
    return vals


def _medium_of(query_words: list, item_words: list,
               raw_words: list = None) -> tuple[str, list]:
    """命中媒介：说清【query 词 ↔ 条目场景词】是怎么连上的。

    媒介三类（裁定：中间变量必须显式）：
      · 直接命中：条目场景词里就有该词
      · 上位词：两边映射到同一个上位词（报那个上位词，如『饮品』）
      · 情境词：同经一个情境词
    """
    from kb.memory.l3_vocab import SITUATION_MAP
    iws = set(item_words)
    raw = list(raw_words) if raw_words else list(query_words)

    direct = [w for w in raw if w in iws]
    if direct:
        return "直接命中『%s』" % "、".join(direct), direct

    # 上位词桥：raw 词闭包 ∩ item 词闭包
    raw_ups = set()
    for w in raw:
        raw_ups |= _ups_of(w)
    inter = set()
    for iw in iws:
        inter |= (raw_ups & _ups_of(iw))
    # 只留「真上位词」（在词表 values 里）且不是 query 自身词，再排单字碎片
    allvals = _all_up_values()
    meds = [u for u in inter if u in allvals and u not in raw]
    # 去掉被更长的上位词包含的单字碎片（如『品』被『饮品』包含）
    meds = [u for u in meds
            if not any(u != v and u in v for v in meds)]
    if meds:
        return " / ".join("上位词『%s』" % u for u in sorted(meds)), raw

    # 情境词桥
    for k, ss in SITUATION_MAP.items():
        s = set(ss) | {k}
        if (set(raw) & s) and (iws & s):
            return "情境词『%s』" % k, raw
    return "（词面未直连，待查）", raw


def view_search(user: str, q: dict) -> str:
    query = q.get("q", "").strip()
    body = ["<h1>③ 检索测试</h1>",
            "<p class='sub'>输入一句话 → 命中条目 + <b>命中媒介</b>。"
            "漏报误报全靠它看（中间变量显式展示）。</p>",
            "<form method=get action='/search'>",
            "<input name=q placeholder='如：咖啡喝烦了' value='%s'>"
            % html.escape(query), " <button>检索</button></form>"]
    if query:
        words = decompose_query(query)
        raw = query_raw_words(query)
        body.append("<div class='card'><b>查询分解</b>：原始词 "
                    + "".join("<span class='tag'>%s</span>" % html.escape(w) for w in raw)
                    + " → 扩展词 "
                    + "".join("<span class='tag dim'>%s</span>" % html.escape(w) for w in words)
                    + "</div>")
        st = _store(user)
        res = st.retrieve(query, k=5)
        if not res:
            body.append("<p class='warn'>无命中 —— 检查场景词抽取/词表覆盖。</p>")
        for it, cnt in res:
            med, hitw = _medium_of(decompose_query(query), it.scene_words or [],
                                   raw_words=query_raw_words(query))
            body.append(
                "<div class='hit'><b>%s</b> %s<br>"
                "<span class='note'>日期 %s · 标签 %s · 存储 %.0f · 提取 %d · 浮现 %d</span><br>"
                "<span class='medium'>媒介 = %s（命中 %d 词）</span></div>"
                % (html.escape(it.id), html.escape(it.content), it.date,
                   html.escape(it.tags or ""), it.storage, it.extraction_at(),
                   it.evoked_count, html.escape(med), cnt))
    return page("检索测试", "".join(body), "/search")


# ═══════════════════════════════════════════════════════════
# ④ 注入块导出
# ═══════════════════════════════════════════════════════════
def view_inject(user: str, q: dict) -> str:
    query = q.get("q", "").strip()
    body = ["<h1>④ 注入块导出</h1>",
            "<p class='sub'>L2 关系包 + L3 命中 → 打包成可复制文本。"
            "这就是喂给芽衣的东西，看对不对。</p>",
            "<form method=get action='/inject'>",
            "<input name=q placeholder='当前用户的话，如：今天下雨了' value='%s'>"
            % html.escape(query), " <button>打包</button></form>"]

    prof = _l2_profile(user)
    body.append("<h2>L2 关系包（上半身）</h2>")
    if prof is not None:
        md = prof.to_markdown()
        body.append("<div class='card'><pre>%s</pre>"
                    "<p class='note'>状态（系统侧，不进注入文本）：%s · 覆盖事件 %d 条</p></div>"
                    % (html.escape(md or "（空）"),
                       html.escape(prof.distill_status), prof.events_seen))
    else:
        body.append("<p class='note'>（无 L2 缓存 —— 需先蒸馏或规则重建）</p>")

    body.append("<h2>L3 命中 + 打包结果</h2>")
    try:
        from kb.memory.service import open_user_memory
        from kb.memory.bundle import build_bundle
        m = open_user_memory(user)
        hits = m.l3_hits(query, k=3) if query else []
        b = build_bundle(prof, hits)
        body.append("<div class='card'><pre>%s</pre>"
                    "<p class='note'>系统侧：l2_status=%s · 命中 %d · 截断 %s</p></div>"
                    % (html.escape(b.text or "（空）"),
                       html.escape(b.l2_status), b.n_memory, b.truncated))
        if hits:
            body.append("<h2>命中明细（含媒介，仅供观测）</h2>")
            for h in hits:
                body.append("<div class='hit'>%s<br><span class='medium'>%s%s</span></div>"
                            % (html.escape(h.item.content), html.escape(h.medium),
                               " · 不应期降级" if h.faded else ""))
    except Exception as e:
        body.append("<pre class='bad'>%s</pre>" % html.escape(str(e)))
    return page("注入块导出", "".join(body), "/inject")


# ═══════════════════════════════════════════════════════════
# 首页
# ═══════════════════════════════════════════════════════════
def view_home(user: str) -> str:
    body = ["<h1>L3 观测台</h1>",
            "<p class='sub'>当前用户：<b>%s</b> · 四个视图，每页回答一个问题。</p>"
            % html.escape(user),
            "<div class='card'><h2 style='margin-top:0'>① 记账审计</h2>"
            "<p class='note'>书记员犯没犯错？—— 原始输出 vs 可解析草稿 vs 入库行。</p>"
            "<a href='/ledger'><button>进入</button></a></div>",
            "<div class='card'><h2 style='margin-top:0'>② L3 全字段</h2>"
            "<p class='note'>入场灵不灵？—— 条目池全字段、沉睡标灰、规则触发日志。</p>"
            "<a href='/l3'><button>进入</button></a></div>",
            "<div class='card'><h2 style='margin-top:0'>③ 检索测试</h2>"
            "<p class='note'>找得准不准？—— 命中条目 + 命中媒介（中间变量显式）。</p>"
            "<a href='/search'><button>进入</button></a></div>",
            "<div class='card'><h2 style='margin-top:0'>④ 注入块导出</h2>"
            "<p class='note'>给芽衣的东西对不对？—— L2 关系包 + L3 命中打包。</p>"
            "<a href='/inject'><button>进入</button></a></div>"]
    return page("首页", "".join(body), "/")


# ═══════════════════════════════════════════════════════════
# HTTP 服务
# ═══════════════════════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):
    user = "u_e2e"

    def _send(self, s: str, code: int = 200):
        data = s.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == "/ledger":
                self._send(view_ledger(self.user, q))
            elif u.path == "/l3":
                self._send(view_l3(self.user))
            elif u.path == "/search":
                self._send(view_search(self.user, q))
            elif u.path == "/inject":
                self._send(view_inject(self.user, q))
            elif u.path == "/":
                self._send(view_home(self.user))
            else:
                self._send(page("404", "<h1>404</h1>", ""), 404)
        except Exception as e:
            import traceback
            self._send("<pre>%s</pre>" % html.escape(traceback.format_exc()), 500)

    def log_message(self, *a):
        pass   # 静默


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--user", default="u_e2e")
    a = ap.parse_args()
    Handler.user = a.user
    srv = HTTPServer(("127.0.0.1", a.port), Handler)
    print("L3 观测台 → http://127.0.0.1:%d/   (user=%s, Ctrl+C 停)" % (a.port, a.user))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停。")


if __name__ == "__main__":
    main()
