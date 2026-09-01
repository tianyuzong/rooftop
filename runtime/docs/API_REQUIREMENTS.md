# 外部能力与配置边界

本项目不采购任何金融数据 API，也不读取存放 API Key 的 JSON 文件。行情、公告、财报、新闻和宏观数据优先采用免费、官方或公开来源；需要凭证的外部服务只允许通过本机环境变量显式提供。

## 本地行情 API

以下接口只读取 SQLite，不会在 HTTP 请求中同步访问外部网站：

- `GET /api/market/quotes?symbols=600519,512400,000001.SH`：最新本地实时快照、来源和观察时间。
- `GET /api/market/minutes/600519?limit=300`：最近的分钟记录，最多 2000 条。
- `GET /api/assets/512400/chart?period=15m`：读取指定周期图表；周期为 `time,5d,1m,5m,15m,30m,60m,120m,1d,1w,1mo,1q,1y`。
- `GET /api/search?q=...&mode=exact|semantic&page=research|strategy|reports`：页面级严格/语义搜索。
- `POST /api/search/reindex`：增量更新本地向量索引。
- `GET /api/intelligence-sources`：查看公开信息源配置状态，不返回任何凭证。
- `GET /api/reports/library`：读取本地研报、公告和宏观日历归档。
- `POST /api/reports/refresh`：触发后台刷新公开研报和公告。
- `POST /api/research/backtest`：运行本地 AKQuant 回测。
- `POST /api/research/factors/run`：运行本地因子评估。
- `GET /api/health`：数据源健康状态和最后成功时间。
- `GET /api/models`：读取研究模型定义、版本和激活范围。
- `POST /api/models` 与 `/api/models/{id}/activate`：创建声明式模型草稿，并经明确批准后激活。
- `GET /api/signals`：读取盘后研究信号与人工复核状态。
- `GET /api/notifications`：读取信号、邮件订阅、SMTP 状态和 outbox。
- `GET/POST /api/notification-subscriptions`：管理显式邮件订阅。

刷新任意标的使用本地命令：

```powershell
python -m app.data_sources.market --symbol 600519
```

普通六位代码按交易所前缀推断；指数使用 `000001.SH` 这类显式后缀，避免与平安银行 `000001` 混淆。

## 已移除接口

以下能力已从代码、数据库 schema 和前端导航中移除：

- 事件图观测：`/api/event-graphs`、Neo4j/Cypher 导出、事件图表。
- Agent Chat：`/api/chat/*`、模型白名单、外部模型路由、聊天记录表。
- API Key JSON：不再读取 `configure_list.json` 或项目内 JSON 密钥配置。

## 邮件

邮件通知已接入本地 outbox。只有显式创建订阅并设置 `ARGUS_EMAIL_SEND_ENABLED=1` 才会外发；否则保留为可审计 dry-run：

```text
ARGUS_SMTP_HOST
ARGUS_SMTP_PORT=465
ARGUS_SMTP_USER
ARGUS_SMTP_PASSWORD
ARGUS_ALERT_TO
ARGUS_EMAIL_SEND_ENABLED=1
```

未设置最后一项时只入本地 outbox。MVP 不支持飞书或微信发送；这些渠道只保留为后续适配器。

远程部署、令牌、同源限制、限流和审计要求见 `SECURITY_DEPLOYMENT.md`；模型与信号契约分别见 `MODEL_REGISTRY.md` 和 `SIGNALS_AND_NOTIFICATIONS.md`。
