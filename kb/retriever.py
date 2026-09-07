"""Roleplay-Eval 知识库检索层 —— 接口与实现。

这是整份「留接口、未来可切向量」设计的核心。三层结构:

    BaseRetriever          ← 抽象检索接口(消费方只依赖它)
        ├── GuideRetriever     ← V1 攻略:标签查表(v0.1 默认)
        └── LoreRetriever      ← V2 背景:主题/维度整块加载(v0.1 默认)
        └── VectorRetrieverStub ← 向量化替换的**占位契约**(规模化后再热切换)

关键设计决策(也是开源报告要写清的):
- **消费方(kb 的使用者:Agent2 接入)只依赖 BaseRetriever.query()**,
  不感知底层是查表还是向量。这就是「接口与实现分离」。
- 未来数据涨到几千条、用户问法变模糊 -> 新增一个 VectorRetriever 实现,
  内部换 embedding + 向量库;query() 签名不变,上层零改动。
- V1(工具知识)可平滑升级到「语义召回」;V2 由于是连贯叙事,
  即便上向量也应保持『整块检索』语义,而非碎片 top-k(见 v2_lore 模块注释)。

数据流:
    text 源文件
      -> parsers 解析成结构化条目/叙事块
      -> 每类建自有索引(标签倒排 / 主题维度索引)
      -> query(topic_keyword) 返回命中的整块/条目(list[Retrieved])
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from .parsers.v1_guide import parse_guide, build_guide_index
from .parsers.v2_lore import parse_lore, build_lore_index

_KB_DIR = os.path.dirname(os.path.abspath(__file__))


@dataclass
class Retrieved:
    """一次检索命中的结果(整块/整条,不做碎片拼接)。"""

    source: str          # "v1_guide" / "v2_lore"
    key: str             # 命中的标签/主题,用于追溯
    domain: str          # 主域/维度,便于按需过滤
    content: str         # 完整可用的文本(整块)
    score: float = 1.0   # 查表命中=1.0;向量未来可给相似度


class BaseRetriever(ABC):
    """抽象检索接口。任何接入方(Agent2、将来的记忆系统)只依赖它。

    一个实现 = 一种知识来源 + 一种检索策略。query() 返回整块结果。
    """

    @abstractmethod
    def query(self, text: str, *, top_k: int = 5,
              domain: Optional[str] = None,
              as_of_time: Optional[float] = None) -> list[Retrieved]:
        """按文本/关键词检索,返回命中的整块内容。

        Args:
            text:  自然语言描述或关键词(如 "琪亚娜" / "雷之律者" / "配队")。
            top_k: 返回上限(查表命中通常远小于此,保留参数以兼容向量实现)。
            domain: 可选主域过滤(如 "武器" / "系统")。
            as_of_time: 时间旅行锚点(秒级时间戳)。**默认 None = 当下**。
                V1/V2 这类无时间维度的检索器不传(忽略即可);
                仅记忆检索器(MemoryRetriever)用它回放某历史时间点的快照。

        Returns:
            命中的 Retrieved 列表,已按相关度降序(向量实现时 score 生效)。
        """
        # 签名即契约(宪法级,一次定型):as_of_time 默认 None = 当下。
        # Guide/Lore 无时间维度 -> 忽略该参数;Phase 2 MemoryRetriever
        # (docs/memory_architecture.md)用它做「好感度=f(事件流,时间戳)」回放。
        # 为使抽象基类不可被裸实例化,这里抛错:具体子类必须实现真正的 query。
        raise NotImplementedError


# ---------------------------------------------------------------------------
# V1 攻略库 —— 标签查表实现(v0.1 默认)
# ---------------------------------------------------------------------------

class GuideRetriever(BaseRetriever):
    """V1 攻略:基于标签精确查表。无外部依赖、零成本、可复现。

    每次启动从源文件重新解析 + 建索引(体量 <5KB,开销可忽略)。
    命中策略:解析用户文本里的关键词 token,对倒排索引做包含匹配。
    """

    def __init__(self, source: Optional[str] = None):
        source = source or os.path.join(_KB_DIR, "sources", "v1_guide.txt")
        with open(source, encoding="utf-8") as f:
            text = f.read()
        self._entries = parse_guide(text)
        self._index = build_guide_index(self._entries)

    @property
    def size(self) -> int:
        return len(self._entries)

    def query(self, text: str, *, top_k: int = 5,
              domain: Optional[str] = None,
              as_of_time: Optional[float] = None) -> list[Retrieved]:
        needles = _meaningful_needles(text)   # 主查询串(去掉助词/语气)
        hits: list[Retrieved] = []
        seen = set()
        candidates: list[tuple[int, dict]] = []

        for ent in self._entries:
            hay = (ent["tags"][0] if ent["tags"] else "") + " " + ent["content"]
            if domain and ent["domain"] != domain:
                continue
            # 评分:命中越靠前的 needle、越多 needle 命中 -> 优先
            score = 0
            for i, nd in enumerate(needles):
                if nd and nd in hay:
                    # 命中的 needle 越接近完整 query 权重越高
                    score += max(1, len(nd))
            if score > 0:
                candidates.append((score, ent))

        # 高相关优先;同分按原文顺序(确定性)
        candidates.sort(key=lambda x: -x[0])
        for _, ent in candidates:
            ident = (ent["tags"][0], ent["content"][:40])
            if ident in seen:
                continue
            seen.add(ident)
            hits.append(Retrieved(
                source="v1_guide", key=ent["tags"][0], domain=ent["domain"],
                content=f"[{ent['tags'][0]}]\n{ent['content']}"))
            if len(hits) >= top_k:
                break
        return hits

    @staticmethod
    def _normalize_tokens(text: str) -> set[str]:
        return _meaningful_needles(text)


# ---------------------------------------------------------------------------
# V2 背景库 —— 主题/维度整块加载(v0.1 默认)
# ---------------------------------------------------------------------------

class LoreRetriever(BaseRetriever):
    """V2 背景知识:按主题/维度整块加载。核心是『整块』,绝不碎片召回。

    对话需要某段背景时(如聊到琪亚娜 / 乐土 / 雷电女王),
    命中 = 取出对应的那一整段叙事块,保证自洽、不拼贴、不出戏。
    """

    def __init__(self, source: Optional[str] = None):
        source = source or os.path.join(_KB_DIR, "sources", "v2_lore.txt")
        with open(source, encoding="utf-8") as f:
            text = f.read()
        self._blocks = parse_lore(text)
        self._index = build_lore_index(self._blocks)

    @property
    def size(self) -> int:
        return len(self._blocks)

    def query(self, text: str, *, top_k: int = 5,
              domain: Optional[str] = None,
              as_of_time: Optional[float] = None) -> list[Retrieved]:
        needles = self._normalize_tokens(text)
        # 要求至少一个≥3 字节有信息量的 needle 命中,过滤纯泛词噪音
        strong = {n for n in needles if len(n) >= 3}
        out: list[Retrieved] = []
        scored: list[tuple[int, LoreBlock]] = []
        for block in self._blocks:
            if domain and block.dimension != domain:
                continue
            hay = f"{block.key} {block.topic} {block.text}"
            hay_l = hay
            matched = [nd for nd in needles if nd and nd in hay_l]
            if not matched:
                continue
            # 必须有足够强的命中(整 3 字以上词),否则视为泛泛噪音
            strong_matched = [nd for nd in strong if nd and nd in hay_l]
            if not strong_matched:
                continue
            # 评分:key/topic 命中权重高(块本身主题相关) + 匹配字节总数
            key_bytes = sum(len(nd) for nd in strong_matched if nd in (block.key + block.topic))
            all_bytes = sum(len(nd) for nd in strong_matched)
            score = key_bytes * 3 + all_bytes
            scored.append((score, block))
        scored.sort(key=lambda x: -x[0])
        for _, block in scored[:top_k]:
            out.append(Retrieved(
                source="v2_lore", key=block.key, domain=block.dimension,
                content=block.text, score=1.0))
        return out

    @staticmethod
    def _normalize_tokens(text: str) -> set[str]:
        return _meaningful_needles(text)


# ---------------------------------------------------------------------------
# 向量化替换的占位契约(未来实现)
# ---------------------------------------------------------------------------

class VectorRetrieverStub(BaseRetriever):
    """『未来向量化实现』的占位契约 —— 不在 v0.1 提供真实现。

    为什么现在不实现、只留契约(开源报告的决策点之一):
    - 当前两库合计 <15KB,标签/模块检索在精度与速度上已足够;
    - 向量库的检索优势在『大规模海量 + 语义模糊』场景才显现(千条量级起);
    - 语义召回对工具类知识(V1)是增益;但对连贯叙事(V2)会引入碎片化风险,
      即使未来上向量,也应保持『整块』语义。
    - v0.1 若硬上向量:引入 embedding API 依赖、数据出境、成本与复杂度,
      换不回一丁点精度,且拖慢最小闭环。

    未来替换路径(数据涨到需要时):
        1. 实现同类:  `class GuideVectorRetriever(GuideRetriever)` 内部把
           每一条目 content embedding 进向量库(local 或 API);
        2. query() 改走『语义召回 + 可选 domain 过滤』;
        3. 上层 Agent2 调用方零改动 —— 只因它依赖的是 BaseRetriever.query()。
    """

    def __init__(self, message: str = "向量实现尚未启用(v0.1 用查表/模块加载)"):
        raise NotImplementedError(message)

    def query(self, text: str, *, top_k: int = 5,
              domain: Optional[str] = None,
              as_of_time: Optional[float] = None) -> list[Retrieved]:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 门面:一键构建整个 KB
# ---------------------------------------------------------------------------

def build_kb(retriever_mode: str = "plain") -> dict[str, BaseRetriever]:
    """构建知识库检索器集合。

    Args:
        retriever_mode: "plain"(默认,查表/模块) | "vector"(抛 NotImplementedError,
            未来开放)。

    Returns:
        {"guide": GuideRetriever, "lore": LoreRetriever}
    """
    if retriever_mode == "vector":
        # 预留切换点:这里改返回 VectorRetriever 实现即可,上层零改动。
        raise NotImplementedError(
            "向量模式尚未开放。v0.1 用 plain(查表/模块加载),请见 VectorRetrieverStub 注释。")
    return {
        "guide": GuideRetriever(),
        "lore": LoreRetriever(),
    }


# -- 内部小工具 -------------------------------------------------------------

# 助词/虚词/语气词——不含检索信息,匹配时剔除,避免噪音命中(中文分词不引入外部
# 依赖前,用『整串包含 + 去虚词』即可满足 v0.1 的目标检索精度)
_STOP = {
    "是", "的", "了", "吗", "呢", "吧", "啊", "哦", "什么", "怎么", "谁", "哪",
    "有没有", "给我", "讲讲", "一下", "说说", "告诉", "我觉得", "你觉", "和", "与",
    "或", "跟", "的武器", "是谁", "是什么", "是什", "这", "那", "知道", "了解",
    "想要", "想让", "帮我", "请问", "想", "要", "个", "会", "能", "可以",
    "关系", "感觉", "是不是", "现在", "你们", "你说", "你喜欢", "你喜", "的定位",
}


def _meaningful_needles(text: str) -> set[str]:
    """把查询提示词转成『主检索串』集合(v0.1 查表用整串包含,不分词)。

    策略(查表阶段够用、零外部依赖):
    1. 去掉常见助词虚词(_STOP);
    2. 保留连续有信息量的片段作 needle,做子串命中;
    3. 刻意不做 2-gram 展开 —— 2-gram 噪音太大(『真理』会误命中无关律者条目),
       4 字专有名词如『真理之钥』『雷电女王』以整词纳入即可覆盖。

    向量检索阶段可无痛切换为 embedding,这里的取舍不影响 BaseRetriever 接口,
    因此不阻塞『未来上向量』的演进。
    """
    import re
    out: set[str] = set()
    # 英文/数字 token
    for seg in re.findall(r"[A-Za-z0-9]+\b[\w-]*", text):
        out.add(seg.lower())
    # 去停用词,再按分隔符切出中文片段
    cleaned = text
    for w in sorted(_STOP, key=len, reverse=True):
        cleaned = cleaned.replace(w, " ")
    for seg in re.split(r"[\s,，。;；:：!?！？/()（）'\"【】\[\]]+", cleaned):
        seg = seg.strip()
        if not seg:
            continue
        out.add(seg)
        if re.search(r"[\u4e00-\u9fff]", seg):
            # 中文整段 + 内部用连词切分出的短语 + 4 字滑窗
            for part in re.split(r"[和与或及跟]", seg):
                part = part.strip()
                if not part:
                    continue
                out.add(part)
                # 3/4 字滑窗:让『琪亚娜』(3字)『往世乐土』(4字)之类的专有名词
                # 都能独立成 needle,被 length>=3 的『强命中』逻辑捕获。比 2-gram
                # 噪音小得多。含虚词的字窗无检索价值,丢弃。
                if len(part) >= 3:
                    for width in (3, 4):
                        for i in range(len(part) - width + 1):
                            w = part[i : i + width]
                            if not any(c in w for c in "的了我都你在里呀过把是就还要会去哪这那好和让给被"):
                                out.add(w)
    out.discard("")
    return out


def _any_token_in(tokens: set[str], hay: str) -> bool:
    hay_l = hay.lower()
    for t in tokens:
        if t and t in hay_l:
            return True
    return False


def _dedup_add(lst: list, seen: set, item: Retrieved) -> None:
    ident = (item.key, item.content[:40])
    if ident not in seen:
        seen.add(ident)
        lst.append(item)
