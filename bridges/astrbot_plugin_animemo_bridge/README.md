# AniMemo Bridge

这是一个 provider-neutral 的 AstrBot 插件，使用 AniMemo Integration Protocol v1 的 HMAC、一次性配对、动作调用和 HTTP long-poll 事件投递。

## 快速开始

1. AniMemo 管理员执行 `python manage.py integration_connection create --provider astrbot --instance-id <stable-id> --name <name>`，保存一次性输出的 key id/secret。
2. 将本目录复制到 `AstrBot/data/plugins/astrbot_plugin_animemo_bridge/`，或使用仓库脚本导出 ZIP。
3. 在 AstrBot 配置中填写服务地址、key id、secret，并 reload 插件。
4. AniMemo 用户登录后生成 pairing code，在私聊发送 `/animemo pair CODE`。
5. 使用 `/animemo status` 与 `/animemo watch entries-search` 检查连接。

默认启用 TLS 校验、事件轮询和私聊投递；群聊业务命令关闭，配对永远只允许私聊。secret、配对码和 HMAC 签名不会写入日志或状态页。本插件不实现 `@filter.llm_tool`、WebSocket 或生产部署。

Marketplace publication = NOT YET PUBLISHED（本仓库为 monorepo，不伪造独立市场仓库）。

详见仓库中的 `docs/astrbot-bridge-v1.md`。
