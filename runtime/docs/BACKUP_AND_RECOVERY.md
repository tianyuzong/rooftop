# 备份与恢复

`market_intelligence.db` 是 Argus 的系统记录，包含行情索引、研究授权书、回测/版本记录、自定义模型、信号、通知订阅和审计日志。使用 SQLite 在线备份 API，不能在服务运行时直接复制数据库主文件。

## 创建备份

```powershell
.\scripts\backup_argus_data.ps1 -Keep 14
```

脚本输出到 `data_lake/backups`，为每个 `.db` 生成同名 JSON 元数据，记录来源、大小、SHA-256、表数量和 `PRAGMA quick_check`。`-Keep` 只清理该目录中匹配 `argus-backup-*.db` 的旧备份。

数据库备份不包含 `raw/` 原始响应、外部 Qwen/深度模型权重和 Python 环境。这些大文件应按数据许可和恢复时间目标使用独立存储快照；模型文件必须连同 SHA-256 清单备份。

## 恢复演练

1. 停止 Argus 服务和计划任务。
2. 核对备份 JSON 中的 SHA-256，并对备份运行 `PRAGMA quick_check`。
3. 保留当前数据库副本，再把已验证备份放入目标数据湖的 `db/market_intelligence.db`。
4. 启动本地回环服务，检查 `/api/health`、`/api/models`、`/api/signals` 和最近活动组合。
5. 确认数据截止日、活动模型版本和通知 outbox 后，再恢复盘后任务或远程入口。

至少每季度执行一次恢复演练。只有“成功恢复并通过校验”的备份，才算可用备份。
