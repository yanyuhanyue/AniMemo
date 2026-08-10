# AniMemo Bridge

这是一个 provider-neutral 的 AstrBot 插件，当前版本为 `0.1.3`，使用 AniMemo Integration Protocol v1 的 HMAC、一次性配对、动作调用和 HTTP long-poll 事件投递。

最低支持 AstrBot `4.27.2`。路由与事件状态保存在 AstrBot 官方 `data/plugin_data/astrbot_plugin_animemo_bridge/` 持久化目录，升级插件代码时不会写入或依赖 `data/plugins/` 安装目录。

插件配置 schema 使用 AstrBot 自己的类型系统：布尔值为 `bool`，整数为 `int`，文本为 `string`；不能使用 JSON Schema 风格的 `boolean`、`number` 或 `password`。AstrBot `4.27.2` 没有独立的 password 类型，`secret` 使用 `string` 并设置 `invisible: true`。Dashboard 会隐藏该字段，但 AstrBot 配置文件不是加密凭证库，因此生产环境优先使用 `ANIMEMO_INTEGRATION_SECRET` 注入 secret。

## 快速开始

1. AniMemo 管理员执行 `python manage.py integration_connection create --provider astrbot --instance-id <stable-id> --name <name>`，保存一次性输出的 key id/secret。
2. 将本目录复制到 `AstrBot/data/plugins/astrbot_plugin_animemo_bridge/`，或使用仓库脚本导出 ZIP。
3. 在 AstrBot 配置中填写服务地址、key id，并通过插件配置或优先使用 `ANIMEMO_INTEGRATION_SECRET` 提供 secret，然后 reload 插件。
4. AniMemo 用户登录后生成 pairing code，在私聊发送 `/animemo pair CODE`。
5. 使用 `/animemo status` 与 `/animemo watch find <关键词>` 检查连接。

默认启用 TLS 校验、事件轮询和私聊投递；群聊业务命令关闭，配对永远只允许私聊。`state.json` 继续以 UTC 保存机器可比对的时间戳，Diagnostics 页面按浏览器本地时区显示时间并动态标注 UTC offset；API 状态值保持稳定机器值，页面仅在展示层提供中文状态。secret、配对码和 HMAC 签名不会写入日志或状态页。Plugin Pages 管理 API 由 AstrBot Dashboard JWT / `plugin` scope 认证层保护，不提供匿名访问。本插件不实现 `@filter.llm_tool`、WebSocket 或生产部署。

Marketplace publication = NOT YET PUBLISHED（本仓库为 monorepo，不伪造独立市场仓库）。

详见仓库中的 `docs/astrbot-bridge-v1.md`。
