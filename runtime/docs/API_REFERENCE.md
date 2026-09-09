# HTTP API 参考

默认基址为 `http://127.0.0.1:<port>`，API 版本由 `GET /api/health` 返回，当前为 `2`。请求和响应均使用 UTF-8 JSON；POST 请求体最大 5 MB。

## 1. 鉴权、限流与错误

- 本机回环服务默认不要求令牌。
- 配置 `ARGUS_REMOTE_TOKEN_FILE` 后，所有 `/api/*` 请求必须包含 `X-Argus-Token`。
- 所有 API 按客户端 IP 限流，默认每分钟 180 次。
- POST 请求校验 Host 和 Origin，并写入 `api_audit_log`。
- 常见状态码：`200` 成功、`201` 创建、`202` 已接受后台任务、`400` 输入错误、`401/403` 令牌错误、`421` Host 不允许、`429` 限流、`503` 依赖或数据暂不可用。
- 失败响应通常为 `{"error":"..."}`。

远程请求示例：

```powershell
$headers = @{ "X-Argus-Token" = (Get-Content -Raw C:\RooftopSecrets\remote.token).Trim() }
Invoke-RestMethod https://argus.example.com/api/health -Headers $headers
```

## 2. GET 接口

服务维护接口 `POST /api/service/shutdown` 仅接受回环客户端，正文必须为 `{"confirmed":true}`，并遵守现有令牌鉴权。它用于启动器受控升级，不接受远程关闭请求。

### 服务、首页和数据

| 路径 | 参数 | 返回与副作用 |
|---|---|---|
| `/api/health` | 无 | 服务、Python、插件版本、代码指纹、数据湖、行情源、数据计数、邮件和桌面适配器状态；只读 |
| `/api/dashboard` | 无 | 市场总览、真实持仓状态、研究摘要和最近证据；只读 |
| `/api/market/quotes` | `symbols`：逗号分隔 | SQLite 中每个标的最后可信报价、来源、观察时间；不在线抓取 |
| `/api/market/minutes/{symbol}` | `limit` 默认 300，最大 2000 | SQLite 中最近 1 分钟 K 线；只读 |
| `/api/assets/{symbol}/chart` | `period` 默认 `1d` | 图表、行情覆盖和指标；需要时异步触发当前标的刷新 |

图表周期支持：`time,5d,1m,5m,15m,30m,60m,120m,1d,1w,1mo,1q,1y`。指数建议使用 `000001.SH`，避免与六位股票代码混淆。

### 股票研究

| 路径 | 参数 | 返回与副作用 |
|---|---|---|
| `/api/stock-comparison` | `stocks`、`profile`、`refresh=0|1` | 2-8 只股票的比较、风险档位、历史回测和图表；登记比较关注池 |
| `/api/strategy-lab` | 无 | 已注册策略、因子、最近回测和组合因子面板 |
| `/api/quant/methodology` | 无 | 当前推荐公式、风险档位、在线模型特征、回测窗口和术语 |
| `/api/signals` | `mandate_key` 可选、`limit` 默认 100 | 研究信号、版本、人工状态和失效条件 |

`profile` 为 `aggressive`、`balanced` 或 `conservative`。

### 模型、通知和 Harness

| 路径 | 参数 | 返回与副作用 |
|---|---|---|
| `/api/models` | 无 | 模型定义、激活作用域和默认模型；只读 |
| `/api/notifications` | 无 | 信号摘要、SMTP 状态、订阅和最近 outbox |
| `/api/notification-subscriptions` | 无 | 全部邮件订阅；只读 |
| `/api/harness` | 无 | Harness、组合演化、持续学习、深度模型、分钟策略、舆情、源码演化和板块缓存状态 |
| `/api/harness/runs/{run_key}` | `after_event_id` 默认 0 | 单次运行、增量事件、工具调用、审批和结果 |

### 搜索与资料

