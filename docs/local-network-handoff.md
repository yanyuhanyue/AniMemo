# 外部服务接入与本地验收

2026-10-07 Windows 接手结果：F03/F04、F41–F45、F52 的代码、API、页面及配置入口已补齐。Bangumi 公共资料已真实验证；邮件送达、OAuth 正式授权和真实 R2 存储桶待专用凭据验收。旧项目本地配置和数据库中未找到可用的 Resend、OAuth client 或 R2 密钥。

| 能力 | 已实现及验证 | 待真实验证 |
| --- | --- | --- |
| 邮箱验证、找回密码 | Resend、加密持久 outbox、幂等投递、重试限流、单次令牌、密码与会话原子更新、保留两步验证；受控服务和浏览器通过 | 发信域名、key 和收件箱；API 接受不代表收到 |
| Bangumi 搜索、绑定、刷新 | 中文搜索、真实详情和封面、字段选择、去重和版本冲突、保留个人内容；真实 API、PostgreSQL 和浏览器通过 | 公开资料无需 OAuth，已可用 |
| OAuth 与收藏同步 | 会话绑定单次 state、加密 token、刷新和断开；持久分页预览、逐项确认、双边冲突、未知写入恢复；受控协议和浏览器通过 | OAuth client、准确回调、隔离收藏账号 |
| R2 图片存储 | 官方 AWS Go SDK SigV4、私有读取、上传回读校验、保留迁移原图、重试、旧图清理、校验清单、离线备份恢复；受控 S3、真实数据库和管理页面通过 | 专用私有 bucket、endpoint、对象读写 key |

宿主 Docker Desktop Linux engine 未就绪；新增容器实例运维流程仍需在可用 Docker 主机执行 npm run test:instance，不能沿用云端旧版本通过记录。

## 配置

将 [integrations.env.example](../deploy/integrations.env.example) 复制到忽略的 .local/integrations.env。同一服务的必需字段一起填写：

- RESEND_API_KEY、RESEND_FROM_EMAIL：Resend key 与已验证发信地址。
- BANGUMI_OAUTH_CLIENT_ID、BANGUMI_OAUTH_CLIENT_SECRET：OAuth 应用凭据。
- BANGUMI_OAUTH_REDIRECT_URI：必须等于 PUBLIC_ORIGIN/api/v1/connections/bangumi/callback；留空自动使用此地址。
- R2_ENDPOINT：https://<32 位 account id>.r2.cloudflarestorage.com。
- R2_BUCKET、R2_ACCESS_KEY_ID、R2_SECRET_ACCESS_KEY：专用私有 bucket 和最小对象读写权限 key。

继续使用原实例 ANIMEMO_SECRET_KEY（64 位十六进制，32 字节），保护两步验证、OAuth token 和邮件 outbox，不要随意更换。npm run dev 和 npm run containers 自动读取上述文件；直接运行二进制需传入同名环境变量。独立实例执行：

    npm run instance -- configure-external --name home --integrations-file .local/integrations.env

命令保存完整配置并重建应用，启动失败恢复原配置。配置和完整备份含密钥，保存在私有存储中。更换 R2 bucket 前先用原配置完成回迁及旧对象清理。远端图片缺少凭据且无保留原图时，应用拒绝不完整启动。

## 产品入口

- 加入番剧 → 从 Bangumi 搜索并填写；详情页刷新或解除绑定。话数使用 eps，不采用可能包含特别篇的 total_episodes。刷新保留评分、标签、短评和观看记录。
- 启用邮件后，注册只发验证链接，邮箱持有人通过链接设置密码；验证前不建立登录。令牌使用 URL fragment，页面读取后清除。找回密码仍要求已启用的 TOTP / 恢复码。
- 账号设置 → 外部账号与收藏同步（/connections）。单次最多 1000 部，逐项确认后才写入。双边修改默认跳过，不自动删除；评分取整在目标预览展示，新建外部收藏默认私密。
- 进度默认不同步；启用后根据明确的整数正片目录更新逐话状态，不生成观看日期；目录缺失、多义或超范围时拒绝进度写回。
- 管理 → 健康与维护 → 图片存储与迁移。先测试 R2 读写删除，再确认迁移。迁移前原图保留；之后新图先暂存，远端校验成功才释放临时副本。提供迁移进度、重试数量和 SHA-256 清单。
- 图片经过应用权限 API，不返回公开对象 URL 或长期签名链接。存储桶本身须保持私有；撤销分享后原应用链接拒绝读取。

## 备份与恢复

个人 ZIP 会读取 R2 封面；JSON / ZIP 导入保留 Bangumi 标识。完整实例 v2 备份含数据库、实例及外部密钥、媒体 ZIP 和校验清单；短暂停止应用确保同一时点。

新实例恢复会离线将图片写入 PostgreSQL，建立新对象目录，不清理源 bucket。旧会话、待发邮件、邮件令牌、OAuth 连接和未完成同步撤销，外部服务默认关闭。正常更新保留配置及任务，旧应用保持停止；显式回滚从完整备份新建数据库并取消外部任务。已经在外部服务完成的操作不会被本地回滚撤销。见 [实例运维](instance-operations.md)。

## 验收

常规命令：npm run check、npm test、npm run test:api、npm run build。PowerShell 设置 $env:ANIMEMO_LIVE_BANGUMI='1' 后运行 npm run test:api -- -run TestLiveBangumi，显式请求真实公共 API，并向隔离本地 schema 导入资料。

TestExternalBrowserFixture 仅在设置绝对输出目录 ANIMEMO_BROWSER_FIXTURE 和构建目录 ANIMEMO_BROWSER_WEB 后启用：随机 schema、回环 HTTP、合成邮件/OAuth/S3，匿名资料走真实 Bangumi。完成后创建输出目录的 stop 文件，夹具停止 worker 并清理 schema。不要用于部署。

凭据到位后还需核验邮箱实际收件、隔离账号 OAuth 与少量收藏写回、专用 bucket 的探测/迁移/回切/完整备份。通过前 F03/F04/F43–F45/F52 保持待真实服务验收。

协议依据：[Bangumi OpenAPI](https://github.com/bangumi/api/blob/master/open-api/v0.yaml)、[OAuth](https://github.com/bangumi/api/blob/master/docs-raw/How-to-Auth.md)、[Resend](https://resend.com/docs/api-reference/emails/send-email)、[R2 S3](https://developers.cloudflare.com/r2/api/s3/api/)、[R2 Go SDK](https://developers.cloudflare.com/r2/examples/aws/aws-sdk-go/)。

插件扩展、市场和 Bridge 是独立开发项，不属于本轮网络阻塞清单；见 [plugins.md](plugins.md)。
