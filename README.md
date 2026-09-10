# Roleplay-Eval v0.1

角色扮演 AI 评测框架(参考实现:芽衣/崩坏3)。当前阶段:**v0.1 最小闭环**。

> 原则:「GitHub 上的 v0.1 > 脑子里的 v3.0」

## 当前范围(v0.1 · 锁死)

- **评测维度**:角色一致性(Role Consistency),单维度
- **被测 Agent**:三 Agent 系统的**简化形态**(见下方形态说明)
- **Judge**:LLM-as-Judge,独立 prompt,沿用锚点「10分必须完美,明显瑕疵最多6分」
- **链路**:`cases.jsonl → 被测Agent → Judge → report.md`
- **验收线**:20 条 case 跑通 + report.md 能出 = 停
- **明确排除**:记忆 / 关系 / 情绪衰减(9月的事)

## 三 Agent 素材 · 三种跑法(关键设计决策)

同一套三 Agent System Prompt,按阶段/用途分三种跑法,**不混用**:

| 形态 | 用途 | 被测内容 | 阶段 |
|---|---|---|---|
| **C(评测用)** | v0.1 评测闭环 | Agent2 角色化直接吃 case → Judge | 现在(先跑通) |
| **A(对比实验用)** | 单 vs 三 Agent 实验 | 完整三 Agent:路由→角色化→质检 | 框架稳定后 |
| **B(产品 demo 用)** | 芽衣作为产品 | 两段式:Agent2 角色化 → 质检 | 产品路线,与评测分开 |

> 注意:C 是为了评测速度快/准的**被测简化**,**不是**芽衣产品的真实形态。B 才是产品 demo 路线。三种素材同源,跑法不同。

## 目录结构

```
roleplay-eval/
├── cases/          # 评测 case(JSONL)
├── prompts/        # 三 Agent 被测系统 prompt(原样存档)
├── judge/          # Judge Agent prompt 与逻辑
├── utils/          # DeepSeek API 客户端等
├── kb/             # 知识库检索模块(攻略 V1 + 背景 V2,接口可扩展向量)
│   ├── retriever.py    # BaseRetriever 接口 + Guide/Lore 实现 + Vector Stub
│   ├── context.py      # 检索结果 → Agent2 上文注入(方案 A)
│   ├── parsers/        # V1 标签条目 / V2 叙事整块解析器
│   ├── sources/        # v1_guide.txt / v2_lore.txt(原始素材)
│   └── selfcheck.py    # 本地自检(不调 API)
├── docs/
│   └── knowledge_architecture.md   # 知识库检索架构 + 切向量演进(开源报告料)
├── output/         # 评测报告(report.md)
├── data/           # 中间产物(回复缓存等)
├── .env            # 密钥(已 gitignore,不进 git)
└── .gitignore
```

## 知识库检索层(v0.1 已接入,方案 A)

芽衣的「认知外挂层」已落地:`kb/`。
- **V1 攻略库**:标签精确查表(GuideRetriever)—— 工具知识,答攻略时的真实事实源。
- **V2 背景库**:主题/维度整块加载(LoreRetriever)—— 聊到相关人物/过往时带出记忆整块。
- **接口与实现分离**:消费方只依赖 `BaseRetriever.query()`;未来数据涨到千条 / 查询变模糊,
  可实现 `VectorRetriever` 热切换(占位契约已留),上层零改动。
- **为何 v0.1 不直接上向量**:见 `docs/knowledge_architecture.md`(规模/数据形态/成本三因)。
- **V2 两层设计**:人格常量焊死进角色卡,详细叙事外挂按需加载 —— 详细见架构文档。

本地验证检索(不调 API):
```bash
python kb/selfcheck.py
```

## 已知边界与验证状态(v0.1.0-demo 明示)

本节显式列出**我们知道但尚未在真实分布下验证**的边界。写在这里不是遗漏,是纪律:
把「已钉死的机制行为」与「真实数据下的触发面」分开交代,tag 发布时它不是裂缝,
而是我们清楚自己还没验证什么。

- **过度矫正防线的触发面(有意保留的验证边界)**
  防过度矫正的探针行为已在**离线环境**验证钉死:`reason` 机械防线只剥指令句、
  固定描述壳(`上一轮质检意见:`)零指令词泄漏、频次闸「连续修正≥3 且 pass 清零」、
  壳只进用户侧【附】格不进 system/人格块 —— 这些都有确定性桩断言(`session/quality_wiring_probe.py`,18 条)。
  但**真实对话分布下的触发面尚未观测** —— 真实模型多久真正触发一次修正壳、
  注入率的真实量级是多少,须等公网部署后的**首批真实数据**校验。
  这是**有意保留的验证边界,不是遗漏**:红线要求不得用模拟数据冒充真实触发面,
  故此处只声明边界,不填假账。部署后以 `qc_injection_rate()`(常态应 <0.2)
  与 `qc_marker_mid_rate()` 两个仪表首批真实读数校准。

- **标记中途出现(改四·常态观察项)**
  首行 `[闲聊]/[攻略]` 标记由引擎剥离(玩家侧永不见);但流式下正文**中部**再冒出的
  标记无法事后抹除。仪表已就位(`engine.marker_mid_rate()`),真实频率待冒烟期记档。
  详见 `session/_marker_mid_archive/CONCLUSION.md`。

## 状态

- [x] 目录骨架
- [x] Judge prompt(角色一致性锚点版)
- [x] DeepSeek API 客户端
- [x] 知识库检索层(kb):
  - [x] BaseRetriever 接口 + V1 Guide / V2 Lore 实现(plain 查表)
  - [x] VectorRetrieverStub 占位契约(未来切向量)
  - [x] 方案 A:检索结果注入 Agent2 上文(按 case 分类的隔离策略)
  - [x] kb/selfcheck.py 本地自检通过
  - [x] docs/knowledge_architecture.md(架构决策 + 演进判据)
- [ ] 三 Agent prompt 落盘
- [ ] 20 条 case
- [ ] 执行器(C 形态)
- [ ] 跑通 + report.md
