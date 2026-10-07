# API 合同与集成约定

源合同：`contracts/openapi.json`。运行中的同版本合同：`GET /api/v1/openapi.json`。`npm run contracts` 同时更新 TypeScript 类型和 Go 镜像内嵌合同；`npm run check:web` 会检查两者没有过期。所有模型以合同为准，未知 JSON 字段会被拒绝。

## 认证与错误

- 浏览器同源访问。登录使用 `animemo_session` HttpOnly / SameSite=Lax Cookie，HTTPS 开启 Secure。不要在 localStorage 保存凭据。
- 写操作（包括登录、初始化）必须发送与 `PUBLIC_ORIGIN` 完全一致的 `Origin`。这项检查不等于授权，私有接口还会检查登录、归属或管理员身份。
- 启用两步验证的账号登录时需传 `code`；可使用动态验证码或单次恢复码。注册邮箱验证和邮件找回密码尚未提供。
- 错误结构：`{"error":{"code":"version_conflict","message":"…","fields":{…}}}`，`fields` 可能省略。用户界面显示 `message`，程序按 `code` 处理，不解析中文文字。
- 400 输入无效；401 未登录或凭据无效；403 来源 / 权限 / 注册开关拒绝；404 不存在或无权读取；409 版本、幂等或工作流冲突；413 文件或容量超限；415 媒体类型错误；429 认证尝试过多；503 服务未配置或资源忙。
- 身份、权限、图片和公开查询均为 `private, no-store`。不要为这些路径加 CDN 强制缓存，否则撤回分享不能及时生效。

## 分组与流程

| 场景 | 入口 | 关键约定 |
| --- | --- | --- |
| 账号 | `/auth/*`、`/settings`、`/avatar` | 当前会话鉴权；修改密码和两步验证会撤销其他会话 |
| 私人手账 | `/entries`、`/history/page`、`/analytics` | 服务器从会话确定 owner；编辑和删除携带读取到的 version |
| 观看 | `/entries/{id}/history` | POST 使用新的 request_id；相同 ID 与相同内容重试不重复计入 |
| 批量管理 | `/entries/bulk` | 最多 100 项，所有版本 / 归属通过后才整体提交 |
| 导入 | `/imports`、`/imports/{id}` | 原始文件上传，先 durable validation，再 apply；预览后手账变化使整批失败 |
| 个人备份 | `/export`、`/backup` | JSON 无图片；ZIP 包含番剧、观看记录、原图及校验清单，不是实例备份 |
| 公开 | `/public/showcases`、`/public/catalog`、`/public/shared/{slug}` | 无需登录；字段经过投影，不包含邮箱、私人 ID、逐次观看历史 |
| 专栏 | `/columns`、`/public/columns` | 草稿提交审核；修改已发布专栏使其退回草稿；撤回立即取消公开 |
| 管理 | `/admin/*` | 每次请求检查当前角色；管理修改在事务内再次鉴权并写入审计 |
| 首次初始化 | `/setup` | 仅无管理员且部署者配置随机口令时开放，不会把第一位普通注册者自动提升为管理员 |

以上表格中的相对路径前缀均为 `/api/v1`。服务健康检查为 `/api/health` 和 `/api/ready`。

## 分页、数据大小和文件

私人条目默认 12 / 页，公开目录与公开内容 12 / 页，历史 50 / 页，管理资源 20 / 页，审计 30 / 页。使用返回的 total、page、page_size（适用时），不要假设一次请求返回全部内容。

JSON 请求最多 128 KiB。标题、短评、正文等字段还有各自字符限制。图片为 JPEG / PNG 原始字节，每张最多 2 MiB、最长边 8192、总像素 1200 万；图片修订地址在替换后失效。原图可能含 EXIF，不进行元数据清除。