| 路径 | 参数 | 返回与副作用 |
|---|---|---|
| `/api/search/status` | 无 | 语义模型、向量索引和每日资料同步状态 |
| `/api/search` | `q`、`mode=exact|semantic`、`page` 可选 | 本地资料结果；语义模型不可用时自动降级为 exact |
| `/api/intelligence-sources` | 无 | Bilibili、X、SEC 等配置状态；从不返回凭证 |
| `/api/reports/library` | 无 | 本地研报、公告、财报和宏观资料库 |
| `/api/reports/sync/status` | 无 | 批量资料同步任务状态和进度 |

六位股票代码查询会自动跨页面进行严格匹配，避免被 `page` 过滤掉。

`POST /api/search/reindex` 无需请求字段，增量更新本地语义向量索引并返回本次索引数量和模型状态。模型不可用时返回 `503`，不会删除原有严格匹配资料。

## 3. POST 接口

### 模型注册表

| 路径 | 请求字段 | 返回/副作用 |
|---|---|---|
| `/api/models` | `model_key,version,name,model_kind,profile?,specification,created_by` | 创建不可变声明式模型版本，`201` |
| `/api/models/{id}/activate` | `scope_type, scope_value, profile?, approved_by, confirmed` | 人工批准后激活；同作用域旧版本失效 |

`model_kind`：`financial / valuation / factor / strategy / risk`。`scope_type`：`DEFAULT / INDUSTRY / SYMBOL`。模型 JSON 禁止 Python、JavaScript、Shell、导入、执行、券商和 API key 字段。

最小财务模型：

```json
{
  "model_key": "financial.custom.balanced-quality",
  "version": "1.0.0",
  "name": "balanced-quality-v1",
  "model_kind": "financial",
  "profile": "balanced",
  "created_by": "researcher",
  "specification": {
    "schema_version": 1,
    "dimensions": {
      "quality": [
        {"field": "roe_pct", "low": 3, "high": 25}
      ]
    },
    "missing_data_policy": "preserve_missing_and_reduce_coverage",
    "research_only": true,
    "order_execution": false
  }
}
```

### 信号与邮件

| 路径 | 请求字段 | 返回/副作用 |
|---|---|---|
| `/api/signals/{id}/acknowledge` | 空对象 | 状态改为 `ACKNOWLEDGED` |
| `/api/signals/{id}/dismiss` | 空对象 | 状态改为 `DISMISSED` |
| `/api/notification-subscriptions` | `id?,name,target,digest_kinds?,watch_stocks?,send_time?,event_kinds,minimum_confidence,mandate_id?` | 创建或更新显式 EMAIL 订阅；更新会取消旧内容的未发送消息 |
| `/api/notification-subscriptions/{id}/enable` | 空对象 | 启用订阅 |
| `/api/notification-subscriptions/{id}/disable` | 空对象 | 停用订阅 |
| `/api/notifications/test` | `subscription_id?` | 向 outbox 加入测试邮件 |
| `/api/notifications/send` | `limit` 默认 50，最大 200 | 处理待发送 outbox；仍受 SMTP 和 send 开关控制 |

`event_kinds` 支持 `BUY`、`SELL`、`REBALANCE`、`HOLD` 和 `WATCH`，仅订阅日报时可为空。`minimum_confidence` 是 0-1；`mandate_id` 是内部数值 ID，不是 `mandate_key`。日报选项、日期与投递状态见下方“投资日报与邮件投递”。

### 真实持仓导入

| 路径 | 请求字段 | 返回/副作用 |
|---|---|---|
| `/api/portfolio/imports/preview` | `mode`、账户名称、手工行或 CSV 映射 | 解析并报价校验，但不写正式持仓，`201` |
| `/api/portfolio/imports/confirm` | 预览标识、`confirmed` 等 | 确认后写入本地组合和持仓 |
| `/api/portfolio/clear` | 组合标识、确认字段 | 清除本地真实持仓快照 |

持仓导入不创建券商连接。CSV 和手工输入仅保存在本地数据湖。

### 资产与约束组合

