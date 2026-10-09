# 札记主题：模板、样式与随包资源

`1.4.0-alpha.3` 支持通过安装 `theme.notes` 包改变私人札记的排版。默认仍是已经确认的纸页；「樱色信笺」1.0.0 提供配色，「星笺展架」1.0.0 提供另一套列表、卡片与阅读布局。新增主题不需要修改核心代码或重新编译网站。

## 使用与作用范围

1. 管理员在「插件」审核并启用随附主题，或上传自己的 `.animemo-plugin` 包。
2. 用户打开「札记 → 札记外观」选择主题。预览使用包内模板和合成内容；取消不改变页面或保存选择。
3. 「应用外观」只保存自己的选择，刷新和其他设备继续使用。选择「纸页原色 → 恢复默认外观」清除个人偏好。
4. 停用、卸载或隔离主题后回退到内置外观。刷新、回到页面或最长约 30 秒在线刷新时更新状态。停用保留偏好以便重新启用；卸载清除对该包的选择。

当前只实现 `private.notes`。列表和阅读正文使用可选模板，写作窗口只应用色彩/字号等 token；编辑、删除、剧透展开、保存和确认控件继续由核心提供。主题不改变记忆内容、日期、权限或其他账号的选择，也不应用到后台、登录、瞬间、收藏、年度或公开页。

全站扩展按以下边界逐批实现，不把保留的范围当作现成功能：

| 范围 | 选择者及规则 | 当前状态 |
| --- | --- | --- |
| 私人札记 `private.notes` | 用户选择管理员已启用的包；无有效选择回退内置 | 已实现 |
| 私人其他页面 | 后续复用主题系统；站点默认之上允许个人选择 | 计划中，无相关 API |
| 站点首页/导航 | 管理员设置站点主题；不能替换身份或授权流程 | 计划中 |
| 公开作品/分享/年度册 | 使用发布者或站点明确选定的公开主题，只接收核心公开投影；不继承访问者私人主题 | 计划中 |
| 登录、账号安全、后台、更新恢复、危险确认 | 由核心维护，主题不可替换 | 固定边界 |

## 开发自己的主题

复制 `server/examples/notes-gallery/`，改 `manifest.json` 中的 `slug`、名称及版本，再修改同目录 `list.html`、`card.html`、`reader.html`、`theme.css` 和 `assets/`。无需 Go 或 WASM 构建：

```sh
node tooling/package-plugin.mjs server/examples/notes-gallery/manifest.json - .local/output/notes-gallery.animemo-plugin
```

打包工具将模板嵌入 JSON，并计算资源 SHA-256。安装仍由服务端重新检查，打包成功不代表已审核。`capabilities` 为 `["theme.notes"]`，`module` 为空，`module_sha256` 为其摘要。含模板的包要求 `host_api_min` 至少为 4；纯 token 主题继续支持 API 3。

`notes_theme` 保留 `canvas/paper/ink/muted/primary/border/rule` 六位色值，以及 `heading_font`（serif/sans）、`reading_size`（standard/large）、`spacing`（comfortable/relaxed）。宿主检查浅色背景和这些 token 的文字对比度；自定义 CSS 的最终对比度仍需主题作者实际验证。

可选 `notes_theme.presentation` 是版本 1 的模板契约：

| 字段 | 内容 |
| --- | --- |
| `schema` / `scope` | `1` / `private.notes`；未知版本或范围拒绝 |
| `list` | 列表容器 HTML，必须恰好包含一个 `items` 槽 |
| `card` | 单篇卡片 HTML，必需 `title`、`excerpt`、`metadata`、`read`；可选 `poster`、`work`、`picture` |
| `reader` | 阅读正文 HTML，必需 `body`、`metadata`；可选 `work` |
| `css` | 本包模板内的 CSS；不依赖网站外层 DOM |
| `assets` | 资源声明数组：`name`、`content_type`、`sha256`；原始字节存于包的顶层 `assets` 对象，以 base64 编码 |

使用标准 HTML 形式的占位符，如 `<div class="copy"><slot name="excerpt"></slot></div>`。宿主解析模板并插入 React 内容，槽中不能带备用内容、重复名称或自定义事件。槽可能产生块级内容，应放入 `div/section` 等容器。标题和阅读槽自带核心按钮；主题不自行构造业务链接。

允许的 HTML 元素为 `div section article header footer aside figure figcaption span p h3 h4 strong em small ul li br hr slot img`；结构元素只允许 `class`。装饰图片必须写成 `<img src="asset:notebook.png" alt="">`，引用包内图片，不能冒充用户海报。实际海报和私人附件由核心槽按原权限加载。每个模板最多 16 KiB、128 个节点、12 层。

