# 外部服务接入与本地验收

> 2026-10-08：当前接手版本为 `1.3.0-rc.2`，新增记忆模型、简化记录入口和个人 v3 包见 [1.3 交付](v1.3-delivery.md)；交接包启动与继续开发见 [本地交接指南](local-package-handoff.md)。本轮云端没有注入 Resend/Bangumi OAuth/R2 正式凭据，网络策略仍有限制。后续在本地验证实际收件、OAuth 授权/收藏、R2 真读写，以及域名/TLS与正式签发；这些缺口不阻塞记忆库核心开发。私人记忆图片当前保存在 PostgreSQL，属于已经验收的本地持久化范围，不依赖 R2。


执行顺序以 [当前更新路线](refactor-next-steps.md) 为准。以下是跨环境接手清单：云端负责可运行的实现和受控验证；真实外部联调可在有网络与专用凭据的 Linux 或 Windows 上完成，不要求回到 Windows，也不等待 VM 或长时负载测试。

2026-10-07 Windows 接手结果：F03/F04、F41–F45、F52 的代码、API、页面及配置入口已补齐。Bangumi 公共资料已真实验证；邮件送达、OAuth 正式授权和真实 R2 存储桶待专用凭据验收。旧项目本地配置和数据库中未找到可用的 Resend、OAuth client 或 R2 密钥。

| 能力 | 已实现及验证 | 待真实验证 |
| --- | --- | --- |
| 邮箱验证、找回密码 | Resend、加密持久 outbox、幂等投递、重试限流、单次令牌、密码与会话原子更新、保留两步验证；受控服务和浏览器通过 | 发信域名、key 和收件箱；API 接受不代表收到 |
| Bangumi 搜索、绑定、刷新 | 中文搜索、真实详情和封面、字段选择、去重和版本冲突、保留个人内容；真实 API、PostgreSQL 和浏览器通过 | 公开资料无需 OAuth，已可用 |
| OAuth 与收藏同步 | 会话绑定单次 state、加密 token、刷新和断开；持久分页预览、逐项确认、双边冲突、未知写入恢复；受控协议和浏览器通过 | OAuth client、准确回调、隔离收藏账号 |
| R2 图片存储 | 官方 AWS Go SDK SigV4、私有读取、上传回读校验、保留迁移原图、重试、旧图清理、校验清单、离线备份恢复；受控 S3、真实数据库和管理页面通过 | 专用私有 bucket、endpoint、对象读写 key |

2026-10-07 的 Windows 本地接手当时没有可用 Docker Desktop Linux engine；这是历史环境限制。当前云端 Linux/Docker 已通过实例运维和恢复验收，具体代码/镜像及范围见 [验收记录](verification.md)。本地重新验证时，只报告实际运行结果，不把其他环境的通过记录算作本机验收。

2026-10-08 本机 RC2 接手已恢复 Docker Desktop Linux engine，并通过最终镜像安装、完整记忆库恢复、加密转移/更新/回滚、PostgreSQL 全量集成及桌面/手机浏览器检查。`local-review` 仅监听 `127.0.0.1:18082`，真实外部服务仍未配置。再次检查旧项目 `.env`、站点设置和 R2 配置表，没有找到可用的 Resend、OAuth 或 R2 密钥；不把字段存在当作凭据已配置。具体本机结果见 [验收记录](verification.md)。

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

