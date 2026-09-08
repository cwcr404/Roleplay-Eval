"""DeepSeek API 客户端 —— v0.1 链路的地基。

职责:封装对 DeepSeek 的调用,供被测 Agent(三 Agent)和 Judge 复用。
密钥从 ../.env 读取,不进代码、不进 git。
"""
import os
import json
import time
import urllib.request
from collections.abc import Iterator

from dotenv import load_dotenv

# 加载 .env(项目根目录)
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

API_URL = "https://api.deepseek.com/chat/completions"
API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")


class DeepSeekError(Exception):
    """DeepSeek 调用失败。"""


def chat(
    system_prompt: str,
    user_message: str,
    model: str = MODEL,
    temperature: float = 0.7,
    max_retries: int = 1,
) -> str:
    """调用 DeepSeek,返回回复文本。失败重试一次(对应 Judge JSON 崩坏重跑策略)。"""
    if not API_KEY:
        raise DeepSeekError("未找到 DEEPSEEK_API_KEY(请检查 roleplay-eval/.env)")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": temperature,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
    )

    last_err = None
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body["choices"][0]["message"]["content"].strip()
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < max_retries:
                time.sleep(1.0)  # 简单退避
    raise DeepSeekError(f"DeepSeek 调用失败: {last_err}")


# ---------------------------------------------------------------------------
# 流式通道(产品线主回复出口 —— 0908 三合一接线第一步)
# ---------------------------------------------------------------------------
# 契约:一个『流式 LLM 出口』是同步生成器,逐块 yield 文本(str),直到吐完。
#   流式出口签名:  def stream(system, user, *, model=None, **kw) -> Iterator[str]
# 首块即出 → 前端能最早开始打字;后续块陆续到 → 打字机效果。
# 同步聚合 helper 把生成器收成完整文本(供回复落地/评测/记录)。
#
# 现在【不接真 DeepSeek SSE】 —— 那是施工图 §九步骤1 的最后一步(接口坑:首块
# 取标记/换行剥离)。此处先落契约 + 假流式桩,证明『分块→首字快→聚合无损』
# 机制可行,且不碰现有同步 chat()(评测线/记忆锚零影响)。


def stream_to_text(stream: Iterator[str]) -> str:
    """把一个流式生成器聚合成完整文本。任一块失败向上抛(调用方决定回退)。"""
    parts: list[str] = []
    for ch in stream:
        if ch is None:
            continue
        parts.append(ch)
    return "".join(parts)


def fake_stream_chat(system: str, user: str, *,
                     model: str = MODEL,
                     text: str | None = None,
                     chunk: int = 24,
                     **kw) -> Iterator[str]:
    """假流式 LLM 出口:按 chunk 长度把 text 切成块逐块 yield(模拟打字机)。

    用于离线接线测试 —— 不烧 API、可注入 engine 做『首字快/分块到达/聚合』
    验证。text 缺省给一段占位回复(含可选的 [闲聊] 首行标记,供后续路由测试)。
    返回同步生成器(惰性),消费时才产出 —— 与真 SSE 的缓冲块语义对得上。
    """
    if text is None:
        text = "[闲聊]（温和地笑了笑）我在呢。你想聊点什么?"
    # 惰性生成器:首块必须『一迭代就立刻可得』,不整体预载 —— 模拟首字快
    body = text
    i = 0
    n = max(1, int(chunk))
    while i < len(body):
        yield body[i:i + n]
        i += n


def stream_chat(system: str, user: str, *,
                model: str = MODEL,
                temperature: float = 0.8,
                max_retries: int = 1,
                **kw) -> Iterator[str]:
    """真 DeepSeek SSE 流式出口(产品线主回复·同步生成器,逐块 yield 文本)。

    与同步 chat() 共用 .env 的 key/base;评测线/记忆锚不碰 —— 只有显式配了
    llm_stream 的引擎才走这里。块语义:SSE 每个 data 帧的 delta.content 是一块;
    [DONE] 后停。失败:头部建连/认证错 → 向上抛(调用方决定回退同步 chat());
    中途断流 → 把已收的文本照常 yield 完再抛,让局部回复不丢(宁半个不丢整句)。
    """
    if not API_KEY:
        raise DeepSeekError("未找到 DEEPSEEK_API_KEY(请检查 roleplay-eval/.env)")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "stream": True,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
    )

    # 流式不自动重试:连建/认证错抛一次让调用方回退;中途断流不重试(半条已 yield,
    # 重试错接更糟)。调用方(engine._main_reply)本就 catch 后回退同步 chat() 兜底。
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            buf = b""
            for raw in resp:
                buf += raw
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    s = line.decode("utf-8", "replace").strip()
                    if not s or not s.startswith("data:"):
                        continue  # 空行/注释/事件行 -> 跳过
                    tok = s[len("data:"):].strip()
                    if tok == "[DONE]":
                        return
                    try:
                        obj = json.loads(tok)
                    except ValueError:
                        continue
                    try:
                        delta = (obj["choices"][0]["delta"].get("content") or "")
                    except (KeyError, IndexError, TypeError):
                        delta = ""
                    if delta:
                        yield delta
    except Exception as e:  # noqa: BLE001
        raise DeepSeekError(f"DeepSeek 流式中断: {e}")
