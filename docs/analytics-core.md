# Analytics Core

`journal.analytics.build_user_analytics()` 是 AniMemo 个人统计的权威定义。网站 `/api/v1/stats/me/` 与只读插件能力 `host.analytics` 共用该服务，未来年度报告不得另写 ORM 统计口径。

当前指标：

- `summary.total`：未删除 Core 条目数。
- `summary.average_score`：只计算非空个人评分。
- `summary.shared`：可见性不是 private 的条目数。
- `status_distribution`：包括 completed、watching、planned、on_hold、dropped 的完整分布。
- `score_distribution`：按两位小数评分值分组。
- `watch_history_count`：日期范围内 Core 观看事件数。
- `active_days`：范围内有至少一条观看事件的不同日历日数。
- `monthly_activity`：按 `watched_on` 的年月分组的观看事件数。

`start` 和 `end` 是可选 ISO 日期，边界均包含。`watched_on` 是 Core `DateField`，因此范围使用用户可见的日历日期而非 UTC 时间戳截断；响应同时声明 Django 当前时区。未提供范围时统计全部观看历史。

本轮不实现年度报告插件、后台物化、任务队列或 OLAP 基础设施。
