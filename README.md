# Rooftop

Rooftop 是一个本地运行、面向 A 股的量化研究与盘后信号系统。它可以比较 2-8 只股票，也可以根据本金、期限、目标收益、最大回撤、止损、止盈、板块和最多持仓数，生成受约束的研究组合、历史回测、未来情景区间和待人工复核的买入/卖出/调仓信号。

Rooftop **不连接券商、不提交订单、不承诺收益**。所有金额、股数和价格区间都是研究输出；只有用户导入真实持仓后，系统才会计算真实账户口径的成本、盈亏和可卖数量。

## 1. 当前能力

- 股票对比：激进、均衡、保守三种研究口径，综合技术面、基本面、估值、催化剂和下行风险。
- 约束选股：输入本金、期限、收益目标、最大回撤、止损、止盈、板块、候选池和最多持仓数。
- 资金分配：按 A 股 100 股整手约束给出研究金额、股数、现金余量和目标权重。
- 未来情景：组合和单股均提供 P10/P50/P90 情景路径；未通过样本外门禁时明确标记为历史基准情景。
- 回测：按时间顺序进行滚动验证和最终留出评估，使用下一交易日开盘或下一根完成 K 线开盘模拟成交。
- 交易约束：佣金、最低佣金、卖出印花税、滑点、整手、成交量参与上限、停牌、涨跌停近似和 T+1。
- 财务与估值：公告日可得数据、每日估值快照、声明式自定义模型、版本化激活范围和缺失覆盖率。
- 信号中心：`BUY / SELL / REBALANCE / WATCH`，支持确认、忽略、版本追踪和失效条件。
- 邮件订阅：显式订阅、最低置信度、事件过滤、去重、失败退避和本地 outbox；默认不外发。
- 行情同步：A 股交易时段每分钟更新关注池；日线、历史、财报和研报由独立后台任务刷新。
- 多源资料：通达信公开行情、本地通达信文件、AKShare、BaoStock，以及可选的 Bilibili、X API、SEC EDGAR。
- 搜索：本地严格匹配；可选 Qwen3 Embedding 语义检索。
- 持续学习：在线概率模型、可选 Qwen3 数值适配器、5 分钟策略演化、组合策略演化和受控源码候选。
- Harness：固定工作流规划、持久化步骤、工具白名单、心跳、租约、人工审批、失败恢复和版本回滚。

## 2. 系统边界

| 能力 | 是否支持 | 说明 |
|---|---:|---|
| 股票研究、回测、情景区间 | 是 | 结果保留数据时间、模型版本和证据覆盖 |
| 研究资金分配 | 是 | 不代表真实持仓或成交 |
| 导入真实持仓 | 是 | 仅用于成本、盈亏和卖出复核；不会连接券商 |
| 邮件提醒 | 是 | 默认 dry-run；显式启用后才发送 |
| 自动下单 | 否 | 代码和数据库均不包含订单执行链路 |
| 收益保证 | 否 | 目标收益只是筛选约束，不是预测承诺 |
| 云端模型 API | 默认否 | 核心路径是本地确定性模型和可选本地 Qwen 模型 |

## 3. 架构

```text
公开/本地数据源
  |-- 通达信公开 TCP / 通达信本地文件
  |-- AKShare / BaoStock / AKQuant
  |-- 公告、研报、Bilibili、X、SEC
  v
不可变原始响应 -> 规范化校验 -> SQLite + data_lake
                                      |
                    +-----------------+------------------+
                    |                 |                  |
                 因子研究          模型与回测          文档搜索
                    |                 |                  |
                    +---------- 组合与信号 -------------+
                                      |
                            HTTP API + 本地 Web UI
                                      |
                           邮件 outbox / 人工复核
```

Web 请求只读本地 SQLite 或创建受控任务。外部抓取、日线回填、模型训练和长期回测在后台线程或独立 Python 进程执行，避免上游超时阻塞页面。

### 代码目录

