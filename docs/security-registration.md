# 安全注册流程

AniMemo 的注册不再创建“未激活但已经知道密码”的用户。流程为：

```text
邮箱请求 → PendingRegistration → 邮箱验证 → 短期完成凭证 → 用户设置用户名/密码 → 正式 User
```

`PendingRegistration` 只保存规范化邮箱、SHA-256 token 摘要、过期时间、验证/消费时间、完成凭证摘要和最小审计摘要。原始注册链接只存在于邮件和浏览器当前 React 内存中；完成凭证消费后立即清空。开发环境邮件日志只记录收件域名和主题，不记录邮件正文或 token。

## 可选注册钩子

SDK v2 插件由宿主 Runtime Loader 显式注册正式 hook，不在 `AppConfig.ready()` 中偷偷修改核心运行时：

```python
host.register_hook("registration.before_complete", callback)
```

支持的正式 hook：

* `registration.before_request`
* `registration.before_complete`
* `registration.after_complete`
* `journal.after_create`
* `journal.after_update`
* `journal.after_delete`
* `column.after_publish`
* `column.after_delete`
* `user.after_created`
* `user.before_delete`
* `user.after_delete`

回调接收经过裁剪的 context 字典；Hook 必须先在 Manifest 声明。注册记录绑定 plugin slug、version 和 runtime id，停用或切换版本时只清理该 runtime 自己的 hook。

插件只收到经过裁剪的 request/user 代理、邮箱/用户名；核心不会传递密码、密码哈希、原始注册链接、完成凭证、JWT、refresh token 或 CSRF token。只有健康且启用的 `PluginDeployment` 才会执行 hook。失败策略由宿主固定：policy/security before hook fail closed，普通 after hook fail open；插件不能覆盖。核心 PendingRegistration 安全性不依赖任何插件。

Hook 使用受信任的进程内 Python 回调。宿主会以 `HOOK_SLOW_WARNING_SECONDS` 记录慢回调遥测；该阈值只用于告警，不会中断或强杀插件代码。

## 旧账号

Pre-1.0 inactive-user 注册兼容命令已经删除。过期 pending 记录只由 `purge_expired_pending_registrations` 清理。