导入格式为新项目自有 `animemo.journal/v1` JSON、UTF-8 CSV 或 `animemo.backup/v1` ZIP。无需兼容旧版导出。CSV 最多 2 MiB / 500 部，JSON 最多 64 MiB，ZIP 最多 160 MiB。每账号最多一个活动任务，预览 24 小时有效。新导入条目统一为私密，需要用户重新开启可见性。

## 两步验证与恢复

部署必须设置持久的 `ANIMEMO_SECRET_KEY`（64 个十六进制字符，即 32 字节）。验证器密钥使用 AES-256-GCM，绑定账号 ID；恢复码仅保存 SHA-256。验证码使用标准 TOTP / SHA-1、6 位、30 秒，允许前后一个时间窗口，已消费的计数器不得重放。

密钥配置缺失或不匹配时返回 503，不会绕过两步验证。备份数据库时必须保留同一个实例密钥；运维备份命令会一并保存。验证器设置 10 分钟到期，恢复码仅在生成响应显示一次。不要将这些响应、Cookie 或初始化口令放进日志和截图。

## 开发验收

```sh
npm run contracts
npm run verify
npm run test:containers
```

跨账号隔离、并发版本、幂等、导入回滚、公开撤回、权限变更、两步验证与真实数据库测试在 `server/internal/api/*integration_test.go`。静态页面与 API 兜底路由同时启用的场景由 `static_test.go` 覆盖，防止仅 API 测试漏掉容器启动路由冲突。

## 受限导入插件

`GET /api/v1/plugins` 列出已启用转换器；`POST /api/v1/plugins/{slug}/imports?filename=...` 接收最多 2 MiB UTF-8 原始文本，运行沙箱后返回 202 和普通 `ImportJob`。后续确认 / 取消仍使用已有导入接口。失败插件不写手账，运行错误为 422 `plugin_failed`，运行期间版本变化为 409，未启用为 404，容量繁忙为 503。

管理员使用 `GET/POST /api/v1/admin/plugins` 查看或安装 JSON/base64 包（原始请求体，最多 12 MiB），`POST /api/v1/admin/plugins/{slug}` 携带 `action:activate|disable`、版本和 revision。安装返回 201 `installed:true`，幂等重传也返回 201；启停返回 200 `updated:true`。清单和 stdout 协议定义在 `pkg/pluginproto`；包不会返回给普通用户。详细状态语义见 `plugins.md`。

`ImportPreview.history` 是最多 100 条即将新增的观看记录明细，包含标题、观看日期、话数、刷次和笔记；不是全部记录，也不包含因同名而跳过的番剧。

## 外部服务

`GET /api/v1/auth/options` 返回是否启用邮件。`POST /api/v1/auth/email/request` 统一返回 202；验证和重置分别使用 `/api/v1/auth/email/verify`、`/api/v1/auth/password/reset`，需要 token、新密码以及启用两步验证时的 code。启用邮件后的注册返回 202 且不创建会话；未启用时保持原注册流程。

`GET /api/v1/providers/bangumi/subjects?query=...&page=...` 搜索；`GET /subjects/{subject}` 和 `/subjects/{subject}/cover` 位于相同 provider 前缀。`POST /api/v1/entries/from-bangumi` 创建私密条目；已有条目 `/api/v1/entries/{id}/source` 支持 POST 字段刷新和 DELETE 解绑，均检查版本，刷新还检查预览 snapshot。

`/api/v1/connections/bangumi` 支持 GET 状态、DELETE 断开；POST `/authorize`、`/verify` 和 GET `/callback` 完成 OAuth。`/sync` 支持 GET 历史和 POST 创建预览；`/sync/{id}` 支持 GET 结果和 POST apply/cancel。token 从不下发浏览器；每项选择只接受预览允许的方向。

管理员 `/api/v1/admin/media/storage` 支持 GET 状态、PUT 后端与版本；POST `/probe` 写入、回读并删除测试图；GET `/api/v1/admin/media/migrations/{id}` 下载校验清单。全部复用管理员权限与同源写入检查。详细字段以生成的 OpenAPI 为准。
