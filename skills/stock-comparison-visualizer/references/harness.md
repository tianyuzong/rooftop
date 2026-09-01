# 股票研究代理 Harness

## 定义与职责

Harness 是模型或领域规划器之外的执行系统。它掌管任务理解后的运行过程：持久化线程和运行、冻结业务上下文、限制可调用工具、记录进度与结果、处理失败、请求人工批准，并在跨请求或服务重启后继续工作。

当前规划器是可替换的确定性领域规划器，不调用生成式模型。是否使用模型与是否构成 Harness 是两个问题：规划器决定下一步做什么，Harness 决定允许做什么、状态如何保存、何时暂停以及如何恢复。

## 运行生命周期

1. `POST /api/harness/runs` 创建线程或复用 `thread_key`，验证输入并冻结配置版本、数据源健康和工具边界。
2. 规划器为选定工作流生成受限步骤；每步只能引用工具允许列表。
3. Harness 逐步执行，持久化事件、工具参数、尝试次数、结果摘要和检查点。
4. 只读与内部审计写入自动继续；高影响写入进入 `WAITING_APPROVAL`。
5. 成功后输出结构化事实、观点和推翻条件；失败后保留错误及最近成功检查点。
6. `FAILED` 或 `INTERRUPTED` 可显式恢复；服务启动时会把遗留的 `RUNNING` 标记为 `INTERRUPTED`，不会重复已成功的工具调用。

主要状态为 `QUEUED`、`RUNNING`、`WAITING_APPROVAL`、`FAILED`、`INTERRUPTED`、`COMPLETED` 和 `CANCELLED`。

## 工作流

### 量化选股与组合

日常页面不直接创建以下 Harness 工作流。它先调用 `POST /api/quant/mandates` 登记约束，再用 `POST /api/quant/decision` 获取匹配约束的最近 `ACTIVE` 策略。首次约束尚无 `ACTIVE` 时，有活动预测模型和参考模板则使用最近一个已完成逐日评测并通过门禁的活动模型训练日作为唯一日期边界，从缓存行情筛选候选并生成 `SNAPSHOT` 推荐；没有活动模型或参考模板但缓存日线不少于 420 个共同交易日时生成 `RULE_SNAPSHOT`。两者都不会刷新数据、采集舆情、创建量化运行、训练模型或启动回测。`SNAPSHOT` 的持仓与排序属于当前候选池推断，P10/P50/P90、回撤和净值来自引用的已发布策略模板，必须在页面上明确区分。`RULE_SNAPSHOT` 不生成持仓或组合收益，只对观察标的按通达信已有周期展示即时统计价格范围，并依据用户期限、风险偏好和纪律生成条件式买卖复核；真实持仓仅在成本止损、账户盈亏和数量计算时需要。新一轮计算期间、失败或被门禁拒绝时继续返回快照或上一版，只有盘后任务在同一事务中完成新 `ACTIVE` 发布后才切换。

以下请求只用于高级审计区的显式手动研究运行：

```json
{
  "workflow": "quant_portfolio",
  "input": {
    "name": "A股量化组合",
    "capital": 100000,
    "horizon_months": 12,
    "target_return_pct": 20,
    "max_drawdown_pct": 15,
    "stop_loss_pct": 8,
    "take_profit_pct": 20,
    "trailing_stop_pct": 8,
    "sectors": ["消费", "新能源"],
    "stocks": [],
    "max_candidates": 12,
    "max_positions": 2,
    "risk_profile": "balanced",
    "preference_weights": {
      "trend": 25,
      "fundamental": 30,
      "probability": 20,
      "liquidity": 10,
      "stability": 15
    },
    "strategy_style": "auto",
    "backtest_window_years": 3,
    "take_profit_mode": "trailing",
    "max_iterations": 10
  }
}
```

该工作流同步带时间戳的 A 股名称和通达信行业层级，按板块发现并按流动性和历史覆盖筛选候选，随后运行三档逐日预测因子回测。它输出当前目标组合、整手股数、现金、止盈止损、留出集净值以及收益 P10/P50/P90、目标概率和亏损概率。研究版本在风险和非退化门禁通过后可自动晋级，但 `order_execution` 始终为 `false`。

