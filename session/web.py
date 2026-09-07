# coding: utf-8
"""session.web —— stdlib 单页 demo 前端（零第三方依赖）。

定位:给「有没有真的让芽衣边聊边记住」一个可点的面。单用户、内存态历史,
仅供本地 demo / 联调;真要上线走 WSGI/前端轮询的正式形态是后话,这里先把
『引擎露出』做对 —— web 只是 SessionEngine 的一个薄消费者,不掺业务。

约定(保持人格/关系边界):
- POST /turn {user, msg} -> {reply, affinity, tier, ritual_blocked, ev}
- GET  /state            -> {user, turns, affinity, tier, ritual_blocked}
- POST /reset            -> 清空引擎历史(账本不清 —— 账本是宪法级,web 无权抹)
页面:GET / 返回单页,底部输入框,一次一答(纯 fetch,无轮询)。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .engine import SessionEngine

_INDEX_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>芽衣 · 会话 demo</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 *{box-sizing:border-box}
 body{font-family:-apple-system,'Segoe UI','Microsoft YaHei',system-ui,sans-serif;
      margin:0;background:#0f1424;color:#eae9f2;height:100vh;display:flex;flex-direction:column}
 header{padding:12px 18px;background:#1a2140;border-bottom:1px solid #2c3560;
        display:flex;align-items:baseline;gap:12px}
 header h1{font-size:16px;margin:0;color:#c8b6ff}
 #rel{font-size:12px;color:#8f97c8}
 .wrap{flex:1;overflow-y:auto;padding:18px;display:flex;flex-direction:column;gap:10px}
 .row{display:flex;max-width:78%}
 .row.u{align-self:flex-end;justify-content:flex-end}
 .row.m{align-self:flex-start}
 .bub{padding:9px 13px;border-radius:14px;line-height:1.5;white-space:pre-wrap;
      word-break:break-word}
 .row.u .bub{background:#3b2f6e;border-top-right-radius:4px}
 .row.m .bub{background:#232a4d;border-top-left-radius:4px}
 .meta{font-size:11px;color:#7b83b0;margin-top:3px}
 .row.u .meta{text-align:right}
 .err{color:#ff7a7a;font-size:12px;padding:2px 6px}
 footer{padding:10px 14px;display:flex;gap:8px;background:#1a2140;
        border-top:1px solid #2c3560}
 #inp{flex:1;padding:11px 13px;border-radius:12px;border:1px solid #3a4666;
      background:#10152e;color:#eae9f2;font-size:15px;outline:none}
 #inp:focus{border-color:#6b5fc7}
 #send{background:#6b5fc7;border:0;color:#fff;border-radius:12px;padding:0 18px;font-size:15px;
       cursor:pointer}
 #send:disabled,#send.busy{opacity:.5;cursor:wait}
 .dot{display:inline-block;width:7px;height:7px;margin-right:5px;border-radius:50%;
      background:#57d39b;vertical-align:middle}
</style></head>
<body>
<header>
  <h1>雷电芽衣</h1>
  <div id="rel">连接中…</div>
</header>
<div class="wrap" id="log"></div>
<footer>
  <input id="inp" placeholder="对芽衣说点什么…" autocomplete="off">
  <button id="send">发送</button>
</footer>
<script>
var log=document.getElementById('log'),inp=document.getElementById('inp'),
    send=document.getElementById('send'),rel=document.getElementById('rel');
function el(kind,cls,txt){var e=document.createElement(kind);if(cls)e.className=cls;
 if(txt!=null)e.textContent=txt;return e;}
function bubble(msg,who){
 var row=el('div','row '+who);
 var box=el('div');
 box.appendChild(el('div','bub',msg));
 row.appendChild(box);log.appendChild(row);
 log.scrollTop=log.scrollHeight;return row;}
function errmsg(t){var e=el('div','err','⚠ '+t);log.appendChild(e);log.scrollTop=log.scrollHeight;}
function setBusy(b){send.disabled=b;send.classList.toggle('busy',b);
 send.textContent=b?'…':'发送';}
function renderRel(s){if(!s)return;rel.textContent=(s.tier||'')+'  ·  亲和 '+s.affinity;
 }
function post(path,body,method){
 var m=method||'POST';var opt={method:m};
 if(body!=null){opt.headers={'Content-Type':'application/json'};
  opt.body=JSON.stringify(body);}
 return fetch(path,opt).then(function(r){return r.json().catch(function(){return {}})
  .then(function(d){if(!r.ok)throw new Error((d&&d.error)||('HTTP '+r.status));return d;});});}
function loadState(){
 post('/state',null,'GET').then(renderRel).catch(function(e){errmsg('读状态失败: '+e.message);});}
function doSend(){
 var msg=inp.value.trim();if(!msg||send.disabled)return;
 var user=bubble(msg,'u');inp.value='';setBusy(true);
 var pending=bubble('','m');pending.firstChild.firstChild.textContent='…';
 post('/turn',{msg:msg}).then(function(d){
   if(!d||typeof d.reply!=='string')throw new Error('后端返回异常(无 reply)');
   pending.firstChild.firstChild.textContent=d.reply;
   var mb=el('div','meta',(d.ritual_blocked?'仪式未过 · ':'')+
      '亲和 '+(d.affinity)+' · '+(d.tier||'')+(d.ev?('  · 记得: '+d.ev):''));
   pending.firstChild.appendChild(mb);
   renderRel({tier:d.tier,affinity:d.affinity});
 }).catch(function(e){pending.remove();user.querySelector('.bub').textContent=
    msg;errmsg('发送失败: '+e.message);})
  .finally(function(){setBusy(false);});}
send.addEventListener('click',doSend);
inp.addEventListener('keydown',function(e){if(e.key==='Enter')doSend();});
loadState();
</script></body></html>"""


def run_web(eng: SessionEngine, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), lambda *a, **k:
                                 _Handler(eng, *a, **k))
    print(f"芽衣 web demo: http://{host}:{port}  (Ctrl+C 停)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nweb 已停。")


class _Handler(BaseHTTPRequestHandler):
    engine: SessionEngine  # 由 run_web 闭包注入(每请求实例化时给到)

    def __init__(self, eng: SessionEngine, *a, **k):
        self.engine = eng
        super().__init__(*a, **k)

    # 关日志噪音
    def log_message(self, *a):  # noqa: D401
        pass

    def _send(self, code: int, obj: dict, ctype: str = "application/json"):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._write(code, body, ctype)

    def _write(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 永不缓存 —— 避免开发者/测试者强刷后仍拿到旧页面旧JS
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            body = _INDEX_HTML.encode("utf-8")
            self._write(200, body, "text/html; charset=utf-8")
            return
        if path == "/state":
            self._send(200, self.engine.state())
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/turn":
            data = self._read_json()
            msg = (data.get("msg") or "").strip()
            if not msg:
                self._send(400, {"error": "empty msg"})
                return
            r = self.engine.turn(msg)
            self._send(200, {
                "reply": r.reply,
                "affinity": r.affinity,
                "tier": r.tier_key,
                "ritual_blocked": r.ritual_blocked,
                "ev": (r.event_type if r.event_recorded else ""),
            })
            return
        if path == "/reset":
            self.engine.reset()
            self._send(200, {"ok": True})
            return
        self._send(404, {"error": "not found"})
