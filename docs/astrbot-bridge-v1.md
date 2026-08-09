# AstrBot Bridge v1

## AstrBot API baseline

本实现按 AstrBot 官方仓库 `AstrBotDevs/AstrBot` 当前 `master` API 审计（截至 2026-08-09，最新 release `v4.27.2`）：`@register`、`Star`、`Context` 来自 `astrbot.api.star`；`@filter.command` 与 `AstrMessageEvent` 来自 `astrbot.api.event`；`MessageChain` 优先来自 `astrbot.api.message`，并保留旧导入路径兼容；事件使用 `get_sender_id()`、`get_sender_name()`、`get_platform_id()`、`get_message_type()`、`is_private_chat()` 和 `unified_msg_origin`；主动投递使用 `await context.send_message(umo, MessageChain().message(text))`；diagnostics 使用 `context.register_web_api(route, handler, methods, desc)` 与 Plugin Pages 的 `window.AstrBotPluginPage.ready()/apiGet()/apiPost()`。Bridge 不使用已标记 deprecated 的 `Context.register_task`，而在 `initialize()` 中创建唯一 asyncio poller，在 `terminate()` 中取消它。

## Architecture

Bridge 是独立可导出的 `astrbot_plugin_animemo_bridge`，由 `httpx.AsyncClient`、HMAC v1 signing、一次性 pairing、identity binding、action client、event long-poll、bounded local dedup 和 AstrBot delivery glue 组成。Bridge 不使用 AniMemo JWT、Cookie 或数据库，也不实现 WebSocket、LLM Tool、MQ 或 Bangumi OAuth。

## Provision connection

在 AniMemo 管理端执行：

```text
python manage.py integration_connection create --provider astrbot --instance-id <stable-instance-id> --name <name>
```

输出 connection id、key id 与只显示一次的 secret。secret 不写入仓库、日志或状态页；轮换使用 `rotate-secret <connection-id>` 后更新 AstrBot 配置并 reload。

## Configure and install

从仓库根目录执行 `python scripts/package-astrbot-bridge.py`，将输出 ZIP 安装到 AstrBot。填写 `animemo_base_url`、`key_id`、`secret`；也支持环境变量 `ANIMEMO_BASE_URL`、`ANIMEMO_INTEGRATION_KEY_ID`、`ANIMEMO_INTEGRATION_SECRET` 覆盖配置。默认 `verify_tls=true`、`poll_events=true`、`poll_wait_seconds=20`、`request_timeout_seconds=35`、`allow_group_commands=false`。

## Pairing and commands

AniMemo 登录用户生成 pairing code，然后在 AstrBot 私聊发送 `/animemo pair CODE`。群聊配对始终拒绝且不会回显 code。常用命令：`/animemo help`、`status`、`ping`、`watch get <entry_id>`、`watch add <entry_id> <date> [episode]`、`watch find <query>`、`unpair-help`。群聊动作只有显式开启 `allow_group_commands` 后才允许；主动事件 v1 仍只投递到已保存的私聊 UMO。任意 action JSON 调试命令默认关闭，开启后仍要求 AstrBot 管理员身份。

## Event delivery and security

事件使用 HTTP long-poll。Bridge 成功发送后先落盘 event id，再 ACK；ACK 失败时重放只重试 ACK，不重复发送。无私聊 route 不 ACK，pending event 会阻止 cursor 越过更早的未处理事件。路由文件和事件状态文件使用 temp + fsync + `os.replace` 原子写入；损坏路由会备份后 fail safe。普通日志只记录错误类型，状态页只显示 route count、平台和外部 ID 哈希片段。

Diagnostics 页面位于 `pages/status/`，可查看脱敏状态、测试 HMAC 连接、重启轮询器，并在确认后清除单条脱敏私聊路由。页面只使用 AstrBot Plugin Pages SDK，不读取 Dashboard cookie、localStorage、secret、签名或原始事件 payload。

## Troubleshooting

- `BridgeAuthError`：检查 key id、secret、服务器时间和 TLS。
- 配对结果未知：在 AniMemo 绑定页确认；若没有成功，生成新的 code，不要重复消费同一个 code。
- 长轮询停止：查看 `/animemo status`，确认 `poll_events` 和凭证配置。
- 事件无投递：重新私聊发送任意 `/animemo` 命令刷新私聊 UMO。

## Multiple instances and lifecycle

每个 AstrBot 实例使用独立 IntegrationConnection、key 和本地数据目录；相同 external user id 不会跨 connection 串线。插件 reload/terminate 会取消旧 poller、关闭 `httpx.AsyncClient`、保存状态，不留下重复任务。

## Upgrade and uninstall

升级只替换插件目录，官方 plugin data directory 中的 route/state 保留。卸载前可备份数据目录；AniMemo 侧另行 revoke/disable connection 和 identity binding。Marketplace publication = NOT YET PUBLISHED。