```text
.codex-plugin/plugin.json       Codex 插件清单
.zcode-plugin/plugin.json       ZCode 兼容清单
commands/                       插件命令入口
skills/                         三档股票对比与主可视化技能
scripts/                        环境、启动、备份、调度、打包脚本
runtime/app/server.py           HTTP 服务、鉴权、限流和后台调度
runtime/app/db.py               SQLite schema、迁移和数据湖入口
runtime/app/data_sources/       行情、研报、资料和桌面数据适配器
runtime/app/stock_compare.py    2-8 股票对比与风险档位
runtime/app/quant_portfolio.py  约束选股、资金分配、组合情景和发布门禁
runtime/app/timeframe_forecast.py 多周期概率区间与校准
runtime/app/model_registry.py   声明式财务/估值/因子/策略/风险模型
runtime/app/continuous_learning.py 在线概率模型和每日学习闭环
runtime/app/deep_learning.py    可选 Qwen3 数值序列适配器
runtime/app/intraday_strategy.py 5 分钟策略演化和回测
runtime/app/strategy_evolution.py 三档组合策略演化
runtime/app/signal_service.py   信号、订阅和邮件队列
runtime/app/agent_harness.py    受限工作流、工具调用、审批和恢复
runtime/app/code_evolution.py   AST 白名单、隔离测试、晋级和回滚
runtime/app/static/             无构建步骤的 HTML/CSS/JavaScript 前端
runtime/tests/                  单元与契约回归测试
runtime/docs/                   架构、接口、模型、数据和部署细节
```

完整模块说明见 [架构文档](runtime/docs/ARCHITECTURE.md)。

## 4. 模型到底用了什么

Rooftop 不是“让一个大模型直接猜股价”。不同任务使用不同模型，并且每个模型都必须保留数据时间和验证状态。

| 任务 | 当前模型 | 默认启用 | 作用 |
|---|---|---:|---|
| 股票对比 | 确定性风险档位规则 + 历史趋势回测 | 是 | 激进/均衡/保守口径排序 |
| 即时约束选股 | 多因子确定性评分 | 是 | 技术、财务、概率、流动性、稳定性综合排序 |
| 多周期价格区间 | 历史相似状态加权分布 + split conformal 校准 | 是 | 1日、1周、1月、1季、1年 P10/P50/P90 |
| 每日上涨概率 | 在线逻辑概率模型 | 是 | 仅用已实现结果逐日更新系数 |
| 财务/估值模型 | 声明式 JSON 评分模型 | 是 | 按默认、行业、个股作用域计算维度分数 |
| 语义搜索 | `Qwen/Qwen3-Embedding-0.6B`，512 维 | 可选 | 中文金融资料向量检索 |
| 深度时序研究 | `Qwen/Qwen3-0.6B` 数值 token 适配器 | 可选 | 下一交易日方向概率和预期收益 |
| 5 分钟策略 | 参数白名单内的规则策略演化 | 是 | 已完成 5 分钟 K 线、下一根开盘模拟 |
| Harness 规划 | `bounded-domain-planner` 确定性规划器 | 是 | 将固定意图映射到允许工具，不是开放式 Agent |

### 在线概率模型

特征为 `bias`、5 日动量、20 日动量、5/20 日均线差、20 日波动率、成交量 z-score、公开资料情绪和市场 5 日动量。模型先预测，再在真实结果出现后更新；同一天的结果不会用于同一天预测。版本门禁比较方向准确率、Brier 分数、收益误差、策略回报和最大回撤。

### Qwen3 数值适配器

深度研究使用本地 `Qwen/Qwen3-0.6B` 主干，将标准化数值特征投影为输入 embedding，并增加方向分类头和收益回归头。权重不在本仓库中；运行时校验 `model.safetensors` 大小和 SHA-256。设备由 `ARGUS_QWEN3_DEVICE=auto|cpu|cuda` 控制。没有本地权重时系统降级，不会调用在线 API。

### 多周期区间

分钟周期只作为描述性上下文。日线周期必须经过按时间排序的 walk-forward、条件相似样本分布、split conformal 校准和相对历史基准的非退化门禁。没有足够样本或没有证据优于基准时，前端必须明确披露，不能把宽泛区间包装成高置信预测。

