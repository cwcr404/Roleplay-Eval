# coding: utf-8
"""utils_stream_smoke —— stream_chat(真 SSE 出口)的离线解析桩(0 API,钉解析逻辑)。

不烧 token:用假 urlopen 喂 canned SSE 帧,验 stream_chat 的逐块/跳过/[DONE]/断流语义,
避免真 DeepSeek 冒烟时因解析 bug 白花钱。串起来的三块:
  1. 正常流:两块正文 + [DONE] → 恰得两块,含正文。
  2. keepalive/空/非data行:全部跳过,不吐脏块。
  3. 断流(urlopen 中途抛) → DeepSeekError。
用法: python -m session.stream_chat_probe
"""
from __future__ import annotations

import io
import sys

# --- 注入假 SSR 帧源(不 import utils.api 的 urllib 发起真网络) ---
import utils.api as A

def _serve(frames: list[bytes]):
    """返回一个假响应对象:iter 逐行吐出已含换行的帧。"""
    class _Reader:
        def __iter__(self):
            for f in frames:
                yield f
    class _Resp:
        def __enter__(self): return _Reader()
        def __exit__(self, *a): return False
    return _Resp()


def _run() -> int:
    ok = 0
    def chk(name, cond):
        nonlocal ok
        ok += 1
        assert cond, name

    A.API_KEY = "sk-test"  # 挡住 urlopen 前的空 key 早抛

    # 1. 正常流两块+[DONE]
    frames = [
        b'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n',
        b'data: {"choices":[{"delta":{"content":"[\\u95f2\\u804a]\\u4f60\\u597d"}}]}\n',   # → 「[闲聊]你好」
        b'data: {"choices":[{"delta":{"content":"\\uff0c\\u6211\\u5728\\u3002"}}]}\n',  # → 「,我在。」
        b'data: [DONE]\n',
    ]
    real_old = A.urllib.request.urlopen

    def fake_urlopen(req, *a, **k):
        return _serve(frames)
    A.urllib.request.urlopen = fake_urlopen
    got = "".join(A.stream_chat("sys", "user"))
    chk("normal:正文逐块聚合", got == "[闲聊]你好，我在。")   # 注意:全角逗号(真实 SSE 产全角)
    A.urllib.request.urlopen = real_old

    # 2. keepalive/空/注释/事件行都不吐块
    frames2 = [
        b': keep-alive comment\n',
        b'\n',
        b'id: 1\n',
        b'data: {"choices":[{"delta":{"content":"ok"}}]}\n',
        b'event: x\n',
        b'blah line no data prefix\n',
        b'data: [DONE]\n',
    ]
    A.urllib.request.urlopen = lambda req, *a, **k: _serve(frames2)
    got2 = "".join(A.stream_chat("s", "u"))
    chk("skip:仅正文块被收,噪声全滤", got2 == "ok")
    A.urllib.request.urlopen = real_old

    # 3. 断开(urlopen 中途抛) → DeepSeekError, 且已收部分不丢(generator 中断前
    #    已 yield 的仍被上游合计聚合 —— 由 engine 回退同步兜住,这里验能收到异常)
    class _Boom:
        def __enter__(self): raise RuntimeError("conn reset")
        def __exit__(self, *a): return False
    A.urllib.request.urlopen = lambda req, *a, **k: _Boom()
    raised = False
    try:
        list(A.stream_chat("s", "u"))
    except A.DeepSeekError:
        raised = True
    chk("断流:抛 DeepSeekError", raised)
    A.urllib.request.urlopen = real_old

    # 4. 空 key 早抛(不真发网)
    A.API_KEY = ""
    raised = False
    try:
        list(A.stream_chat("s", "u"))
    except A.DeepSeekError:
        raised = True
    chk("空key:早抛不真连网", raised)
    A.API_KEY = "sk-test"

    print(f"stream_chat_probe: {ok} 断言全过")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_run())
    except AssertionError as e:
        print("stream_chat_probe FAIL @", e)
        raise SystemExit(1)
