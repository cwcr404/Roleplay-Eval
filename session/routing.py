# coding: utf-8
"""session.routing —— 产品线「首行自报标记」的解析/剥离/缺省（纯函数层）。

核心契约（org orchestration_rebuild.md §二/验收洞1·洞2 写死）：
- 内嵌路由：agent2 在回复**首行**自报 `[闲聊]` / `[攻略]`，engine 靠它分叉。
- **标记只作 engine 分叉依据，绝不进玩家可见流**（洞1：玩家侧全文 0 命中标记）。
- **缺省分支写死**（洞2）：首行解析失败（自由发挥/空行/emoji 开头/吐不出标记）
  → 默认按 [闲聊] 走，不重试、不阻塞、记一条日志（含原始首行原文）。

本模块只做文本判定，不碰 LLM / 不碰记忆 —— 可单独离线测。
"""
from __future__ import annotations

import re
from typing import Optional

# 合法标记取值（双模：闲聊/攻略；[闲聊] 又是缺省分支的落点）
CHAT_MODALITY = "闲聊"
GUIDE_MODALITY = "攻略"
_VALID = frozenset({CHAT_MODALITY, GUIDE_MODALITY})

# 首行标记形如： [闲聊] / [攻略] / ［闲聊］ / ［攻略 ］(改二:容忍全角括号+内部空白)
#   —— 半角括号、全角括号都认;空格/全角空格在标签内与括号前后可容忍。
#   注意:半角开『[』+全角闭『］』这类“混搭括号”也会被认 —— 这是【有意放宽】,
#   容忍度本就为意外输入设的(LLM 偶发混排/粘贴错位),未来勿当 bug “修掉”收紧它。
_MARKER_OPEN = r"[\[［]"      # [ 或 ［
_MARKER_CLOSE = r"[\]］]"     # ] 或 ］
_MARKER_WS = r"[ \t\u3000]*"      # 半角/全角空格容忍
_MARKER_RE = re.compile(rf"^\s*{_MARKER_OPEN}{_MARKER_WS}(闲聊|攻略){_MARKER_WS}{_MARKER_CLOSE}{_MARKER_WS}(.*)$", re.S)


def modality_from_first_line(first_line: str) -> Optional[str]:
    """从『首行文本』解析出标记；无合法标记返回 None（供缺省走 [闲聊]）。

    Args:
        first_line: 玩家可见流里取出的首行原文（可能含 [闲聊]/[攻略]，可能没有）。
    Returns:
        "闲聊" / "攻略"；解析不到合法标记则为 None（调用方负责按缺省 [闲聊] 落）。
    """
    m = _MARKER_RE.match(first_line or "")
    if m:
        return m.group(1)
    return None


def strip_marker(first_line: str) -> str:
    """把首行里的标记子串剥掉，返回不含标记的正文起始（洞1 剥离）。

    只剥离行首成形的 `[闲聊]`/`[攻略]` 标记本身；若首行不含成形标记则原样返回
    （缺省分支由调用方决定不放行自由文本,见 decompose_first_chunk）。
    注意:剥的是『标记 token』,正文后续的换行/空白原样保留给上层继续拼流。
    """
    m = _MARKER_RE.match(first_line or "")
    if m:
        return m.group(2)  # 标记后的正文（可为空串 -> 正文从下一行开始）
    return first_line


def decompose_first_chunk(chunk: str, *, eol: str = "\n") -> tuple[Optional[str], str, str]:
    """把『流式首块』按行拆解,产出 (modality, 已剥离标记的首行剩余, 缓冲待定)。

    洞1「先解析再放行」两段式的纯函数基础：
      - 首块可能只含半行（标记还没吐完 / 正文没到），或整行已经齐。
      - 这里返回：
          modality      = 从首块首行解析出的标记（None=缺省走 [闲聊]）
          safe_prefix   = 首行去掉标记后可安全放行的正文开头（空=不要放行任何东西）
          pending       = 无法判定需继续缓冲的部分（下一块到了再拼）
    跨块拼行由流式组装层（engine，非本模块）负责推进；本模块只保证『单次判定』
    与『标记剥离』正确、可单测。
    """
    if "\n" not in chunk and not chunk.endswith(eol):
        # 首块甚至还没遇到换行 -> 整块存疑,不判定不放行
        return None, "", chunk
    # 遇到换行:首行 = 到第一个换行为止
    brk = chunk.find(eol)
    first_line = chunk[:brk]
    rest = chunk[brk + len(eol):]
    mod = modality_from_first_line(first_line)
    # 有合法标记才可能从可见流剥掉它并放行正文;否则整段按缺省处理(调用方日志记原文)
    if mod is not None:
        safe = strip_marker(first_line)
        return mod, safe, rest
    return None, "", first_line + eol  # 无标记:首行整体进缺省(不放行,原文留待日志)
