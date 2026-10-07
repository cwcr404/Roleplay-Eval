# coding: utf-8
"""kb.memory.roles —— 角色词典（专有名词层，零 LLM）。

首府 2026-10-07 工单 P0-3 裁定：
- 角色词典是**专有名词层**，零 LLM、词法匹配，与情绪探针同款模式。
- **挂倒排索引，不学情绪走独立通道** —— 依据：角色名是专有名词，本就是倒排
  索引的合法居民；情绪词才需隔离。两类语义两条纪律。
- 零命中纪律沿用：**不猜角色，没提不亮**。

与「情绪通道独立」的区别（两条纪律，别混）：
  情绪词 = 通用心理词汇 → 语义弥散，混仓会污染场景合法集 → 独立通道
  角色名 = 专有名词     → 语义收敛，本身就该在倒排索引里 → 挂主索引

收词范围：崩三主力先收，滚动扩充。零 LLM —— 本文件即词表单一事实源。
"""
from __future__ import annotations

# ── 角色词典：规范名 → 别名集合（含正式名/昵称/别名/英文）─────────
# 键 = 规范名（canonical）；值 = 该角色的所有写法（含规范名自身可不列）。
# 匹配时全部映射回规范名。
ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    # ── 崩三主角组 ──
    "琪亚娜": ("琪亚娜", "琪亚娜·卡斯兰娜", "Kiana", "kiana", "琪", "草履虫",
               "空之律者", "终焉之律者", "白色恶魔"),
    "芽衣": ("芽衣", "雷电芽衣", "Mei", "mei", "雷之律者", "始源之律者",
             "雷电女王", "北辰一刀流"),
    "布洛妮娅": ("布洛妮娅", "布洛尼亚", "布洛妮娅·扎伊切克", "Bronya", "bronya",
                 "板鸭", "理之律者", "重装小兔"),
    "符华": ("符华", "Fu Hua", "fu_hua", "班长", "赤鸢", "识之律者", "华"),
    "德丽莎": ("德丽莎", "德丽莎·阿波卡利斯", "Theresa", "学园长", "大姨妈"),
    "希儿": ("希儿", "希儿·芙乐艾", "Seele", "seele", "黑希", "死生之律者"),
    "姬子": ("姬子", "无量塔姬子", "Himeko", "姬子老师"),
    "八重樱": ("八重樱", "樱", "Yae Sakura", "嘤嘤嘤"),
    "卡莲": ("卡莲", "卡莲·卡斯兰娜", "Kallen"),
    "丽塔": ("丽塔", "丽塔·洛丝薇瑟", "Rita", "失落迷迭"),
    "幽兰黛尔": ("幽兰黛尔", "比安卡", "Durandal", "呆鹅"),
    "爱莉希雅": ("爱莉希雅", "Elysia", "爱莉", "粉色妖精"),
    "梅比乌斯": ("梅比乌斯", "Mobius"),
    "凯文": ("凯文", "凯文·卡斯兰娜", "Kevin"),
    "渡鸦": ("渡鸦", "Raven"),
    "阿波尼亚": ("阿波尼亚", "Aponia"),
    "伊甸": ("伊甸", "Eden", "伊甸之星"),
    "千劫": ("千劫", "Kalpas"),
    "苏": ("苏", "Su"),
    "格蕾修": ("格蕾修", "Griseo"),
    "帕朵菲莉丝": ("帕朵菲莉丝", "帕朵", "Pardofelis"),
    "科斯魔": ("科斯魔", "Kosma"),
    "维尔薇": ("维尔薇", "Vill-V"),
    "奥托": ("奥托", "奥托·阿波卡利斯", "Otto"),
    "可可利亚": ("可可利亚", "Cocolia"),
    "瓦尔特": ("瓦尔特", "瓦尔特·杨", "Welt"),
    "雷电龙马": ("雷电龙马", "龙马"),
}

# ── 长词优先的扁平索引（扫描用；单字别名最低优先）────────────
_ALL_PAIRS: list[tuple[str, str]] = []
for _canon, _aliases in ROLE_ALIASES.items():
    for _a in _aliases:
        _ALL_PAIRS.append((_a, _canon))
# 长词优先 → 防短别名吃掉长名（与 l3_store.decompose_query 同纪律）
_ALL_PAIRS_SORTED: tuple[tuple[str, str], ...] = tuple(
    sorted(_ALL_PAIRS, key=lambda p: len(p[0]), reverse=True))

# 已收录别名集合（边界判定用：能扩出更长别名 → 当前是片段）
_ALIAS_SET: frozenset[str] = frozenset(a for a, _ in _ALL_PAIRS)

# 规范名集合（打标/渲染用）
CANONICAL_ROLES: frozenset[str] = frozenset(ROLE_ALIASES.keys())

