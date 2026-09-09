# 配置参考

Rooftop 通过环境变量、启动脚本参数和邮件设置页面配置。邮件页面设置保存于数据湖 `secrets/email_settings.json`，优先于对应环境变量，授权码使用 Windows DPAPI 加密。项目不会自动读取 `.env`；仓库根目录的 `.env.example` 只用于说明字段。凭证、令牌、邮箱和本机路径应在启动进程的 PowerShell 会话或受控服务账户中设置。

布尔变量统一使用字符串 `1` 表示启用。未说明的空值表示不配置。

## 1. 启动脚本参数

`scripts/start_stock_compare.ps1` 是推荐入口。

| 参数 | 默认 | 约束 | 说明 |
|---|---|---|---|
| `-Stocks` | 空 | 对比视图必须 2-8 只 | 逗号、中文逗号、顿号、分号或空格分隔 |
| `-Profile` | `balanced` | `aggressive/balanced/conservative` | 支持中文别名 |
| `-View` | `auto` | `auto/agent/signals/compare/harness` | `auto` 有股票时进入对比，否则进入信号中心 |
| `-Port` | `0` | 0 或有效 TCP 端口 | 0 表示选择可用端口 |
| `-BindAddress` | `127.0.0.1` | IPv4/IPv6 | 非回环地址必须提供 `-TokenFile` |
| `-PublicHost` | 自动 | 主机名或 IP | 只用于生成页面 URL |
| `-PublicScheme` | `http` | `http/https` | HTTPS 由外部反向代理终止 |
| `-DataLakePath` | 自动 | 本机绝对或相对路径 | 优先级高于环境变量 |
| `-TokenFile` | 空 | 非空文本文件 | 内容作为 `X-Argus-Token` |
| `-AllowedHosts` | 空 | 逗号分隔 | HTTP Host 白名单 |
| `-AllowedOrigins` | 空 | 逗号分隔完整 origin | 修改请求的 Origin 白名单 |
| `-ApiRateLimitPerMinute` | `180` | 30-10000 | 每客户端 IP、每分钟 API 请求数 |

数据湖选择优先级：`-DataLakePath` > 当前进程 `ARGUS_DATA_LAKE` > 用户级 `ARGUS_DATA_LAKE` > `runtime/data_lake`。

## 2. Python 与路径

| 变量 | 默认 | 必填 | 说明 |
|---|---|---:|---|
| `ARGUS_STOCK_COMPARE_VENV` | 无 | 首次安装是 | Python venv 或 conda 环境根目录 |
| `ARGUS_BOOTSTRAP_PYTHON` | 自动探测 | 否 | 创建 venv 使用的 Python 可执行文件 |
| `ARGUS_PIP_INDEX_URL` | 清华 PyPI 镜像 | 否 | 安装脚本的 pip index |
| `ARGUS_DATA_LAKE` | `runtime/data_lake` | 否 | 运行数据唯一根目录 |
| `ARGUS_TDX_HOME` | 进程/常见目录探测 | 否 | 通达信客户端目录；本地文件为空时不冒充已接入 |
| `ARGUS_QMT_HOME` | 空 | 否 | xtquant/QMT 模块目录，只读探测 |
| `ARGUS_FUTU_PORT` | `11111` | 否 | 本机 Futu OpenD 探测端口，只读探测 |

`runtime/.venv-path` 由安装脚本生成，只保存本机环境位置，不能提交到 Git。

## 3. HTTP 服务与安全

| 变量 | 默认 | 范围/格式 | 说明 |
|---|---|---|---|
| `ARGUS_BIND_ADDRESS` | `127.0.0.1` | IP 地址 | 服务监听地址 |
| `ARGUS_PUBLIC_HOST` | `127.0.0.1` | 主机名/IP | 健康信息和页面 URL 使用 |
| `ARGUS_REMOTE_TOKEN_FILE` | 空 | 本机文件路径 | 配置后所有 `/api/*` 都要求 `X-Argus-Token` |
| `ARGUS_ALLOWED_HOSTS` | 空 | 逗号分隔主机 | 为空时不额外限制 Host |
| `ARGUS_ALLOWED_ORIGINS` | 同源 | 逗号分隔 origin | 仅修改请求校验；例如 `https://rooftop.example.com` |
| `ARGUS_API_RATE_LIMIT_PER_MINUTE` | `180` | 30-10000 | 内存限流，服务重启后窗口清空 |
| `ARGUS_MAX_CONCURRENT_COMPARISONS` | `8` | 1-32 | 并发股票对比槽位 |

规则：

1. 监听非回环地址而未配置令牌时，服务拒绝启动。
2. 令牌文件存在但为空时，API 返回 `503`。
3. 缺少令牌返回 `401`，错误令牌返回 `403`。
4. Host 不在白名单返回 `421`；超限返回 `429`。
5. POST 请求还要通过 Origin 校验，并写入 `api_audit_log`。
6. 静态页面不要求 API 令牌，但页面调用 API 时必须携带令牌。

