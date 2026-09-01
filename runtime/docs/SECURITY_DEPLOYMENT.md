# 部署与安全

Argus 默认只监听 `127.0.0.1`。外部试用应采用“反向代理 TLS + API 令牌 + 防火墙来源白名单”，不要把开发服务直接暴露到公网。

## 最低要求

1. 使用随机高强度令牌文件，并通过 `-TokenFile` 启动。非回环监听没有令牌会直接拒绝启动。
2. 使用 Caddy、Nginx、IIS 或同类反向代理终止 HTTPS；Argus 后端仍监听内网地址。
3. 防火墙只允许明确的办公/VPN 地址，禁止对全网开放数据库、数据湖和 Python 环境。
4. 设置 `ARGUS_ALLOWED_HOSTS` 与 `ARGUS_ALLOWED_ORIGINS`；多个值用逗号分隔。
5. 保持默认每客户端每分钟 180 次 API 限流，按实际刷新频率调整。
6. 定期备份并验证 SQLite，保留管理员、运行账户和恢复责任人。

示例：

```powershell
.\scripts\start_stock_compare.ps1 -View signals `
  -BindAddress 0.0.0.0 -PublicHost argus.example.edu -PublicScheme https `
  -TokenFile D:\ArgusSecrets\remote.token `
  -AllowedHosts "argus.example.edu,127.0.0.1" `
  -AllowedOrigins "https://argus.example.edu"
```

浏览器只把令牌保存在当前标签会话的 `sessionStorage`，不会把令牌写入 URL、SQLite 或本地持久配置。所有变更 API 以及鉴权、Host、Origin、限流拒绝会进入 `api_audit_log`；日志不记录令牌或请求正文。

## 数据隔离

- 外部用户只访问 Web/API，不共享插件源码目录、数据湖目录或远程桌面。
- SQLite 不存券商凭据、邮箱密码或 API Key JSON。
- SMTP、社交平台等凭证从服务账户环境变量注入，并按最小权限轮换。
- 核心模型可保留在后端；对外导出的报告只包含必要结果和来源，不包含凭证或私有原始材料。

当前令牌是共享服务令牌，不是多租户身份系统。需要不同用户权限、数据隔离、撤销和逐人审计时，应先增加正式身份代理或 SSO，不能把共享令牌包装成多租户生产服务。
