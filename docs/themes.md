# 札记主题扩展

`1.4.0-alpha.1` 首批提供 `theme.notes`：私人札记列表、阅读及写作窗口的浅色主题。当前确认过的纸页版式始终是内置默认。安装主题不改变任何人的外观；管理员启用后，用户自行选择。

## 使用

1. 管理员在「插件」启用随附的 **樱色信笺 1.0.0**，或审核并安装自己的声明式主题包。
2. 用户打开「札记 → 札记外观」，选择主题查看示例。预览和取消都不会保存，也不会改变对话框外的页面。
3. 点击「应用外观」保存到自己的账号，刷新及其他设备可继续使用。在同一入口选择「纸页原色 → 恢复默认外观」清除个人选择。
4. 管理员停用、卸载或隔离主题后，页面回退到内置外观。客户端在刷新、回到页面或最长约 30 秒的在线刷新后读取状态。停用保留原个人偏好，重新启用可继续使用；卸载清除对该包的选择。

主题不读取番剧或札记数据，不改变其他账号、瞬间画廊、收藏、公开页、登录、账号安全或后台。扩展出错、缺失或接口不可用时采用内置外观。作品详情和记忆检索中的札记编辑也复用个人主题；主题切换不会修改正文、日期或可见范围。

## 第一个可修改的包

清单在 `server/examples/notes-hanami/manifest.json`。无需编译 WASM：

```sh
node tooling/package-plugin.mjs server/examples/notes-hanami/manifest.json - .local/output/notes-hanami.animemo-plugin
```

包沿用现有 JSON 清单格式，`capabilities` 为 `["theme.notes"]`，宿主范围必须从 API 3 起，`module` 为空字符串，摘要是空字节的 SHA-256。主题内容放在 `notes_theme`，并纳入不可变包身份：

| 字段 | 允许的值 |
| --- | --- |
| `canvas`、`paper`、`ink`、`muted`、`primary`、`border`、`rule` | 完整六位十六进制颜色 |
| `heading_font` | `serif` / `sans`，均使用核心已有字体 |
| `reading_size` | `standard` / `large` |
| `spacing` | `comfortable` / `relaxed`，手机留白为 24 / 28 px |

宿主校验浅色背景、文字至少 4.5:1 对比度及选项范围。原始 CSS、JavaScript、选择器、远端字体/图片、未知字段与可执行模块均不能进入主题。CSS 变量和组件类名属于核心实现，不是公开 SDK。深色模式、主题资源及语义展示插槽留待后续实际消费者验证。

同 `slug + version` 的内容不能改写，修改清单需递增版本。上传新版不自动切换；复用插件页启用指定版本及切回历史版本。个人选择跟随管理员当前启用版本；过时的选择请求会被拒绝并要求重新预览。

## 官方身份、兼容与恢复

Docker 镜像的 `bundled-extensions.json` 把主题和 TXT 包绑定到准确核心二进制与包摘要，只有该清单中的包显示官方身份。手动上传同一示例不自行获得官方身份。随附主题可停用，不可直接卸载。

API 1–2 的文件转换器继续工作；主题不能进入转换接口。旧宿主拒绝 API 3 主题；实例升级预检、启动隔离和恢复规则继续适用。

包及个人偏好包含在完整实例数据库备份中。正常升级保留；恢复为新实例停用所有扩展，并清除个人主题选择，管理员和用户重新审阅选择。个人记忆 JSON/ZIP 只携带记忆内容，不携带实例扩展包与账号偏好。

## 验证

```sh
npm run check
npm run test:api -- -run 'TestNotesTheme|TestBundledNotesTheme|TestPluginLifecycleAndRealImport|TestPluginUpgradePreflight|TestPackageIdentityAndCompatibility'
# 仅在隔离的合成验收实例执行；文件含测试管理员凭据，不提交 Git。
REVIEW_ADMIN_ACCESS=.local/output/theme-review-access.json node tooling/themes-browser.mjs
# 对实际候选镜像验证随附身份、备份/恢复和更新；仅创建/清理 probe-* 实例。
ANIMEMO_CANDIDATE_IMAGE=候选镜像 npm run test:stage3
```

浏览器报告在 `.local/output/browser/themes/`。当前阶段未实现插件市场、任意前端插件、访问记忆的持久授权或 AstrBot Bridge。