基本面进入正式信号：历史回测只读取 `NOTICE_DATE <= signal_date` 的最新财报和当日可得估值，输出报告期、公告日、成长/质量/现金流/安全/估值五维得分、覆盖率与淘汰原因。三档使用各自的基本面权重和门槛；页面由用户显式选择激进、中立或保守。

自动档位只在最大回撤与非退化门禁通过的策略中选取。若没有策略的 P50 达到目标，返回风控合格且 P50 距离目标最近的 `CLOSEST_FEASIBLE` 方案，并显示差额；若所有策略都违反风控门禁，则返回 `NO_RISK_FEASIBLE`、保持 100% 现金，最接近目标的策略仅作为研究候选。停盘日没有入场信号时，100% 现金本身是风险约束下的有效最优推荐，不能描述成尚未计算。日常页面默认只显示约束输入与最近发布或快照推断的最终结论，完整 Harness 事件、工具和审批记录位于折叠的高级审计区。

### 股票研判

```http
POST /api/harness/runs
Content-Type: application/json

{
  "workflow": "stock_analysis",
  "input": {
    "stocks": ["600519", "000858"],
    "profile": "balanced",
    "research_query": "高端白酒需求"
  },
  "intent": "比较两只股票并检查本地研报证据"
}
```

依次解析股票、运行评分与历史回放、检索本地研究证据。结果将事实、模型观点与推翻条件分开，并明确 `order_execution=false`。

### 质量巡检

```json
{
  "workflow": "quality_audit",
  "input": {"stock_limit": 20},
  "intent": "检查数据与配置质量"
}
```

运行确定性主动探针、当前版本回归集，并列出可供人工审阅的候选。它可以记录坏案例和评测，但不会自动激活候选。

### 策略自进化

```json
{
  "workflow": "strategy_evolution",
  "input": {
    "name": "白酒与新能源组合",
    "capital": 100000,
    "horizon_months": 12,
    "target_return_pct": 20,
    "max_drawdown_pct": 20,
    "stocks": ["600519", "000858", "300750"],
    "sectors": ["消费", "新能源"],
    "max_positions": 2,
    "max_iterations": 10,
    "take_profit_mode": "trailing"
  }
}
```

该工作流先冻结投资授权书，再为激进、平衡、保守三档生成参数候选。回测以 126 个交易日为一期：第一期冻结使用基线参数；以后每期开始前只根据已经结束的半年结果重选参数，本期结果只能参与下一期更新。最后一期在选参完成后只对入选参数和基线运行一次独立留出检查。它写入研究实验但不会激活版本，结果始终包含 `order_execution=false`。

### 策略激活

```json
{"workflow":"strategy_activation","input":{"experiment_key":"experiment_..."}}
```

先复核实验是否成功且三档策略均通过半年递推样本外验证、最终留出风险门禁和基线非退化门禁，再进入 `WAITING_APPROVAL`。目标收益未命中不会单独禁止激活，因为它是软目标；风险或非退化门禁未通过一定禁止激活。

### 持续自进化

```json
{
  "workflow": "continuous_learning",
  "input": {
    "phase": "POST_CLOSE",
    "stock_limit": 20,
    "max_social_symbols": 3,
    "max_drawdown": 0.15,
    "train_deep_model": true,
    "deep_epochs": 32,
    "evolve_intraday": true,
    "intraday_interval": 5,
    "evolve_source_code": true,
    "auto_promote": true,
    "auto_promote_code": true
  }
}
```

盘前阶段固化待验证预测；每个开盘日盘后阶段刷新 A 股日线与分钟数据、多源新闻和公开社交元数据，结算已有预测，滚动重训并评测在线模型与 Qwen3-0.6B 数值时序适配器，进化 5 分钟策略，并刷新活动量化组合。候选模型只有通过样本量、非退化、改善和最大回撤门禁才晋级；否则保留上一活动模型及其 `training_end`，停盘日快照也继续锁定这个日期。Qwen3 主干使用经过 SHA-256 校验的本地官方权重并保持冻结；20 日特征序列映射为数值 token，只更新轻量投影层及涨跌/收益双头。训练、验证和最终留出集严格按日期分离，概率与收益校准只使用验证集，每个候选只运行一次留出门禁；已经被人工修正规则使用过的留出窗口只能作为复核，强晋级必须等待新滚动窗口或前向影子样本。受控源码候选只允许改写 `runtime/app/evolvable/intraday_recipes.py`，在隔离代码副本和 SQLite 快照上运行编译、全量测试、验证集和独立留出集门禁。只有全部门禁通过才原子发布并备份旧版本；否则拒绝，生产文件不变。评测沙箱随后自动清理。

