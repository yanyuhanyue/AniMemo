# 星笺展架

私人札记与手账组合主题 1.1.0，要求宿主 API 5。`list.html`、`card.html`、`reader.html` 使用标准 HTML 和命名 slot，`theme.css` 只作用于主题自己的展示区域。`journal-list.html`、`journal-card.html`、`journal-row.html`、`journal-detail.html` 和 `journal.css` 定义手账卡片/列表、作品详情概览。两组模板共享 `assets/`，用户按页面单独选择。核心负责填入已经授权的内容及动作。改这些文件、递增清单版本后打包即可，无需修改或重新编译核心前端。

```sh
node tooling/package-plugin.mjs server/examples/notes-gallery/manifest.json - .local/output/notes-gallery.animemo-plugin
```

`notebook.png` 来自原 AniMemo 的 `public/assets/site-icon.png`，属于项目默认品牌视觉；遵循项目 NOTICE/TRADEMARKS，不从源码许可证推定商标授权。`display.woff2` 为 Noto Sans SC 拉丁字符子集，使用 SIL OFL 1.1，许可见 `assets/font-license.txt`；中文继续使用核心系统字体。字体通过 `var(--theme-font)` 引用。主题资源只允许包内 PNG/JPEG/WOFF2 和许可文本，不读取或上传用户数据。
