# coding: utf-8
"""校准辅助:对一次真蒸馏产物跑 5 项检查的『可程序化部分』(字符数/污染/兑现翻篇占位)。"""
import io, json, re, os

def audit(path):
    d = json.load(io.open(path, encoding="utf-8"))
    s = d["L2_our_story"]; w = d["L2_who"]; total = len(s) + len(w)
    print("== 程序化度量 ==")
    print(f"story_chars={len(s)}  who_chars={len(w)}  total={total}")
    print(f"under_500char_headroom(元口径=500 字符,与规格同单位): total<=500 ? {total<=500} | 超预算则引擎走定向压缩/截断")
    fence = (s + w).count("```")
    print(f"markdown代码围栏数量={fence}  含 `---WHO---` 分隔残留={('---WHO---' in s+w)}")
    for t in ["[闲聊]", "[攻略]", "```md", "json"]:
        if t in (s + w):
            print("  POLLUTE:", repr(t))
    print("== 兑现翻篇探测(④双写) ==")
    print("WHO 仍保留'雪山日出'(已兑现却当进行时?):", "雪山日出" in w)
    ev = d["L1_events"]
    settled = [e for e in ev if e["type"] == "承诺兑现"]
    print(f"L1 兑现事件数={len(settled)}:", [e['content'][:22] for e in settled])
    print("== 分词/句读存在性(供人眼复核推断评价) ==")
    for kw in ["重视", "坚毅", "勇敢", "善良", "可靠", "温柔的人", "内心", "应该", "不该"]:
        if kw in (s + w):
            print("  INFER_HINT:", kw, "→", (s + w).find(kw))

if __name__ == "__main__":
    import sys
    audit(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CALIB_JSON", ""))