# 单字别名（如「琪」「樱」「苏」「华」）—— 高危：易被普通文本误命中。
# 纪律：单字别名**只在没有更长别名命中时才采用**，且要求整词边界。
_SHORT_ALIASES: frozenset[str] = frozenset(
    a for a, _ in _ALL_PAIRS if len(a) <= 1)


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


# 允许紧邻的虚词/标点（左或右）—— 名字后面接「和/的/在/吗」等是常态，
# 不得因粒子而拒绝匹配。
# （工单实测：琪亚娜·卡斯兰娜和芽衣 曾因「和」被误拒。）
_NEUTRAL_NEIGHBORS: frozenset[str] = frozenset(
    "和与跟及同的在吗呢吧啊呀么问说聊提是的了过就都也还很太多个位"
    "，。！？、；：·…—～’‘“”（）《》【】 ")


def _boundary_ok(text: str, key: str, i: int) -> bool:
    """词边界判定（独立实现，避免与 l3_store 循环依赖）。

    核心判据：**拒绝的是「作为一个更长词的片段」的命中，不是「名字后面接了
    虚词」的命中**。

    - 扩字检验：若 left+key（或 key+right）仍落在已收录别名集合里，说明有更长
      的合法写法，当前命中是片段 → 拒。
    - 词性护栏：邻接非中性 CJK 时保守拒绝，防「苏醒」的苏 /「才华」的华
      被单字别名吃掉。
    - 虚词/标点邻居（和、的、吗、，…）**不构成拒绝理由**。
    """
    n = len(key)
    left = text[i - 1] if i > 0 else ""
    right = text[i + n] if i + n < len(text) else ""

    # 扩字检验：能拼出更长已收录别名 → 当前是片段
    if left and (left + key) in _ALIAS_SET:
        return False
    if right and (key + right) in _ALIAS_SET:
        return False

    # 词性护栏：**仅对单字别名**生效（高危）。
    # 多字名字（琪亚娜/布洛妮娅）接任何 CJK 都是正常句子，不得因邻居被拒。
    # 工单实测：`，琪亚娜徒手…` 曾因后邻「徒」非中性字被误拒。
    if len(key) <= 1:
        if _is_cjk(key[0]) and left and _is_cjk(left) and left not in _NEUTRAL_NEIGHBORS:
            return False
        if _is_cjk(key[-1]) and right and _is_cjk(right) and right not in _NEUTRAL_NEIGHBORS:
            return False

    # ASCII 别名：左右不得接字母/数字/下划线
    if not _is_cjk(key[0]):
        if left and (left.isalnum() or left == "_"):
            return False
    if not _is_cjk(key[-1]):
        if right and (right.isalnum() or right == "_"):
            return False
    return True


def decompose_roles(text: str) -> list[str]:
    """把一段文本本地分解为命中的角色规范名（零 LLM、毫秒级）。

    纪律：
    - **不猜**：没提到就是空列表，绝不兜底补角色。
    - 长词优先：先扫长别名，命中后记录该角色；短别名若与已命中角色相同则跳过。
    - 单字别名（琪/樱/苏/华）**仅在长别名未命中任何角色时**才启用，
      且必须整词边界 —— 防「苏」误吃「苏醒」这类。
    """
    found: list[str] = []
    spans: list[tuple[int, int]] = []   # 已占用的字符区间（防重叠误吃）

    def _overlaps(i: int, n: int) -> bool:
        for s, e in spans:
            if not (i + n <= s or i >= e):
                return True
        return False

    # 第一轮：多字别名（长度 >= 2）
    for alias, canon in _ALL_PAIRS_SORTED:
        if len(alias) <= 1:
            continue
        start = 0
        while True:
            i = text.find(alias, start)
            if i < 0:
                break
            if _boundary_ok(text, alias, i) and not _overlaps(i, len(alias)):
                if canon not in found:
                    found.append(canon)
                spans.append((i, i + len(alias)))
                break
            start = i + 1

    # 第二轮：单字别名（仅当该角色尚未命中时）
    for alias, canon in _ALL_PAIRS_SORTED:
        if len(alias) != 1:
            continue
        if canon in found:
            continue
        start = 0
        while True:
            i = text.find(alias, start)
            if i < 0:
                break
            if _boundary_ok(text, alias, i) and not _overlaps(i, 1):
                found.append(canon)
                spans.append((i, i + 1))
                break
            start = i + 1

    return found


def canonical_of(word: str) -> str | None:
    """别名 → 规范名。未收录返回 None（**不猜**）。"""
    for canon, aliases in ROLE_ALIASES.items():
        if word in aliases:
            return canon
    return None


def tag_characters(text: str) -> list[str]:
    """给一条内容打角色标（规范名列表）。零 LLM；零命中 = 空列表。

    叫法与 decompose_roles 分开只是语义区分：打标=写入侧，分解=检索侧，
    两者共用同一份词典（写读对称纪律）。
    """
    return decompose_roles(text)
