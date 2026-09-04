# B站、X、SEC 本地账号与采集配置

更新日期：2026-08-12。系统只做研究与公开信息归档，不执行关注、点赞、评论、转发、发帖或交易。

## 绝对不要通过聊天提供的内容

不要在聊天、Issue、截图或项目文件中发送/填写以下内容：平台密码、短信验证码、Cookie、`auth_token`、`ct0`、Bearer Token、浏览器 Profile 目录。本项目不读取存放 API Key 的 JSON 文件。

## 1. B站：先试公开元数据，再按需使用本机登录态

首选开源实现为 `yt-dlp`，仅读取视频/频道/搜索结果的元数据，不下载音视频。系统把原始 JSON 存入 `data_lake/raw/documents/bilibili`，规范化条目存入 `source_documents` 和版本表。

无需账号的测试目标示例：视频或 UP 主频道 URL，或 `bilisearch20:AI 硬件`。如果公开请求不够：

1. 你自己在 Chrome、Edge 或 Firefox 正常登录 B站；
2. 关闭无关账号页面，确认使用的是愿意用于研究的账号；
3. 在启动 `run.ps1` 的同一个 PowerShell 窗口设置浏览器类型，例如 `$env:ARGUS_BILIBILI_BROWSER='chrome'`；
4. 采集请求显式选择 `use_browser=true` 后，`yt-dlp` 在本机读取该浏览器的会话。密码不会交给本系统，Cookie 也不会写入金融数据库。

优先使用专门的研究账号，低频、只读、只抓公开页面。若平台出现验证码、访问限制或条款不允许，应停止，不做绕过。

## 2. X：账号不等于免费 API

X 官方读取 API 当前按量付费，因此在“零付费数据接口”约束下默认关闭。即使已有普通 X 账号，也仍需在 X Developer Console 创建项目、App，取得 API 凭证并购买读取额度。

若以后接受官方费用，只把 Bearer Token 放入启动进程的本机环境变量：

```powershell
$env:ARGUS_X_BEARER_TOKEN='在你本机粘贴，不要发给任何人'
.\run.ps1
```

项目不会将 Token 写入 SQLite、日志或原始数据文件。由于 X 官方开发者指引明确禁止爬虫和浏览器自动化，本项目不集成 `twscrape`、`snscrape` 等绕过官方 API 的方案；它们不能作为可靠后备源。

## 3. SEC EDGAR：无需账号、无需 API Key

SEC 只要求自动程序提供可识别的 User-Agent。请在本机设置一个项目名和可联系邮箱：

```powershell
$env:ARGUS_SEC_IDENTITY='RooftopResearch your-email@example.com'
.\run.ps1
```

邮箱不是密码，可使用专门的研究联系邮箱。系统访问 `data.sec.gov/submissions/CIK##########.json`，并建议限制到每秒不超过 2 次，低于 SEC 公布的每秒 10 次公平访问上限。每次查询先保存原始 JSON，再把申报索引落入传统数据库。

## 本地接口

- `GET /api/intelligence-sources`：查看三个信息源是否已配置，不返回任何凭证。
- `POST /api/collect/bilibili`：`{"target":"视频/频道URL","use_browser":false}`。
- `POST /api/collect/x`：`{"query":"AI hardware","max_results":10}`，只有配置官方付费 API 后可用。
- `POST /api/collect/sec`：`{"cik":"0000320193"}`。

所有采集都是显式触发；当前不会后台自动抓取，也不会访问私信、收藏、历史记录或非公开内容。