## 4. 行情刷新

| 变量 | 默认 | 有效范围 | 说明 |
|---|---|---|---|
| `ARGUS_MARKET_PROVIDER` | `tdx` | `tdx/mixed` | 默认仅在线调用通达信；mixed 保留兼容源 |
| `ARGUS_MARKET_SYMBOLS` | `000001.SH,512400,562500,600519` | 逗号分隔 | 后台基础关注池 |
| `ARGUS_LIVE_REFRESH_ENABLED` | `1` | `0/1` | 行情后台线程总开关 |
| `ARGUS_LIVE_SYMBOL_LIMIT` | `100` | 4-200 | 基础池、对比历史、持仓和活动组合合并后的上限 |
| `ARGUS_MARKET_POLL_SECONDS` | `10` | 最小 5 | 调度线程轮询间隔，不等于行情刷新频率 |
| `ARGUS_MINUTE_REFRESH_SECONDS` | `60` | 最小 60 | A 股开盘时的报价/分钟线刷新间隔 |
| `ARGUS_DAILY_REFRESH_SECONDS` | `21600` | 最小 3600 | 日线刷新间隔，默认 6 小时 |
| `ARGUS_HISTORY_REFRESH_SECONDS` | `86400` | 最小 3600 | 慢速历史回填间隔，默认 24 小时 |

A 股开盘判断使用 `Asia/Shanghai`：09:30-11:30、13:00-15:00，并结合本地交易日历；日线和历史任务与盘中报价使用独立进程。

## 5. 研报、公告和资料

| 变量 | 默认 | 范围 | 说明 |
|---|---|---|---|
| `ARGUS_REPORT_REFRESH_ENABLED` | `1` | `0/1` | 研报库后台刷新总开关 |
| `ARGUS_REPORT_POLL_SECONDS` | `3600` | 最小 3600 | 统一资料同步检查间隔 |
| `ARGUS_REPORT_DAILY_POLL_SECONDS` | `3600` | 最小 3600 | 当日增量检查间隔 |
| `ARGUS_REPORT_MAX_EQUITIES` | `8` | 2-20 | 单轮重点股票数量 |
| `ARGUS_REPORT_BULK_DELAY_SECONDS` | `0.25` | 0.1-5.0 | 批量股票之间的节流延迟 |
| `ARGUS_DAILY_SOCIAL_SYMBOLS` | `3` | >=0 | 每日允许采集社交资料的标的数量 |
| `ARGUS_BILIBILI_BROWSER` | 未配置 | `chrome/edge/firefox` | 可选本机浏览器会话；不保存密码 |
| `ARGUS_X_BEARER_TOKEN` | 未配置 | X 官方 API token | 只存在于环境变量，不写数据库 |
| `ARGUS_SEC_IDENTITY` | 未配置 | `产品名 email` | SEC EDGAR 合规 User-Agent，例如 `RooftopResearch a@b.com` |

Bilibili、X 和 SEC 都是可选源。未配置时状态必须显示为未配置，而不是伪造数据。

## 6. Harness 与持续学习

| 变量 | 默认 | 范围 | 说明 |
|---|---|---|---|
| `ARGUS_HARNESS_AUTONOMY_ENABLED` | `1` | `0/1` | Harness 后台轮询总开关 |
| `ARGUS_HARNESS_POLL_SECONDS` | `86400` | 最小 3600 | 自动 Harness 周期 |
| `ARGUS_HARNESS_STARTUP_DELAY_SECONDS` | `20` | 最小 5 | 服务启动后的首次延迟 |
| `ARGUS_HARNESS_STOCK_LIMIT` | `20` | 1-100 | 自动研究股票上限 |
| `ARGUS_HARNESS_RUN_LEASE_MINUTES` | `240` | 最小 30 | 运行租约；超时才允许接管 |
| `ARGUS_CONTINUOUS_LEARNING_ENABLED` | `1` | `0/1` | 持续学习调度总开关 |
| `ARGUS_CONTINUOUS_LEARNING_POLL_SECONDS` | `60` | 最小 30 | 调度状态检查间隔 |
| `ARGUS_CONTINUOUS_LEARNING_STARTUP_DELAY_SECONDS` | `30` | 最小 5 | 首次调度延迟 |
| `ARGUS_CONTINUOUS_LEARNING_STOCK_LIMIT` | `20` | 正整数 | 默认学习股票数量 |
| `ARGUS_POST_CLOSE_RETRY_MINUTES` | `30` | 最小 5 | 盘后数据尚未覆盖当天时的重试间隔 |
| `ARGUS_STRATEGY_RETRY_POLL_SECONDS` | `1800` | 最小 300 | 检查未通过三档策略的持久重试队列；门禁失败仍需等待新交易日数据 |
| `ARGUS_QUANT_RUN_LEASE_MINUTES` | `240` | 最小 30 | 量化组合运行租约 |

关闭后台开关不会删除已发布版本，页面继续使用最后一个可信版本。

## 7. 板块缓存