盘后在在线模型和组合刷新之前增加基本面阶段，保存新财报与当日估值快照，并用同一收盘日期重新评测三档策略。Skill 文本是稳定控制面，不由每日数据重写；运行制品才进行版本切换。

交易日使用官方开市日历缓存判定，缓存覆盖范围内未列出的工作日视为休市。盘后轮次只有在 `data_asof` 覆盖轮次日期时才算数据完整；若行情尚未到齐，结果记录 `market_date_complete=false`，服务调度器按受控间隔复用同一轮次重试。Windows 任务盘前 08:45 运行，盘后 18:00 运行，服务启动时仍会执行幂等补跑。

### 候选晋级与版本回滚

```json
{"workflow":"candidate_activation","input":{"candidate_id":3}}
```

```json
{"workflow":"version_rollback","input":{"version_key":"baseline-v1"}}
```

策略激活、候选晋级和版本回滚在执行最终写入前进入 `WAITING_APPROVAL`。批准接口：

```http
POST /api/harness/approvals/{approval_key}/resolve
Content-Type: application/json

{"approved":true,"resolved_by":"姓名","confirmed":true}
```

拒绝时传 `approved:false`；运行会停止且不会执行高影响工具。`resolved_by` 与 `confirmed:true` 均为必填门禁。

## 查询、恢复与取消

- `GET /api/harness`：运行摘要、工作流、工具定义、最近运行和待审批项，同时保留反馈子系统数据。
- `GET /api/harness/runs/{run_key}`：完整运行；可加 `?after_event_id=12` 增量读取事件。
- `POST /api/harness/runs/{run_key}/resume`：恢复 `FAILED` 或 `INTERRUPTED` 运行。
- `POST /api/harness/runs/{run_key}/cancel`：取消非终态运行。
- 复用上下文时，在新建运行请求中传已有 `thread_key`。

## 工具和风险边界

允许工具只有：`resolve_stocks`、`compare_stocks`、`search_research`、`construct_quant_portfolio`、`continuous_learning_cycle`、`autonomous_audit`、`baseline_regression`、`list_activation_candidates`、`evaluate_candidate`、`activate_candidate`、`rollback_version`、`validate_investment_mandate`、`evolve_portfolio_strategies`、`review_strategy_experiment` 和 `activate_strategy_experiment`。

- `READ`：只读解析与检索，可自动执行并有限重试。
- `INTERNAL_WRITE`：只写缓存、审计记录、坏案例或评测结果，可自动执行但全程留痕。
- `CONSEQUENTIAL_WRITE`：切换当前配置或回滚版本，必须人工批准。

允许列表不包含任意 Shell、JavaScript/Skill 自修改、API Key 管理、券商连接和订单执行。`continuous_learning_cycle` 内部只允许对单一白名单 Python 策略配方生成候选，AST 禁止导入、调用、属性访问、文件、网络、进程、密钥与券商相关内容；候选在隔离副本中通过全量测试和样本外风险门禁后才可发布。每日自动更新作用于行情、舆情、模型权重、策略参数、组合研究版本和该受控策略配方；股票研究结果不承诺收益。

## 反馈与配置演化子系统

用户反馈和运行异常会进入可重复回归的坏案例。系统可生成搜索别名、股票别名和完整六维评分权重候选；数据链路、页面交互和代码缺陷只标记为工程候选。

标准流程是：记录坏案例 -> 补齐可验证期望 -> 生成候选 -> 当前/候选对照评测 -> Harness 人工审批 -> 生成不可变版本 -> 必要时人工审批回滚。候选必须修复目标、至少改善一例并且新增退化为零。

相关接口：

- `POST /api/harness/bad-cases`
- `POST /api/harness/candidates/generate`
- `POST /api/harness/evaluate`
- `POST /api/harness/autonomous/run`，固定传 `{"auto_apply":false,"stock_limit":20}`

旧的直接批准和回滚接口仍为兼容接口，但同样要求批准人和 `confirmed:true`。新交互优先通过代理运行及 `/api/harness/approvals/{approval_key}/resolve` 完成，以保留完整上下文、事件和工具审计链。
