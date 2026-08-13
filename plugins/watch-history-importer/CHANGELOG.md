# Changelog

## 0.4.3

- 将当前 AniMemo 自有插件源码的 Manifest 许可证元数据更新为 `PolyForm-Noncommercial-1.0.0`。
- 仅发布新的 `0.4.3` 身份承载本次元数据更新；已发布的 `0.4.2` 包及其不可变内容身份不得重写。
- 明确 Bangumi API、返回数据与媒体，以及 React、Django、Django REST Framework 和 requests 等依赖继续适用各自条款。

## 0.4.2

- 通过 Manifest 明确声明 `journal` 与 `watch_history` Core capability。
- 通过 Host SDK 门面访问设置、存储与错误类型，移除官方插件对宿主内部模块的直接依赖。
- 复用 Core `JournalEntryService` 的条目 DTO、owner 隔离、serializer 校验与 mutation hook 边界。

## 0.4.1

- 限制单次 TXT 总上传量、持久化批次大小与每用户批次数，并由标准维护任务清理过期批次。
- 使用批次行锁保证不同 request_id 的并发提交只执行一次。
- 将 Core 条目、观看记录与批次状态纳入同一事务，完成事件改为 robust on-commit 投递。

## 0.4.0

- 保留 TXT/文档解析、编码探测、Bangumi 候选解析、批次预览与确认流程在 importer 插件内。
- 将标准化后的观看记录交给 AniMemo Core `WatchHistoryService` 持久化，不再读写 `PluginData/watch_history`。
- 通过绑定请求或 Integration 当前用户的 Host capability 访问 Core 条目与观看记录，不再直接导入 Core ORM。
- 声明 `0.4.0` 数据兼容回滚下限，阻止回滚后重新产生第二份观看记录数据源。

## 0.3.3

- 删除 provider-specific 接口，官方插件仅依赖 Integration Protocol v1。
- Web、Integration 动作与导入提交共用同一观看记录校验与幂等去重规则。
- Integration 文本预览限制为 120 KiB，为 256 KiB 请求 envelope 保留 JSON 开销。

## 0.3.2

- 补充观看记录事件的安全摘要字段与导入完成统计。
- 保留 0.3.1 的不可变包内容，并增强 Integration Bridge 诊断与回归测试。

## 0.1.0

- 支持年度 TXT 多文件拖拽上传与只读解析预览。
- 支持 Bangumi 分批匹配、人工选择与话数冲突拦截。
- 支持事务导入、已有番剧合并和观看历史幂等去重。
- 保留多刷记录与观看日期，并预留 provider-neutral 外部集成契约。
- 将来源标题中的季数作为硬匹配条件，兼容“第几季 / 第几部分 / 第几クール / 罗马数字”并阻止跨季误匹配。
# 0.3.1

- 增加 watch-history-importer 的 Integration Protocol v1 动作：观看记录读取/新增、条目搜索、导入预览与提交。
- 增加 `history-updated`、`import-completed` 私聊事件。
- 保留 0.3.0 的用户 PluginData、安装状态与可回滚版本。