| 路径 | 请求字段 | 返回/副作用 |
|---|---|---|
| `/api/assets/resolve` | `query` | 解析股票名/代码并返回图表和档案 |
| `/api/quant/decision` | `input` 或直接传约束 | 读取匹配的已发布结果；不登记、不训练、不回测 |
| `/api/quant/mandates` | `input` 或直接传约束 | 登记/复用约束，返回 ACTIVE/SNAPSHOT/RULE_SNAPSHOT，`201` |
| `/api/quant/cache` | `sectors,force?` | 触发板块候选缓存，`202` |

约束字段：

| 字段 | 默认 | 约束 |
|---|---:|---|
| `name` | `A股量化组合` | 最长 80 字符 |
| `capital` | 100000 | 1000-1,000,000,000 |
| `horizon_months` | 12 | 1-120 |
| `target_return_pct` | 20 | 0-300 |
| `max_drawdown_pct` | 15 | 1-80 |
| `stop_loss_pct` | 8 | 1 到最大回撤 |
| `take_profit_pct` | 20 | 1-300 |
| `trailing_stop_pct` | 8 | 1 到最大回撤 |
| `stocks` | 空 | 最多 30；与 sectors 至少一个非空 |
| `sectors` | 空 | 最多使用前 20 个 |
| `max_candidates` | 12 | 2-30 |
| `max_positions` | 2 | 1-8，且不超过候选数 |
| `max_iterations` | 10 | 1-20 |
| `take_profit_mode` | `trailing` | `trailing/fixed` |
| `risk_profile` | `balanced` | `auto/aggressive/balanced/conservative` |
| `backtest_window_years` | 3 | 1、3 或 5 |
| `strategy_style` | `auto` | auto 或白名单细分策略 |
| `preference_weights` | 档位默认 | 各项 0-100，服务归一化到 1 |

### Harness

| 路径 | 请求字段 | 返回/副作用 |
|---|---|---|
| `/api/harness/bad-cases` | `case_type,input,expected?,observed?,source?,page?,severity?,notes?` | 记录坏案例，`201` |
| `/api/harness/runs` | `workflow,input,intent?,thread_key?,requested_by?` | 创建允许的持久化工作流，`201` |
| `/api/harness/runs/{run_key}/resume` | `requested_by?` | 从最后成功工具恢复 |
| `/api/harness/runs/{run_key}/cancel` | `requested_by?` | 取消可取消运行 |
| `/api/harness/approvals/{approval_key}/resolve` | `approved,resolved_by,confirmed` | 解决人工审批 |
| `/api/harness/candidates/generate` | `bad_case_id` | 生成受控配置候选，`201` |
| `/api/harness/evaluate` | `candidate_id?` | 评测指定或待评测候选 |
| `/api/harness/autonomous/run` | `stock_limit` 1-100 | 运行一轮自动审计；固定 `auto_apply=false` |
| `/api/harness/candidates/approve` | `candidate_id,approved_by,confirmed` | 人工批准配置候选 |
| `/api/harness/versions/rollback` | `version_key,approved_by,confirmed` | 回滚配置版本 |
| `/api/harness/code-evolution/run` | `symbols?`, `stock_limit?`, `max_drawdown?`, `auto_promote?` | 创建、隔离测试并门禁源码候选 |
| `/api/harness/code-evolution/rollback` | `reason,approved_by,confirmed` | 明确确认后回滚源码版本 |

Harness 不接受任意 Shell 或任意工具名。可用 workflow 和工具由 `agent_harness.py` 白名单定义。

### 研究、资料和风险

