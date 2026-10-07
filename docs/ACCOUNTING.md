# 芽衣项目 · 用量账目

> 口径纪律：**能自动出数的才叫账**。手工倒推仅 2026-10-07 一次，此后销项。
> 记账实现：`utils/api.py` → `_record_usage()` / `usage_snapshot()`（commit `a7122a4`）
> 落盘口：环境变量 `DEEPSEEK_USAGE_JSONL`（逐行 JSON）
> 对账器：`dev/usage_check.py`（单轮校验，不重跑）

## 计价基准（DeepSeek 官方账单导出实测，2026-10-07）

| 字段 | 单价 (CNY/token) |
|---|---|
| input_cache_hit_tokens | 0.00000002 |
| input_cache_miss_tokens | 0.000001 |
| output_tokens | 0.000004 |

**缓存命中 vs 未命中 = 价差 50 倍**（原估「近十倍」，实测更陡）。
多轮对话 prompt 越滚越长 → cache 命中率直接决定单位成本曲线。

---

## 场次 1 · 2026-10-07 Day 7 真实对话验收（15 轮）

**口径标注：日粒度账单，含 2026-10-07 当日全部调用，非仅该场。**
（官方导出为天粒度，194 次请求混入当日其他实验/探针，无法拆分场次。
  该场 15 轮验收为其一部分。）

| 项 | 值 |
|---|---|
| prompt_tokens | 12,846,346 （hit 12,556,160 + miss 290,186） |
| completion_tokens | 66,232 |
| prompt_cache_hit_tokens | 12,556,160 |
| prompt_cache_miss_tokens | 290,186 |
| request_count | 194 |
| 花费 | ¥0.8062372 CNY |
| cache 命中率 | 97.7% |
| 断点 | 0 |
| 耗时 | 28.2s（15 轮，~1.9s/轮） |

**验收结论**：零断点 ✅ —— 这是该场账目的**唯一验收语义**，已达成。
**token 数**：受日粒度限制，作趋势参考，不当精确场次账。

**销项备注**：精确场次账待 API Key 隔离后自动可得（回头单独测）。

---

## 场次 2 · 2026-10-07 (a) 落地校验（单轮）

输入：「今天好累，不想说话」；2 次调用（主回复 + 书记员）

| 调用 | prompt | completion | cache_hit | cache_miss | 耗时 |
|---|---|---|---|---|---|
| 1 主回复 | 1480 | 26 | 1280 | 200 | 0.917s |
| 2 书记员 | 1819 | 6 | 1536 | 283 | 0.697s |
| **合计** | **3299** | **32** | **2816** | **483** | **1.614s** |

**cache 命中率 85%** —— 同轮内 system prompt 复用即吃到缓存红利。
首份「cache 命中率决定单位成本」的活证据。

---

## 采集设施（供后续复用）

- `_record_usage(body, kind=, model=, seconds=)` —— 追加事件，失败静默
- `kind` 取值：`chat` / `stream` / `stream-partial`
- `usage_snapshot()` —— 进程内聚合（calls / 四字段 / seconds / by_kind）
- `usage_reset()` —— 清空进程内账（测试用）
- `dev/usage_check.py` —— 单轮校验器

## SOP

1. 跑场次：`$env:DEEPSEEK_USAGE_JSONL="<path>"` 后正常跑
2. 读账：`usage_snapshot()` 或读 jsonl
3. 对账：与 DeepSeek 控制台增量比对（**按 API Key 隔离后可精确到场次**）
4. 计价：按上表三单价折算

## 待办（回头单独测）

- [ ] 建验收专用 API Key → 场次账目自动精确，手工倒推彻底销项
