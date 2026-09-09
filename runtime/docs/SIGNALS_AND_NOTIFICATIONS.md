# 信号与通知

信号中心把盘后研究结果转成可复核记录，不把研究建议转成订单。

## 生成规则

- 新 `ACTIVE` 组合版本首次纳入股票时生成 `BUY`。
- 已有研究仓位的参考股数或权重变化时生成 `REBALANCE`，无实质变化时生成 `HOLD`。
- 股票从新 `ACTIVE` 版本退出时生成 `SELL`。
- `SNAPSHOT`、`REJECTED` 或未过正式门禁的研究候选只生成 `WATCH`。
- 同一授权书和版本只发布一次；新信号会把对应旧状态标为 `SUPERSEDED`。

每条记录保存数据截止日、模型/组合版本、验证状态、参考价、参考数量、权重、止盈止损参考、置信度、原因、证据摘要和失效条件。参考数量不是订单数量。

## 人工复核

信号初始为 `NEW`，用户可以标记为 `ACKNOWLEDGED` 或 `DISMISSED`。系统不提供“已成交”状态，也没有券商账户、订单或自动执行接口。

## 邮件订阅

支持显式创建或编辑 `EMAIL` 订阅。日报内容与研究信号分别选择，可只收日报而不订阅任何交易方向信号。

| 设置 | 用途 |
|---|---|
| `digest_kinds` | `MARKET` 昨日大盘、`COMPANIES` 关注公司，可多选 |
| `watch_stocks` / `watch_symbols` | 名称或六位股票代码，最多 20 只；关注名单不写入持仓 |
| `send_time` | 北京时间 `HH:MM`，默认 `08:30` |
| `event_kinds` | 可选 `BUY / SELL / REBALANCE / HOLD / WATCH`；允许空列表 |
| `minimum_confidence` | 仅用于研究信号的最低置信度过滤 |
| `target`、`enabled` | 收件邮箱与订阅启用状态 |

日报使用前一个北京时间自然日的本地报价、日线、已公告财报和相关报道。缺少当天行情时标注缺失，不以演示、未来日期或其他交易日的行情替代。预览和手动发送可指定历史报告日期。

邮件先写入 `alert_outbox`：研究信号使用信号与订阅组成的去重键，日报使用 `digest:{subscription_id}:{report_date}`。同一订阅同一日期不重复发送；更新内容会取消该订阅尚未发送的旧邮件，停用订阅也会取消未发送邮件。发送失败按退避时间重试，最多 5 次；邮件故障不会把已完成的回测或组合发布改成失败。

计划时间过后，后台线程尝试排队昨日简报。服务必须保持运行；重新启动只会自动检查当时的昨日简报，更早漏发日期通过页面选择。飞书和微信尚未实现，不宣称当前可用。

相关接口：

- `GET /api/signals`
- `POST /api/signals/{id}/acknowledge`
- `POST /api/signals/{id}/dismiss`
- `GET/POST /api/notification-subscriptions`
- `POST /api/notification-subscriptions/{id}/enable|disable`
- `GET /api/notifications`
- `POST /api/notifications/test`
- `POST /api/notifications/send`
- `GET/POST /api/email-settings`
- `POST /api/email-settings/verify`
- `POST /api/digests/preview`
- `POST /api/digests/send`
- `POST /api/notifications/{id}/retry`
- `POST /api/notifications/{id}/received`

## 发件配置与收件验证

页面可配置 SMTP 主机、端口、登录邮箱、授权码以及 SSL / STARTTLS。设置写入数据湖的 `secrets/email_settings.json`；授权码经 Windows DPAPI 加密，接口只返回是否已配置，不回传授权码。密文依赖当前 Windows 账户，换机器或账户后需重新配置。

页面保存的设置优先于对应进程环境。未保存页面设置时可使用 `ARGUS_SMTP_*` 环境变量，并以 `ARGUS_EMAIL_SEND_ENABLED=1` 启用外发。默认不会发送。

| 状态 | 含义 |
|---|---|
| SMTP `CONNECTED` | 登录与连通性验证成功，尚不能证明收件 |
| `PENDING` | 等待发送 |
| `RETRY` / `FAILED` | 自动重试中或失败；保留错误原因并支持手动重试 |
| `SENT` | SMTP 服务器接受邮件，不保证已进入收件箱 |
| `received_at` 有值 | 用户实际看到邮件后，主动点击“我已收到” |
| `CANCELLED` | 未发出的消息因订阅更新或停用而取消 |

页面先验证连接，再通过测试邮件或日报验证真实投递。用户确认收件单独记录，不把 SMTP 接受当成真实收件。收件服务商可能延迟、拒收或放入垃圾邮件，因此不承诺必达。