更完整的模型输入、输出、训练、门禁与降级规则见 [模型栈说明](runtime/docs/MODEL_STACK.md)。

## 5. 环境要求

- Windows 10/11 或 Windows Server。
- PowerShell 5.1+。
- Python 3.12。
- 建议至少 8 GB 内存。
- 本地 Qwen 模型可使用 CPU；训练建议 NVIDIA GPU。RTX 50 系显卡需要匹配的 PyTorch CUDA 构建。
- 通达信客户端不是必需项；免费公开节点可提供行情，但稳定性、历史深度和授权边界由上游决定。

## 6. 安装

```powershell
git clone https://github.com/tianyuzong/rooftop.git
Set-Location rooftop

$env:ARGUS_STOCK_COMPARE_VENV = "C:\venvs\argus-stock-comparison"
./scripts/setup_stock_compare_env.ps1
```

安装脚本会创建或复用独立环境，安装行情、研究和语义检索依赖，并在 `runtime/.venv-path` 写入本机环境路径。该文件已被 Git 忽略。

若使用自定义 PyPI 镜像：

```powershell
$env:ARGUS_PIP_INDEX_URL = "https://pypi.org/simple"
./scripts/setup_stock_compare_env.ps1
```

## 7. 启动

### 信号中心

```powershell
./scripts/start_stock_compare.ps1 -View signals
```

### 股票对比

```powershell
./scripts/start_stock_compare.ps1 `
  -View compare `
  -Stocks "贵州茅台,五粮液,宁德时代" `
  -Profile balanced
```

`Profile` 支持 `aggressive / balanced / conservative` 及对应中文别名。

### 约束选股

```powershell
./scripts/start_stock_compare.ps1 -View harness
```

启动脚本输出 JSON，`url` 为页面地址。未指定端口时会选择可用端口，并且只复用 Python 环境、插件版本、运行时代码指纹和数据湖都一致的健康服务。

### 指定数据目录

```powershell
$env:ARGUS_DATA_LAKE = "C:\RooftopData\data_lake"
./scripts/start_stock_compare.ps1 -View signals
```

不要把数据湖放进 Git 仓库。它可能包含行情原始响应、研报正文、邮箱地址、模型输出和审计日志。

## 8. 最小 API 示例

默认本机服务不需要令牌。假设地址是 `http://127.0.0.1:8765`：

```powershell
$base = "http://127.0.0.1:8765"
Invoke-RestMethod "$base/api/health"
Invoke-RestMethod "$base/api/market/quotes?symbols=600519,000858,300750"
```

登记研究条件：

```powershell
$body = @{
  input = @{
    name = "白酒与新能源"
    capital = 100000
    horizon_months = 12
    target_return_pct = 20
    max_drawdown_pct = 15
    stop_loss_pct = 8
    take_profit_pct = 20
    trailing_stop_pct = 8
    sectors = @("白酒", "新能源")
    max_candidates = 12
    max_positions = 8
    max_iterations = 10
    risk_profile = "balanced"
    take_profit_mode = "trailing"
  }
} | ConvertTo-Json -Depth 6

Invoke-RestMethod `
  -Method Post `
  -Uri "$base/api/quant/mandates" `
  -ContentType "application/json" `
  -Body $body
```

接口不会在请求线程中启动训练或完整回测。它优先返回最近的 `ACTIVE` 版本；没有活动版本但缓存足够时返回明确标记的 `SNAPSHOT` 或 `RULE_SNAPSHOT`，完整评测留给盘后任务。

全部 GET/POST 路由、请求字段、状态码和副作用见 [API 参考](runtime/docs/API_REFERENCE.md)。

## 9. 配置

Rooftop 只从进程环境读取本机路径和凭证，不读取仓库内的密钥 JSON，也不会自动加载 `.env`。`.env.example` 只是字段清单。

