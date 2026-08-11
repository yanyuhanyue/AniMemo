# Maintenance

维护任务统一通过 Django management command 执行。每个任务独立报告 `PASS`/`FAIL`，某个任务失败不会静默吞掉错误；总命令在任一任务失败时返回非零状态。

```bash
python backend/manage.py run_maintenance
python backend/manage.py run_maintenance --task purge_expired_revoked_tokens
```

当前入口只包含非破坏性任务：过期注册、撤销 token、外部账号会话、Integration 事件与已完成动作回执清理、媒体使用量刷新，以及只读孤立媒体审计。Integration 默认清理超过 1 天的已 ACK 事件、超过 7 天的未 ACK 事件，以及超过 7 天的 completed/failed 动作回执；pending 回执不会被维护任务删除。`audit_orphan_media` 默认只列出文件，删除必须显式传入它自己的 `--delete` 参数，不会由总入口触发。

## Query evidence

代表性手账列表查询可以在开发数据库或 CI 数据库中生成真实计划：

```bash
python backend/manage.py profile_journal_queries --username <用户名> --format json
# 或：python backend/manage.py profile_journal_queries --user-id <用户 ID> --format json
```

命令必须明确指定一个用户，支持 SQLite 和 PostgreSQL，默认最多分析 24 行，限制上限为 500；它只输出 `EXPLAIN`，不会输出用户名、邮箱、评论、令牌或凭据，也不会自动新增索引。只有当计划和基准证明需要时，才允许新增 migration/index。

## Scheduling

生产调度器可以使用宿主机的 cron 或 systemd timer，通过运行中的 AniMemo API 容器调用 `run_maintenance`，但不需要常驻 worker。实际生产 Compose 工作目录为 `/opt/1panel/docker/compose/anime-journal/app`，必须显式使用 `.env.production` 和 `deploy/docker-compose.yml`：

```cron
17 * * * * cd /opt/1panel/docker/compose/anime-journal/app && /usr/bin/docker compose --env-file .env.production -f deploy/docker-compose.yml exec -T api python manage.py run_maintenance >> /var/log/anime-journal-maintenance.log 2>&1
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
WorkingDirectory=/opt/1panel/docker/compose/anime-journal/app
ExecStart=/usr/bin/docker compose --env-file .env.production -f deploy/docker-compose.yml exec -T api python manage.py run_maintenance
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

启用前执行 `systemctl daemon-reload && systemctl enable --now animemo-maintenance.timer`，再用 `systemctl status animemo-maintenance.timer` 和 `journalctl -u animemo-maintenance.service` 检查调度与失败告警。不要把 `audit_orphan_media --delete` 或任何 destructive task 放进 timer。