CSS 最多 32 KiB，支持常规选择器、Grid/Flex、媒体查询，以及颜色、尺寸计算、渐变、基础变换等函数。禁止 `@import`、其他 at-rule、转义、宿主选择器、外部 URL、未知函数和视口固定定位；包内图片可写 `url('asset:notebook.png')`。具体支持值以 `server/internal/plugins/presentation.go` 的验证器和安装错误为准；本版不承诺完整 CSS 语法兼容。

模板样式运行在 Shadow DOM 中，宿主限制绘制范围。只将 `--notes-*` 主题 token、`--theme-font` 和核心内容的 `.theme-title/.theme-read/.theme-work/.theme-metadata/.theme-visibility/.theme-kept/.theme-tag/.theme-spoiler` 类用于当前模板契约；不要依赖 Tailwind、Base UI 或外层页面类。阅读正文保持核心结构，复杂排版先围绕 `body` 槽完成。

资源最多 8 个、单个 2 MiB、总计 4 MiB，只允许 PNG/JPEG、一个 WOFF2 字体及 UTF-8 许可文本（最多 32 KiB）。文件名为简单小写名称，不接受路径。图片检查实际解码及尺寸；字体检查头部和展开大小，由浏览器加载验证，失败回退系统字体。字体通过 `font-family:var(--theme-font),sans-serif` 使用，不写 `@font-face`。图片及字体许可须随包保留。

## 核心内容与安全边界

主题不能运行 JavaScript、WASM、事件处理器，不能发起任意网络请求或后台读取记忆。宿主把当前用户已经获准看到的内容交给固定槽，主题只安排其位置和视觉。剧透正文及附件在用户点击核心展开控件后才进入模板；展开前既不挂载正文，也不请求附件。

资源接口为 `GET /api/v1/themes/{slug}/{version}/assets/{name}`。必须登录且对应版本当前已启用、健康；每次读取核对摘要，停用或资源失效返回 404，响应为 `private, no-store`。此处不是私人图片存储，不能把用户附件复制到主题包。

Shadow DOM 是样式隔离，不是任意脚本沙箱。管理员仍须审核主题来源和视觉行为：自定义 CSS 仍可能做出难读、隐藏内容或不适合手机的页面。模板/脚本校验、错误边界和内置回退并不等同于第三方市场审核；作者应验证手机、键盘、对比度及长标题。外观选择器和后台保持核心界面，坏主题可被撤销。

## 版本、兼容与恢复

同一 `slug + version` 内容不可改写；模板、CSS 或资源变化均需递增版本。上传新版不自动启用，复用插件页切换指定版本及历史版本。个人选择跟随该包当前启用的版本；过期修订请求要求重新预览。

协议分为独立的 `server/pkg/converterproto`、`themeproto` 和包清单 `pluginproto`。TXT 示例只依赖转换协议，避免主题结构变化再牵动 WASM 字节。本次拆分后的 TXT 包为 1.1.3，转换语义不变；升级继续保留原先启用的旧包和字节。

镜像随附清单绑定准确核心二进制与包摘要，只有匹配清单的包获得官方身份；手动上传示例不自动获得身份。API 1–2 转换器、API 3 token 主题继续可用；旧宿主拒绝 API 4 模板包。降低核心版本须按实例回滚/恢复流程，不能将新数据库直接交给旧镜像。

迁移 024 把资源存入 `plugin_releases.assets`，模板与资源都随完整数据库备份带走；没有新的外部资源服务。正常更新保留包和个人选择；克隆恢复停用扩展并清除个人选择，重新审核启用后资源可用。个人记忆 JSON/ZIP 不包含实例主题或账号偏好。

## 验证入口

```sh
npm run check
npm run test:api -- -run 'TestThemePresentation|TestNotesTheme|TestBundledNotesTheme|TestPluginLifecycleAndRealImport|TestPluginUpgradePreflight|TestPackageIdentityAndCompatibility'
# 仅在隔离的合成实例运行，凭据文件不提交 Git。
REVIEW_ADMIN_ACCESS=.local/output/theme-review-access.json node tooling/theme-templates-browser.mjs
ANIMEMO_CANDIDATE_IMAGE=候选镜像 ANIMEMO_PREVIOUS_IMAGE=旧验收镜像 npm run test:stage3
```

浏览器脚本实际安装一份不同 slug、不同模板的包，验证无需改核心即可改变页面；同时覆盖资源、剧透、编辑、手机和停用回退。已有 `themes-browser.mjs`、`notes-browser.mjs` 保留 token 主题与默认纸页回归。实际证据见 [验收记录](verification.md)。
