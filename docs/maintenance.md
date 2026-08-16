# Maintenance

维护任务统一通过 Django management command 执行。每个任务独立报告 `PASS`/`FAIL`，某个任务失败不会静默吞掉错误；总命令在任一任务失败时返回非零状态。

```bash
python backend/manage.py run_maintenance
python backend/manage.py run_maintenance --task purge_expired_revoked_tokens
```

当前入口只包含非破坏性任务：过期注册、撤销 token、外部账号会话、Integration 事件与已完成动作回执清理、Watch History Importer 过期批次清理、媒体使用量刷新、过期媒体写入 reservation 对账，以及只读孤立媒体审计。Integration 默认清理超过 1 天的已 ACK 事件、超过 7 天的未 ACK 事件，以及超过 7 天的 completed/failed 动作回执；pending 回执不会被维护任务删除。Importer 批次默认保留 7 天。`reconcile_media_write_reservations` 只把过期 pending 标记为 `abandoned`，不删除本地或远程对象。`audit_orphan_media` 默认只列出本地文件，删除必须显式传入它自己的 `--delete` 参数，不会由总入口触发，也不枚举 R2。

## Abandoned media reservation operations

`MediaWriteReservation` 保存 backend、object key、size、content type 与 SHA-256，供崩溃后人工核对。`abandoned` 只表示数据库 finalize 未完成或写入失败，不能单独证明远程对象一定存在，更不能证明它无人引用。

人工处置顺序：

1. 先运行 `reconcile_media_write_reservations`，记录 pending/finalized/abandoned 数量，不执行删除。
2. 对单个 abandoned reservation 核对其 backend、精确 object key、时间、size 和 SHA；同时查询 `MediaObject` 与所有稳定 `media-objects/<uuid>` 引用。
3. 若远端对象存在，下载或读取 metadata 后校验 size/SHA，并确认没有对应 `MediaObject`、没有业务引用、没有仍有效的 pending reservation。
4. 只有 operator 对这个精确 key 建立书面证据并显式批准时，才可使用该 backend 的受限工具删除该单一对象；删除后记录 audit evidence 并再次确认读取路径不受影响。
5. 任一步存在歧义就保留对象并升级为人工调查。未知 remote object 永不因为“不在当前数据库”而自动删除。

定时任务、总维护入口、Updater、migration 与 production acceptance 都不得自动删除 R2/远程孤儿对象。

## Query evidence

代表性手账列表查询可以在开发数据库或 CI 数据库中生成真实计划：

```bash
python backend/manage.py profile_journal_queries --username <用户名> --format json
# 或：python backend/manage.py profile_journal_queries --user-id <用户 ID> --format json
```

命令必须明确指定一个用户，支持 SQLite 和 PostgreSQL，默认最多分析 24 行，限制上限为 500；它只输出 `EXPLAIN`，不会输出用户名、邮箱、评论、令牌或凭据，也不会自动新增索引。只有当计划和基准证明需要时，才允许新增 migration/index。

## Scheduling

生产调度器可以使用宿主机的 cron 或 systemd timer，通过运行中的 AniMemo API 容器调用 `run_maintenance`，但不需要常驻 worker。canonical 工作目录为 `/opt/animemo`；命令必须使用固定项目、exact Compose 材料和由托管配置派生的 `/run/animemo-updater/managed.env`：

```cron
17 * * * * cd /opt/animemo && /usr/bin/docker compose --project-name animemo --env-file /run/animemo-updater/managed.env -f /opt/animemo/deploy/docker-compose.yml -f /opt/animemo/updater/docker-compose.runtime.yml exec -T api python manage.py run_maintenance >> /var/log/animemo-maintenance.log 2>&1
```

systemd service/timer 也应使用同一个命令，并让失败状态进入宿主机日志和告警系统；本项目不引入 Celery、Celery Beat、RabbitMQ、Kafka 或新的后台 worker。

一个最小的 systemd 配置示例（按实际部署路径和用户调整）：

`/etc/systemd/system/animemo-maintenance.service`

```ini
[Unit]
Description=AniMemo maintenance tasks

[Service]
Type=oneshot
User=root
WorkingDirectory=/opt/animemo
ExecStart=/usr/bin/docker compose --project-name animemo --env-file /run/animemo-updater/managed.env -f /opt/animemo/deploy/docker-compose.yml -f /opt/animemo/updater/docker-compose.runtime.yml exec -T api python manage.py run_maintenance
```

`/etc/systemd/system/animemo-maintenance.timer`

```ini
[Unit]
Description=Run AniMemo maintenance hourly

[Timer]
OnCalendar=hourly
Persistent=true
Unit=animemo-maintenance.service

[Install]
WantedBy=timers.target
```

启用前执行 `systemctl daemon-reload && systemctl enable --now animemo-maintenance.timer`，再用 `systemctl status animemo-maintenance.timer` 和 `journalctl -u animemo-maintenance.service` 检查调度与失败告警。不要把 `audit_orphan_media --delete`、远端对象删除或任何 destructive task 放进 timer。本轮生产 timer 变更为 **NOT RUN**。