- 记下看过的番 → 只填作品名即可保存；之后在详情页搜索并绑定 Bangumi、刷新或解除绑定，也可手动上传封面。话数使用 eps，不采用可能包含特别篇的 total_episodes。刷新保留评分、标签、短评和观看记录。
- 启用邮件后，注册只发验证链接，邮箱持有人通过链接设置密码；验证前不建立登录。令牌使用 URL fragment，页面读取后清除。找回密码仍要求已启用的 TOTP / 恢复码。
- 账号设置 → 外部账号与收藏同步（/connections）。单次最多 1000 部，逐项确认后才写入。双边修改默认跳过，不自动删除；评分取整在目标预览展示，新建外部收藏默认私密。
- 进度默认不同步；启用后根据明确的整数正片目录更新逐话状态，不生成观看日期；目录缺失、多义或超范围时拒绝进度写回。
- `recorded` 表示看过但未记录详细状态；Bangumi 没有对应状态，写回预览会提示不能映射，不猜成已看完。
- 管理 → 健康与维护 → 图片存储与迁移。先测试 R2 读写删除，再确认迁移。迁移前原图保留；之后新图先暂存，远端校验成功才释放临时副本。提供迁移进度、重试数量和 SHA-256 清单。
- 图片经过应用权限 API，不返回公开对象 URL 或长期签名链接。存储桶本身须保持私有；撤销分享后原应用链接拒绝读取。

## 备份与恢复

个人 ZIP 会读取 R2 封面；JSON / ZIP 导入保留 Bangumi 标识。完整实例 v3 备份含数据库、实例及外部密钥、媒体 ZIP、版本/迁移/扩展清单；短暂停止应用确保同一时点。跨主机用 age 加密转移，见 [实例操作](instance-operations.md)。

新实例恢复会离线将图片写入 PostgreSQL，建立新对象目录，不清理源 bucket。旧会话、待发邮件、邮件令牌、OAuth 连接和未完成同步撤销，外部服务默认关闭。正常更新保留配置及任务，旧应用保持停止；显式回滚从完整备份新建数据库并取消外部任务。已经在外部服务完成的操作不会被本地回滚撤销。见 [实例运维](instance-operations.md)。

## 验收

日常按当前路线选择受影响的检查，不要求顺序执行所有全量命令。真实 Bangumi 测试显式访问公共 API，并向隔离本地 schema 导入资料；只在有网络的接手环境执行：

```sh
# Linux / WSL2
ANIMEMO_LIVE_BANGUMI=1 npm run test:api -- -run TestLiveBangumi
```

```powershell
# Windows PowerShell
$env:ANIMEMO_LIVE_BANGUMI='1'
npm run test:api -- -run TestLiveBangumi
Remove-Item Env:ANIMEMO_LIVE_BANGUMI
```

TestExternalBrowserFixture 仅在设置绝对输出目录 ANIMEMO_BROWSER_FIXTURE 和构建目录 ANIMEMO_BROWSER_WEB 后启用：随机 schema、回环 HTTP、合成邮件/OAuth/S3，匿名资料走真实 Bangumi。完成后创建输出目录的 stop 文件，夹具停止 worker 并清理 schema。不要用于部署。

凭据到位后还需核验邮箱实际收件、隔离账号 OAuth 与少量收藏写回、专用 bucket 的探测/迁移/回切/完整备份。通过前 F03/F04/F43–F45/F52 保持待真实服务验收。

某项网络或凭据受限时，记录该项未验证的原因与接手步骤，并继续其他功能。受控测试通过不等于真实服务通过；未纳入本次发布承诺的外部集成默认关闭，不把它变成整个开发流程的阻塞条件。

协议依据：[Bangumi OpenAPI](https://github.com/bangumi/api/blob/master/open-api/v0.yaml)、[OAuth](https://github.com/bangumi/api/blob/master/docs-raw/How-to-Auth.md)、[Resend](https://resend.com/docs/api-reference/emails/send-email)、[R2 S3](https://developers.cloudflare.com/r2/api/s3/api/)、[R2 Go SDK](https://developers.cloudflare.com/r2/examples/aws/aws-sdk-go/)。

插件扩展、市场和 Bridge 是独立开发项，不属于本轮网络阻塞清单；见 [plugins.md](plugins.md)。

## 1.1 发行接手

云端候选构建与容器验证独立完成；GitHub CLI 发行授权未通过，所需 API/签名服务网络未完整开放，尚未远端签发或发布。提交、标签、来源证明、真实下载及启动的逐步说明见 [发行说明](release.md)，实施范围见 [1.1 交付](v1.1-delivery.md)。VM 和 25 分钟负载不作为接手前置。
