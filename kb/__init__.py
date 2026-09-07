"""Roleplay-Eval 知识库检索模块。

芽衣的「认知外挂层」。这里承载 V1 攻略库 + V2 背景知识的检索。
核心原则:定义清晰的检索接口(BaseRetriever),当前用「查表/模块加载」实现,
未来数据规模化后可热切换到向量检索——接口不变,上层调用方零改动。

详见 docs/knowledge_architecture.md。
"""
from .retriever import (
    BaseRetriever,
    Retrieved,
    VectorRetrieverStub,
    GuideRetriever,
    LoreRetriever,
    build_kb,
)

__all__ = [
    "BaseRetriever",
    "Retrieved",
    "VectorRetrieverStub",
    "GuideRetriever",
    "LoreRetriever",
    "build_kb",
]
