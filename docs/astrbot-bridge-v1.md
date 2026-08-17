# AstrBot Bridge v1

## AstrBot API baseline

本实现已对 AstrBot 官方仓库的 `v4.27.2` (`ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf`) 与固定 master 快照 (`30e20318cbaaa2e1ba57f3e0eee265d9ee98115c`) 执行真实 runtime smoke。`@register`、`Star`、`Context`、`StarTools` 来自 `astrbot.api.star`；`@filter.command`、`AstrMessageEvent`、`MessageChain` 来自 `astrbot.api.event`；主动投递使用 `await context.send_message(umo, MessageChain().message(text))`；持久化目录由 `StarTools.get_data_dir("astrbot_plugin_animemo_bridge")` 创建；diagnostics 使用 `context.register_web_api(route, handler, methods, desc)` 与 Plugin Pages 的 `window.AstrBotPluginPage.ready()/apiGet()/apiPost()`。Bridge 不使用已标记 deprecated 的 `Context.register_task`，而在 `initialize()` 中创建唯一 asyncio poller，在 `terminate()` 中取消它。详细官方源码证据见 `docs/astrbot-runtime-compatibility-audit.md`。

Bridge `0.1.3` 的 CI 还会把源码和最终 ZIP 分别交给真实 AstrBot `AstrBotConfig` schema parser 与 `PluginManager.load()`。AstrBot `4.27.2` 支持 `int`、`float`、`bool`、`string`、`text`、`list`、`file`、`object`、`template_list` 和 `dict`；这些是 AstrBot 配置类型，不是 JSON Schema 类型名。门禁会稳定拒绝旧版使用的 `boolean`、`number` 和 `password`。

## Architecture

Bridge 是独立可导出的 `astrbot_plugin_animemo_bridge`，由 `httpx.AsyncClient`、HMAC v1 signing、一次性 pairing、identity binding、action client、event long-poll、bounded local dedup 和 AstrBot delivery glue 组成。Bridge 不使用 AniMemo JWT、Cookie 或数据库，也不实现 WebSocket、LLM Tool、MQ 或 Bangumi OAuth。

## Provision connection

在 AniMemo 管理端执行：

```text
python manage.py integration_connection create --provider astrbot --instance-id <stable-instance-id> --name <name>
```

输出 connection id、key id 与只显示一次的 secret。secret 不写入仓库、日志或状态页；轮换使用 `rotate-secret <connection-id>` 后更新 AstrBot 配置并 reload。

## Configure and install

从仓库根目录执行 `python scripts/package-astrbot-bridge.py`，将输出 ZIP 安装到 AstrBot。填写 `animemo_base_url`、`key_id`、`secret`；也支持环境变量 `ANIMEMO_BASE_URL`、`ANIMEMO_INTEGRATION_KEY_ID`、`ANIMEMO_INTEGRATION_SECRET` 覆盖配置。`animemo_base_url` 是 canonical HTTPS 服务源，只允许 scheme、host 与可选 port；禁止 URL userinfo、非根路径、查询参数和片段。TLS 证书验证强制使用系统信任存储，不提供关闭选项。默认 `poll_events=true`、`poll_wait_seconds=20`、`request_timeout_seconds=35`、`allow_group_commands=false`。

AstrBot `4.27.2` 没有独立的 `password` schema 类型。Bridge 将 `secret` 声明为受支持的 `string` 并设置 `invisible: true`，Dashboard 会隐藏该字段，但配置文件本身不是加密凭证库。生产环境优先使用 `ANIMEMO_INTEGRATION_SECRET`，并继续避免把 secret 写入仓库、日志、状态页、`routes.json` 或 `state.json`。运行时会自行解析布尔值并校验 `0 <= poll_wait_seconds <= 25`、`5 <= request_timeout_seconds <= 120` 以及 `request_timeout_seconds > poll_wait_seconds`，不把 Dashboard 的 `min/max` 元数据当作唯一安全边界。

## Pairing and commands

AniMemo 登录用户生成 pairing code，然后在 AstrBot 私聊发送 `/animemo pair CODE`。群聊配对始终拒绝且不会回显 code。常用命令：`/animemo help`、`status`、`ping`、`watch get <entry_id>`、`watch add <entry_id> <date> [episode]`、`watch find <query>`、`unpair-help`。群聊动作只有显式开启 `allow_group_commands` 后才允许；主动事件 v1 仍只投递到已保存的私聊 UMO。任意 action JSON 调试命令默认关闭，开启后仍要求 AstrBot 管理员身份。

## Event delivery and security

事件使用 HTTP long-poll。Bridge 成功发送后先落盘 event id，再 ACK；ACK 失败时重放只重试 ACK，不重复发送。无私聊 route 不 ACK，pending event 会阻止 cursor 越过更早的未处理事件。`routes.json` 与 `state.json` 位于 AstrBot `data/plugin_data/astrbot_plugin_animemo_bridge/`，使用 temp + fsync + `os.replace` 原子写入；损坏文件会备份后 fail safe。普通日志只记录错误类型，状态页只显示 route count、平台和外部 ID 哈希片段。

Diagnostics 页面位于 `pages/status/`，可查看脱敏状态、测试 HMAC 连接、重启轮询器，并在确认后清除单条脱敏私聊路由。Bridge 内部与 `state.json` 继续使用 UTC，页面按浏览器本地时区显示时间并动态标注 UTC offset；API 保持英文机器状态值，普通界面在展示层提供中文标签和状态。AstrBot 现代 dispatcher 对这些路由强制 `plugin` scope：Dashboard JWT 或具有该 scope 的 API key 可访问，匿名请求不可访问。页面只使用 AstrBot Plugin Pages SDK，不读取 Dashboard cookie、localStorage、secret、签名或原始事件 payload。

## Troubleshooting

- `BridgeAuthError`：检查 key id、secret、服务器时间和 TLS。
- 配对结果未知：在 AniMemo 绑定页确认；若没有成功，生成新的 code，不要重复消费同一个 code。
- 长轮询停止：查看 `/animemo status`，确认 `poll_events` 和凭证配置。
- 事件无投递：重新私聊发送任意 `/animemo` 命令刷新私聊 UMO。

## Multiple instances and lifecycle

每个 AstrBot 实例使用独立 IntegrationConnection、key 和本地数据目录；相同 external user id 不会跨 connection 串线。插件 reload/terminate 会取消旧 poller、关闭 `httpx.AsyncClient`、保存状态，不留下重复任务。

## Upgrade and uninstall

升级只替换插件目录，官方 plugin data directory 中的 route/state 保留。卸载前可备份数据目录；AniMemo 侧另行 revoke/disable connection 和 identity binding。Marketplace publication = NOT YET PUBLISHED。
