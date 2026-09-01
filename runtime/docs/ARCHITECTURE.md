# 系统架构

## 数据面

`data_lake/` 是唯一运行数据根目录：

```text
data_lake/
  raw/market|documents|news/   不可变原始响应
  normalized/                  规范化 CSV/Parquet
  db/                          SQLite 主库
  documents/                   财报、公告、研报及元数据
  cache/                       可重建缓存
  models/                      本地 embedding 模型权重
  research/factors             因子研究输出
  research/backtests           单策略回测输出
  research/strategy_evolution  三档组合策略实验快照
  dead_letter/                 抓取或字段失败证据
  backups/ logs/ outbox/       备份、日志、邮件发件箱
```

SQLite 负责资产、点时行情、持仓、证据、研报、信号、策略、因子、回测和数据血缘。它也持久化 Harness 的线程、运行、事件、工具调用、人工审批、坏案例、评测与配置版本，以及投资授权书、策略演化候选、组合模拟和已批准策略版本。系统不包含事件图、Neo4j 镜像、通用 Agent Chat 或模型 API Key JSON 路由。

## 数据进入规则

1. 所有外部查询必须先保存原始响应，再规范化入库；禁止生产代码把一次性网络响应直接交给 UI 后丢弃。
2. 校验必需字段、交易日、OHLC 关系、重复键和异常跳变。
3. 写入时附 `source_id`、`captured_at`、`raw_path` 和演示标志。
4. 抓取失败只记录 `ingestion_runs`/`dead_letter`，不破坏上次可信数据。
5. 跨源存在差异时并存原始证据，规范化主值由版本化质量规则决定。
6. 行情进入 `quote_snapshots / minute_bars / market_daily_bars / prices`。
7. 新闻、公告、财报、研报和社交内容进入 `source_documents`，历次正文进入不可覆盖的 `source_document_versions`。
8. 回测和因子结果进入 `backtest_runs / factor_runs`，并在 `data_lake/research/` 保存可审计输出。

行情链路为 `通达信本地文件/公开协议 -> SQLite 最后可信快照`。后台线程负责行情刷新，Web API 只查询本地库，因此上游超时不会拖死页面。`quote_snapshots` 保存实时快照，`minute_bars` 保存一分钟数据及 `bar_kind`，`market_daily_bars` 保存可继续聚合为周/月/季/年的前复权日线。财报和估值通过独立基本面采集器落库，不能冒充通达信行情。

`app.persistence` 是外部文档的强制持久化入口。新增抓取器若没有调用该入口（或行情专用 `_write_*` 入口）就不算完成接入。前端查询、图表生成、搜索和策略研究只消费本地数据库。

## 研究面

- 客观模型：波动、回撤、动量、流动性、相关性、压力情景、止盈止损线。
- 图表指标：同一 OHLCV 窗口计算 MA5、KDJ(9,3,3)、MACD(12,26,9)；筹码分布是成交量在价格区间上的估算，并明确区别于券商逐笔持仓成本数据。
- 主观研究：事实、观点、待验证假设分类；保留来源、观测时间和反证条件。
- 数据源：通达信公开/本地行情与 AKShare 是主要参考方向，免费公开源优先，所有抓取结果落本地审计。
- 策略回测：AKQuant 只用于离线回测、因子表达式、walk-forward、参数检验和风险报告。
- 前端：参考 ValueCell 的本地研究工作台风格，但不复制交易连接能力。
- 搜索：严格匹配直接检索可审计文档；语义匹配由本地 Qwen3 Embedding 生成 512 维向量并保存在 SQLite `semantic_documents`，不调用付费向量数据库。

## 代理 Harness

`app.agent_harness` 是受限股票研究代理的执行控制面。规划器负责把六类领域意图转换为固定工具计划；Harness 负责持久化状态、冻结上下文、执行工具允许列表、流式记录事件、检查点恢复和人工审批。两者边界分离，规划器可替换，运行安全边界不随规划器变化。

- `harness_threads` 保存可跨运行复用的任务上下文。
- `harness_runs` 保存工作流、输入、上下文快照、计划、当前步骤、结果和错误。
- `harness_run_events` 与 `harness_tool_calls` 形成增量进度和工具审计链。
- `harness_approvals` 把候选晋级和版本回滚暂停为显式人工决策。
- 旧的 `harness_bad_cases`、候选、评测和版本表继续承担反馈与配置演化，不再代表 Harness 的全部能力。
- `investment_mandates` 冻结用户目标与硬风险约束；`strategy_evolution_candidates` 保存参数白名单内的每轮候选。
- `strategy_simulations` 分开保存滚动验证和最终留出结果；`strategy_evolution_versions` 只接收三档风险门禁均通过且人工批准的实验。

`app.strategy_evolution` 使用前一交易日信号和下一交易日开盘成交，支持多标的持仓、换股、止损、固定或浮动止盈，以及组合回撤守卫。成交模型显式计入佣金最低收费、卖出印花税、滑点、100 股整手、成交量参与上限、停牌、涨跌停近似和 T+1。价格限制是授权书中的可审计假设，不冒充对不同板块和特殊证券规则的完整识别。

服务启动时，只有超过默认四小时租约的陈旧 `RUNNING`/`QUEUED` 运行会改为 `INTERRUPTED`；另一健康服务仍在执行的新鲜任务保持原状态。人工恢复后从最后成功工具继续。允许工具不包含任意 Shell、券商连接或订单执行；源码进化只允许受 AST 白名单和隔离测试门禁约束的策略配方。当前 `bounded-domain-planner` 是确定性实现且 `generative_model=false`，后续可替换为模型规划器而不改变执行协议。

## 安全边界

- 数据库无 `orders`、`broker_accounts`、通用 `chat_sessions`、通用 `chat_messages`、`event_graphs`、`graph_nodes`、`graph_edges`、`graph_snapshots` 等表。`harness_runs` 是受限工作流执行记录，不是任意聊天或下单任务。
- 桌面券商适配器只暴露行情读取；代码审查禁止导入下单方法。
- 绿线是“进入研究区”，不是自动买入；红线是“必须人工复核卖出”，不是自动卖出。
- 邮件先入本地 outbox，默认 dry-run，去重键避免重复发送。
- 自定义研究模型使用声明式 JSON、不可变版本和人工激活；运行时不执行用户代码或 Excel 宏。
- 正式组合版本生成 `BUY / SELL / REBALANCE` 人工复核信号，快照和未过门禁结果只生成 `WATCH`。
- 所有结论必须标注“事实 / 观点 / 待验证假设”，并保留来源与反证条件。