| 变量 | 默认 | 范围 | 说明 |
|---|---|---|---|
| `ARGUS_SECTOR_CACHE_ENABLED` | `1` | `0/1` | 板块候选池和基本面缓存总开关 |
| `ARGUS_SECTOR_CACHE_POLL_SECONDS` | `3600` | 最小 1800 | 缓存任务轮询间隔 |
| `ARGUS_SECTOR_CACHE_STARTUP_DELAY_SECONDS` | `5` | 最小 2 | 服务启动后延迟 |
| `ARGUS_SECTOR_CACHE_MARKET_BATCH` | `12` | 1-30 | 单批行情标的数 |
| `ARGUS_SECTOR_CACHE_FUNDAMENTAL_BATCH` | `3` | 1-10 | 单批基本面标的数 |

## 8. 本地 Qwen3 模型

| 变量 | 默认 | 范围 | 说明 |
|---|---|---|---|
| `ARGUS_QWEN3_MODEL_PATH` | `data_lake/models/qwen3/Qwen3-0.6B` | 目录 | 必须包含 `config.json` 和 `model.safetensors` |
| `ARGUS_QWEN3_MODEL_SHA256` | 代码内官方哈希 | 64 位十六进制 | 覆盖预期权重哈希，用于自管镜像 |
| `ARGUS_QWEN3_DEVICE` | `auto` | `auto/cpu/cuda` | `cuda` 不可用时显式失败；auto 自动降级 |
| `ARGUS_QWEN3_BATCH_SIZE` | GPU 128/CPU 16 | 1-512 | 训练和推理批大小 |

深度模型权重约 1.5 GB，不提交到仓库。语义搜索使用单独的 `Qwen/Qwen3-Embedding-0.6B`，由 sentence-transformers 缓存管理。

## 9. 邮件

| 变量 | 默认 | 必填条件 | 说明 |
|---|---|---:|---|
| `ARGUS_SMTP_HOST` | 空 | 外发时是 | SMTP 主机（SSL 或 STARTTLS） |
| `ARGUS_SMTP_PORT` | `465` | 否 | SMTP 端口，需匹配加密方式 |
| `ARGUS_SMTP_SECURITY` | `ssl` | 否 | `ssl`（常用 465）或 `starttls`（常用 587）；页面设置优先 |
| `ARGUS_SMTP_USER` | 空 | 外发时是 | 登录名，同时作为 From |
| `ARGUS_SMTP_PASSWORD` | 空 | 外发时是 | 应使用应用专用密码 |
| `ARGUS_ALERT_TO` | 空 | 否 | 没有订阅目标时的默认收件人 |
| `ARGUS_EMAIL_SEND_ENABLED` | 未启用 | 否 | 只有精确值 `1` 才允许发送 |

SMTP 主机、用户和密码必须同时存在才显示 configured。即使配置完整，未启用发送时仍为 dry-run。存在已保存的页面设置时，主机、端口、用户、加密方式和启用状态优先读取页面设置；不要仅修改环境变量就假定覆盖了页面配置。

## 10. 推荐配置组合

### 仅本机研究

```powershell
$env:ARGUS_STOCK_COMPARE_VENV = "C:\venvs\argus-stock-comparison"
$env:ARGUS_DATA_LAKE = "C:\RooftopData\data_lake"
./scripts/start_stock_compare.ps1 -View signals
```

### 关闭耗时后台任务

```powershell
$env:ARGUS_REPORT_REFRESH_ENABLED = "0"
$env:ARGUS_HARNESS_AUTONOMY_ENABLED = "0"
$env:ARGUS_CONTINUOUS_LEARNING_ENABLED = "0"
$env:ARGUS_SECTOR_CACHE_ENABLED = "0"
./scripts/start_stock_compare.ps1 -View compare -Stocks "600519,000858" -Profile balanced
```

### 远程只读试用

1. 创建仓库外令牌文件并限制文件 ACL。
2. 配置 HTTPS 反向代理。
3. 使用 `-BindAddress`、`-TokenFile`、`-AllowedHosts` 和 `-AllowedOrigins` 启动。
4. 防火墙只允许反向代理或可信来源访问后端端口。
5. 如不需要邮件外发，在页面关闭发送；未保存页面配置时使用 `ARGUS_EMAIL_SEND_ENABLED=0`。

## Codex 投资研究 Agent

`ARGUS_CODEX_EXECUTABLE` 可指定本机 Codex CLI 路径；默认从 PATH 或标准安装位置发现。
`ARGUS_CODEX_MODEL` 可指定账户支持的模型；未配置时沿用 Codex CLI 默认模型。
需要先在本机执行 `codex login`。Agent 使用已有登录，通过 `codex exec --output-schema` 提取投资约束和解释本地计算结果。
Windows 系统代理会传递给 Codex 子进程。Agent 禁用 shell、外部应用和多代理工具；API 令牌不传给模型。
投资人的自然语言需求和精选本地证据会发送至 Codex。研究历史保存于本地 research_agent_runs，支持追问、取消和重试。