| 路径 | 请求字段 | 返回/副作用 |
|---|---|---|
| `/api/research/backtest` | `strategy_key,symbol` | 运行本地策略回测 |
| `/api/research/factors/run` | `factor_key` | 运行本地因子评估 |
| `/api/stock-comparison` | `stocks,profile,refresh?` | POST 版股票对比 |
| `/api/collect/bilibili` | `target,use_browser?` | 采集公开元数据或显式本机会话并持久化原始响应 |
| `/api/collect/x` | `query,max_results` | 调用 X 官方 recent search，需要 bearer token |
| `/api/collect/sec` | `cik` | 调用 SEC submissions JSON，需要合规 identity |
| `/api/reports/refresh` | 空对象 | 触发统一资料增量刷新 |
| `/api/reports/stock` | `stock` | 注册并刷新单只股票资料 |
| `/api/reports/sync/start` | `mode=full|daily,restart?` | 启动/复用批量同步 |
| `/api/reports/sync/pause` | 空对象 | 请求暂停批量同步 |
| `/api/risk/evaluate` | `cost_price,current_price,highest_since_entry?,market,asset_type` | 计算止损、止盈、研究区和纪律动作；不下单 |

## 4. 响应中的关键状态

| 状态 | 含义 |
|---|---|
| `ACTIVE` | 通过门禁并已发布的版本 |
| `SNAPSHOT` | 基于已评测候选生成的即时快照 |
| `RULE_SNAPSHOT` | 缓存数据上的确定性即时研究，未代表完整组合样本外验证 |
| `REJECTED` | 未通过风险或非退化门禁 |
| `UNCHANGED` | 数据、模型和候选与已发布版本相同 |
| `WATCH` | 观察/复核信号，不是正式买卖信号 |
| `ACKNOWLEDGED` | 用户已确认看过研究信号 |
| `DISMISSED` | 用户忽略该信号 |
| `DRY_RUN` | 邮件已进入 outbox，但未外发 |

任何组合或预测响应都应包含 `research_only=true` 和 `order_execution=false`。

## 自然语言研究与服务状态

- `GET /api/services`：后台线程存活、等待时点、自动重启次数、Codex 登录和学习评测摘要。
- `GET /api/research-agent`：Agent 状态与最近 30 条研究。
- `POST /api/research-agent/runs`：提交 `{ "question": "投资需求", "parent_key": null }`；返回 202 和持久 run_key。
- `GET /api/research-agent/runs/{run_key}`：阶段、事件、冻结计划、本地计算证据、报告和用量。
- `POST /api/research-agent/runs/{run_key}/cancel`：取消研究，保留已完成步骤。
- `POST /api/research-agent/evolve`：通过 Harness 提交行情更新、预测评分、模型/策略和受限源码评测闭环。

上述接口沿用现有认证、来源校验和限流。页面入口 `/?view=agent`。后台队列一次调用一个 Codex，最多等待 8 项；服务中断后记录 INTERRUPTED，可显式重试。

## 投资日报与邮件投递

- `GET /api/email-settings`：返回已脱敏的 SMTP 配置和发送开关，不返回授权码。
- `POST /api/email-settings`：保存 `host,port,user,security=ssl|starttls,password,enabled,default_target`。Windows DPAPI 加密授权码。
- `POST /api/email-settings/verify`：验证 SMTP 连接与登录；不等同于收件成功。
- `POST /api/notification-subscriptions`：可带 `id` 更新，支持 `digest_kinds=[MARKET,COMPANIES]`、`watch_stocks`（名称或代码）、`send_time=08:30`、`event_kinds=[]`。关注名单不写入持仓；修改订阅会取消旧内容的待发邮件。
- `POST /api/digests/preview`：`digest_kinds,watch_stocks,report_date` 生成所选日期日报预览，缺少数据时明确标注。
- `POST /api/digests/send`：`subscription_id,report_date` 发送已保存订阅的日报，同一订阅同一天不重复发送。
- `POST /api/notifications/{id}/retry`：重试失败邮件。
- `POST /api/notifications/{id}/received`：用户确认实际收到，记录与 SMTP 接受状态分离。

通知线程每 30 秒检查一次发送时间，每天按北京时间总结前一天；本机服务需要运行。错过时间但当天重新启动会补发前一天。报告只使用所选日期行情和新闻，以及截至该日已公告财报，不把未来或演示行情补入空缺。
