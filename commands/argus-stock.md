---
description: 运行 Argus A 股研究信号、邮件提醒、模型版本、股票对比或约束选股。
argument-hint: "[股票列表，或本金/期限/风险/板块/持仓数等条件]"
skills: stock-comparison-visualizer
---

使用 `stock-comparison-visualizer` 技能处理下面的 A 股研究请求：

$ARGUMENTS

严格按下面的模式路由：

1. 有 2-8 只明确股票并要求对比：调用 `scripts/start_stock_compare.ps1 -View compare -Stocks "..." -Profile ...`。
2. 有本金、期限、风险、板块、持仓数等条件：股票可以为空；调用 `scripts/start_stock_compare.ps1 -View harness`，然后把条件登记到 `/api/quant/mandates` 并返回 Harness 链接。只要有板块，就不得要求用户补股票。
3. 要求查看信号、提醒、模型，或命令没有附加参数：调用 `scripts/start_stock_compare.ps1 -View signals`，直接打开信号中心；不得要求用户补股票，不得创建新任务或启动即时回测。

结果仅供研究，不连接券商、不下单、不承诺收益。
