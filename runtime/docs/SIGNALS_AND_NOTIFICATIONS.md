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

MVP 支持显式创建 `EMAIL` 订阅，可选择 `BUY / SELL / REBALANCE / HOLD / WATCH` 和最低置信度。默认只订阅三类需要动作复核的信号。

邮件先写入 `alert_outbox`，使用信号与订阅组成的去重键。发送失败按退避时间重试，最多 5 次；邮件故障不会把已完成的回测或组合发布改成失败。飞书和微信仅保留为后续适配器，不宣称当前可用。

相关接口：

- `GET /api/signals`
- `POST /api/signals/{id}/acknowledge`
- `POST /api/signals/{id}/dismiss`
- `GET/POST /api/notification-subscriptions`
- `POST /api/notification-subscriptions/{id}/enable|disable`
- `GET /api/notifications`
- `POST /api/notifications/test`
- `POST /api/notifications/send`

SMTP 凭证只从进程环境读取，不写入 SQLite。`ARGUS_EMAIL_SEND_ENABLED` 未设为 `1` 时，邮件保留在 outbox，不会外发。
