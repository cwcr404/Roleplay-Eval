"""DeepSeek API 客户端 —— v0.1 链路的地基。

职责:封装对 DeepSeek 的调用,供被测 Agent(三 Agent)和 Judge 复用。
密钥从 ../.env 读取,不进代码、不进 git。
"""
import os
import json
import time
import random
import urllib.request
from collections.abc import Iterator

from dotenv import load_dotenv

# 加载 .env(项目根目录)
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

API_URL = "https://api.deepseek.com/chat/completions"
API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")


# ---------------------------------------------------------------------------
# 用量账本(首府 2026-10-07 裁定 (a):账目要能自动出数)
# ---------------------------------------------------------------------------
# 纪律:**只加不改**。读 usage 是**追加事件**,不改任何状态/参数/prompt。
# 字段(首府指定四个 + 单轮耗时):
#   prompt_tokens / completion_tokens / prompt_cache_hit_tokens /
#   prompt_cache_miss_tokens / seconds / kind / model
# 缓存命中价差近十倍 → 15 轮里 prompt 越滚越长,命中率直接决定单位成本曲线,
# 是后续 300 人成本模型的实测地基。写账**绝不影响**主调用返回值/异常路径。
_USAGE_LOG: list = []


def _record_usage(body: dict, *, kind: str, model: str,
                  seconds: float = 0.0) -> dict:
    """从返回体读 usage 并追加记账。失败静默(记账不是主链路)。"""
    try:
        u = (body or {}).get("usage") or {}
        rec = {
            "kind": kind,
            "model": model,
            "prompt_tokens": int(u.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(u.get("completion_tokens", 0) or 0),
            "prompt_cache_hit_tokens": int(u.get("prompt_cache_hit_tokens", 0) or 0),
            "prompt_cache_miss_tokens": int(u.get("prompt_cache_miss_tokens", 0) or 0),
            "seconds": round(float(seconds), 3),
        }
        _USAGE_LOG.append(rec)
        _path = os.getenv("DEEPSEEK_USAGE_JSONL", "")
        if _path:
            with open(_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec
    except Exception:  # noqa: BLE001
        return {}


def usage_snapshot() -> dict:
    """进程内用量汇总(供账目脚本读)。不改状态,纯查询。"""
    agg = {"calls": len(_USAGE_LOG), "prompt_tokens": 0, "completion_tokens": 0,
           "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0,
           "seconds": 0.0, "by_kind": {}}
    for r in _USAGE_LOG:
        for k in ("prompt_tokens", "completion_tokens",
                  "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            agg[k] += r.get(k, 0)
        agg["seconds"] = round(agg["seconds"] + r.get("seconds", 0.0), 3)
        bk = agg["by_kind"].setdefault(r.get("kind", "?"),
                                       {"calls": 0, "prompt_tokens": 0,
                                        "completion_tokens": 0,
                                        "prompt_cache_hit_tokens": 0,
                                        "prompt_cache_miss_tokens": 0})
        bk["calls"] += 1
        for k in ("prompt_tokens", "completion_tokens",
                  "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            bk[k] += r.get(k, 0)
    return agg


def usage_reset() -> None:
    """清空进程内账(测试用;不碰任何持久状态)。"""
    _USAGE_LOG.clear()


class DeepSeekError(Exception):
    """DeepSeek 调用失败。"""


def chat(
    system_prompt: str,
    user_message: str,
    model: str = MODEL,
    temperature: float = 0.7,
    max_retries: int = 1,
) -> str:
    """调用 DeepSeek,返回回复文本。失败重试一次(对应 Judge JSON 崩坏重跑策略)。

    施工纪律(首府 2026-10-07 裁定 (a),只加不改):
      本函数对返回体**只做一件事** —— 追加读 usage 写账(见 _record_usage)。
      prompt 拼装、参数、错误处理路径**一行不改**,行为对调用方完全不变,
      仍返回 str。这是**追加事件**,不是改状态。
    """
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
            _t0 = time.time()
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                _record_usage(body, kind="chat", model=model,
                              seconds=time.time() - _t0)
                return body["choices"][0]["message"]["content"].strip()
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < max_retries:
                # 指数退避 + 抖动:1s×2^attempt 基础上加 [0,0.5)s 抖动,
                # 避免多请求同时重试造成的节拍对齐(惊群)。无新依赖。
                backoff = 1.0 * (2 ** attempt)
                time.sleep(backoff + random.uniform(0.0, 0.5))
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
        # 追加:让末帧带上 usage(不设则流式不返回用量)。
        # 只影响服务端多回一个数据帧,不影响 yield 的正文内容/顺序/异常路径。
        "stream_options": {"include_usage": True},
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
    _t0 = time.time()
    _usage_holder: dict = {}
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
                        _record_usage(_usage_holder, kind="stream", model=model,
                                      seconds=time.time() - _t0)
                        return
                    try:
                        obj = json.loads(tok)
                    except ValueError:
                        continue
                    # 末帧(include_usage)带 usage,正文为空 —— 只记账,不 yield
                    if obj.get("usage"):
                        _usage_holder.clear()
                        _usage_holder.update(obj)
                    try:
                        delta = (obj["choices"][0]["delta"].get("content") or "")
                    except (KeyError, IndexError, TypeError):
                        delta = ""
                    if delta:
                        yield delta
    except Exception as e:  # noqa: BLE001
        # 中途断流也把已收用量记账(若末帧已到)
        _record_usage(_usage_holder, kind="stream-partial", model=model,
                      seconds=time.time() - _t0)
        raise DeepSeekError(f"DeepSeek 流式中断: {e}")