最常用配置：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `ARGUS_STOCK_COMPARE_VENV` | 无 | 独立 Python 环境目录 |
| `ARGUS_DATA_LAKE` | `runtime/data_lake` | SQLite、原始响应、日志和模型输出根目录 |
| `ARGUS_TDX_HOME` | 自动探测 | 通达信客户端目录 |
| `ARGUS_MARKET_PROVIDER` | `tdx` | `tdx` 或 `mixed` |
| `ARGUS_MARKET_SYMBOLS` | 默认关注标的 | 后台基础关注池 |
| `ARGUS_LIVE_REFRESH_ENABLED` | `1` | 是否运行行情后台刷新 |
| `ARGUS_EMAIL_SEND_ENABLED` | 未启用 | 只有值为 `1` 才发送邮件 |
| `ARGUS_QWEN3_MODEL_PATH` | 数据湖模型目录 | 本地 Qwen3-0.6B 路径 |
| `ARGUS_QWEN3_DEVICE` | `auto` | `auto / cpu / cuda` |
| `ARGUS_REMOTE_TOKEN_FILE` | 无 | 非回环监听强制要求的令牌文件 |
| `ARGUS_ALLOWED_HOSTS` | 不限制 | 远程部署 Host 白名单 |
| `ARGUS_ALLOWED_ORIGINS` | 同源 | 修改请求 Origin 白名单 |

全部变量、类型、范围、默认值和安全级别见 [配置参考](runtime/docs/CONFIGURATION.md)。

## 10. 数据源与刷新节奏

### 行情

- 默认在线源：MIT `tdxrs` 客户端访问通达信公开 TCP 节点。
- 本地补充：`ARGUS_TDX_HOME` 指向通达信目录；本地文件为空时只报告不可用。
- `mixed` 模式保留腾讯/东方财富/BaoStock 兼容采集路径，但默认 `tdx` 模式不会在线调用旧行情源。
- QMT/miniQMT 和 Futu OpenD 当前只做只读就绪探测，不包含交易方法。

### 刷新

- 交易时段：09:30-11:30、13:00-15:00，每 60 秒刷新已跟踪报价和当前分钟线。
- 日线：默认每 6 小时检查一次。
- 历史：默认每 24 小时检查一次。
- 研报/公告：默认每小时检查当天增量。
- 持续学习：后台轮询器负责盘前、盘后和失败重试；法定休市日跳过。

HTTP 行情接口只读 SQLite。即使上游节点超时，页面仍可显示最后可信快照并标记时间。

## 11. 邮件订阅

```powershell
$env:ARGUS_SMTP_HOST = "smtp.example.com"
$env:ARGUS_SMTP_PORT = "465"
$env:ARGUS_SMTP_USER = "research@example.com"
$env:ARGUS_SMTP_PASSWORD = "在本机设置，不要提交"
$env:ARGUS_ALERT_TO = "research@example.com"
$env:ARGUS_EMAIL_SEND_ENABLED = "1"
```

未设置 `ARGUS_EMAIL_SEND_ENABLED=1` 时，消息只进入本地 `alert_outbox`。创建订阅后才会按事件类型和最低置信度排队。SMTP 密码只存在于进程环境，不写入 SQLite。

## 12. 远程部署

默认只绑定 `127.0.0.1`。绑定非回环地址时必须提供令牌文件：

```powershell
./scripts/start_stock_compare.ps1 `
  -View signals `
  -BindAddress 0.0.0.0 `
  -PublicHost argus.example.com `
  -PublicScheme https `
  -TokenFile C:\ArgusSecrets\remote.token `
  -AllowedHosts "argus.example.com" `
  -AllowedOrigins "https://argus.example.com"
