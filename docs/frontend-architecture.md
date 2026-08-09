# 前端状态与路由架构

## 四类状态

1. **Server state**：手账条目、观看记录、分析、外部身份、外部同步、账号连接和服务端设置。它们从 API 读取，不写入浏览器持久存储。
2. **Authentication state**：内存中的 access token、当前用户和共享 refresh promise。refresh token 由后端通过 HttpOnly Cookie 管理。
3. **UI state**：模态框、当前 tab、筛选展开状态、加载/错误状态和表单草稿，只属于当前组件或页面。
4. **Persistent preference**：演示模式下的本地记录、视图偏好和筛选预设；生产认证凭据绝不在 localStorage/sessionStorage 中保存。

## Server-state consistency

`src/lib/serverState.js` 提供按 domain 的 revision/invalidation 通道。`src/lib/api.js` 在成功 mutation 后按路径发出失效事件，`useDashboardData` 和相关面板重新读取受影响的 server state：

- JournalEntry mutation：条目列表、详情和分析；
- Watch History mutation：观看记录、条目摘要和分析；
- External Sync mutation：同步预览、条目摘要和分析；
- 外部账号/import、settings 和 filters：仅失效对应 domain。

GET 请求不会触发失效；mutation 失败不会发布成功状态。组件仍由页面负责展示业务冲突和 provider 错误，不会被全局错误处理吞掉。

## Routing and loading

`src/App.jsx` 保留公开首页作为首屏 shell，其余大型页面通过 `React.lazy()` 加载，并由共享 `Suspense` fallback 提供可访问的加载状态。插件路由只有在服务端声明、manifest 校验和访问权限通过后才挂载；动态 plugin runtime path 不进入 OpenAPI core schema。

项目不引入 Redux、Zustand、MobX 或新的全局 server-state 库；当前 revision 通道是与既有 axios/API 层兼容的最小失效方案。
