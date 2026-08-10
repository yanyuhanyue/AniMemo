# Dashboard Mutation Correctness 生产 Smoke 计划

本文件是 Dashboard Mutation Correctness 修复合并后的生产验收计划。它只描述如何在受控窗口验证客户端状态与服务端状态的一致性；本轮代码任务不连接生产、不 SSH、不执行任何真实写入。

## 适用范围

- 目标页面：已登录用户的 `/dashboard`。
- 目标行为：entry create/update/delete、快速修改观看状态、批量操作、个人资料保存、快速筛选保存/删除，以及查询变化和组件卸载期间的状态一致性。
- 生产部署：`NOT RUN`。
- 数据库迁移：`NOT APPLICABLE`；本修复不包含后端模型或 migration 变化。

## 运行前检查

以下检查只读，可在生产执行：

1. 确认当前部署 SHA 与计划验收的前端构建身份一致，并记录浏览器、API、反向代理的 scoped 日志窗口。
2. 确认测试账户属于明确的个人账户，且不会触发公开分享、外部同步或其他自动化流程。
3. 确认测试作品、测试标签和测试头像文件已提前选定；不要临时创建不可回收的测试数据。
4. 确认浏览器开发者工具已开启 Network、Console 和 Preserve log；不得复制 access token、cookie、头像上传内容或完整用户标识。

## SAFE TO RUN

这些步骤不写入业务数据，或只读取当前用户已有数据：

- 打开 `/dashboard`，确认首屏 `entries/` 只加载一次，随后只发生预期的 settings、filters、tag-presets、stats 请求。
- 使用已有搜索、状态、标签、年份、活动、排序和快速筛选，确认 URL/query 改变后旧请求不会覆盖当前列表。
- 执行 `/animemo` 之外的 Dashboard 只读检查：查看列表、海报视图、统计区、继续观看和提醒区。
- 在 Network 中确认旧请求被取消或其响应被丢弃，Console 没有 JavaScript、React 或未处理 Promise 错误。
- 刷新页面一次，确认服务端数据重新载入，且不会出现重复首屏请求。

## DO NOT RUN WITHOUT SUPERVISION

以下动作会改变生产数据或账户状态，必须在明确的维护窗口由拥有回滚权限的人员监督执行：

- 修改或删除已有 entry。
- 创建新的 entry，尤其是带上传封面的 entry。
- 批量修改观看状态、可见性或标签。
- 保存/删除自定义快速筛选。
- 保存头像、昵称、副标题或其他个人资料。
- 任何会触发 watch history、外部账号绑定、公开分享或导入流程的动作。

监督要求：每个动作只执行一次；记录请求方法、脱敏路径、HTTP 状态、响应时间和页面最终状态。失败时保留对话框与草稿，不要重复点击，先确认是否已有服务端成功响应。

## 建议验收顺序

1. **UPDATE 失败**：让一次资料保存返回 4xx/5xx（仅在可控的 staging 或合成环境）；确认对话框、草稿和原列表记录保留，状态为 `PASS` 或 `NOT RUN`。
2. **UPDATE 成功**：修改一个低风险字段，确认服务端响应返回后列表才更新，状态为 `PASS`。
3. **DELETE 成功/失败**：分别确认服务端确认后才移除，失败时记录仍在且不会重复 DELETE，状态为 `PASS`。
4. **CREATE 成功/失败**：确认成功后才插入本地列表，失败保留输入，状态为 `PASS`。
5. **快速筛选**：保存、重命名和删除各一次；失败保留编辑器与草稿，成功后只刷新筛选元数据，不无谓重载 entries，状态为 `PASS`。
6. **资料与头像**：使用小于 5MB 的 JPG/PNG/WebP 文件，确认请求为 multipart 且字段名为 `avatar`；成功后重新打开设置并确认头像来自服务端，状态为 `PASS`。
7. **并发/生命周期**：mutation pending 时改变查询、翻页或离开 Dashboard；确认旧响应不会写入新页面，组件卸载后无 state update/flash，状态为 `PASS`。
8. **scoped restart**：仅重启前端应用容器或进程（不得重启数据库、Redis、反向代理或整机），随后重复只读检查，状态为 `PASS`。

## 证据与停止条件

每个步骤至少保留：时间（Asia/Shanghai）、脱敏 URL/方法、HTTP 状态、可见 UI 结果和对应日志摘要。任何一个关键写入返回未知、出现重复请求、列表与服务端不一致、Console 有异常或影响其他用户时，立即停止后续写入，整体验收不得报告 `PASS`。

不得在报告、截图、归档或 issue 中记录：

- access/refresh token、cookie、CSRF token；
- Integration secret、头像二进制、完整 HMAC 签名；
- 完整 external identity 或可直接定位个人账户的标识。

## 本轮状态

```text
PRODUCTION DEPLOY: NOT RUN
PRODUCTION SMOKE: NOT RUN
DATABASE CHANGE: NOT APPLICABLE
MIGRATION RUN: NOT APPLICABLE
```