```

远程 API 使用 `X-Argus-Token` 请求头。所有 API 有按客户端 IP 的内存限流；修改请求还校验 Origin，并写入 `api_audit_log`。生产环境仍必须使用 HTTPS 反向代理、防火墙来源限制、最小权限账户、独立数据盘和定期备份。

详见 [安全部署](runtime/docs/SECURITY_DEPLOYMENT.md)。

## 13. 后台任务与备份

安装工作日盘前/盘后任务：

```powershell
./scripts/install_continuous_learning_tasks.ps1 -StockLimit 20 -MaxDrawdown 0.15
```

手动运行持续学习：

```powershell
./scripts/run_continuous_learning.ps1 -Phase POST_CLOSE -StockLimit 20
```

创建一致性备份：

```powershell
./scripts/backup_argus_data.ps1 -Keep 14
```

备份包含 SQLite 一致性快照和必要元数据，不包含可重建缓存、外部模型权重或 Python 环境。恢复步骤见 [备份与恢复](runtime/docs/BACKUP_AND_RECOVERY.md)。

## 14. 测试

```powershell
$venv = (Get-Content -Raw runtime/.venv-path).Trim()
$python = @(
  (Join-Path $venv "python.exe"),
  (Join-Path $venv "Scripts/python.exe")
) | Where-Object { Test-Path $_ } | Select-Object -First 1

Push-Location runtime
try {
  & $python -m unittest discover -s tests -v
}
finally {
  Pop-Location
}
```

测试覆盖数据库迁移、行情落库、图表、股票对比、组合约束、回测、预测区间、模型注册、邮件、Harness、持续学习、源码演化、权限边界和前端契约。

## 15. 打包

```powershell
./scripts/build_portable_plugin.ps1
```

产物写入 `dist/`，只包含源码、插件清单、技能、脚本、文档、测试和依赖清单；不包含数据湖、模型权重、数据库、虚拟环境或密钥。

## 16. 可信度与已知限制

- 免费行情可能延迟、缺失、限流或改变协议；页面时间戳比“实时”字样更可信。
- 涨跌停模型是以前收盘价为基准的近似，尚未完整覆盖各板块、ST 和特殊交易状态。
- 历史回测不能证明未来有效；重复使用过的留出窗口不会继续宣称“未触碰”。
- `RULE_SNAPSHOT` 是缓存数据上的即时研究，不等同于完整组合样本外评测。
- P10/P50/P90 是条件历史分布，不是置信保证；样本不足会扩大区间或降级。
- 财务数据按公告日可得性对齐，缺失字段会降低覆盖率而不是用未来数据补齐。
- Bilibili 浏览器会话、X API、SEC、SMTP 和本地模型均为可选配置，不配置时必须显式显示未就绪或降级。
- 数据和模型许可证由使用者负责核对；商用前必须确认上游授权。

## 17. 开发约束

- 不得增加券商下单、账户授权或自动交易路径。
- 外部响应必须先保存原始证据，再规范化入库。
- 所有预测必须携带 `data_asof`、模型/规则版本和验证状态。
- 用户自定义模型只允许声明式 JSON，不执行 Python、Shell、JavaScript、Excel 宏或任意表达式。
- 源码演化只能修改 `runtime/app/evolvable/intraday_recipes.py`，并经过 AST 白名单、隔离数据库、全量测试、验证集和留出集门禁。
- 新接口应补充 API 文档和测试；新环境变量应同步更新 `.env.example` 与配置文档。

## 18. 文档索引

- [配置参考](runtime/docs/CONFIGURATION.md)
- [API 参考](runtime/docs/API_REFERENCE.md)
- [模型栈说明](runtime/docs/MODEL_STACK.md)
- [系统架构](runtime/docs/ARCHITECTURE.md)
- [信号与通知](runtime/docs/SIGNALS_AND_NOTIFICATIONS.md)
- [模型注册表](runtime/docs/MODEL_REGISTRY.md)
- [策略研究](runtime/docs/STRATEGY_RESEARCH.md)
- [数据覆盖](runtime/docs/DATA_COVERAGE.md)
- [免费数据源](runtime/docs/FREE_DATA_SOURCES.md)
- [语义搜索](runtime/docs/SEARCH.md)
- [安全部署](runtime/docs/SECURITY_DEPLOYMENT.md)
- [备份与恢复](runtime/docs/BACKUP_AND_RECOVERY.md)

## License

MIT，见 [LICENSE](LICENSE)。
