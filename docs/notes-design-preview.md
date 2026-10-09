# 札记版式试用（2026-10-09）

用户希望先试用三个方向再选定正式界面，随后反馈纸本与旧版风格各有优劣，因此追加一份融合预览。现有默认排版保留，在札记页点击「试用新排版」进入；顶部切换版式，「返回现有版」退出。预览选择只记录在 URL，不修改账号偏好。

| 方向 | 路径 | 主要区别 |
| --- | --- | --- |
| 番剧影评 | `/memory?notes_ui=review#notes` | 单栏文章列表，突出关联作品、标题与正文；有配图时并排展示 |
| 纸本札记 | `/memory?notes_ui=paper#notes` | 暖纸色、宋体标题、日期页边与连续页面；窄屏日期移到正文上方 |
| 旧版风格 | `/memory?notes_ui=classic#notes` | 参考旧项目的墨线边框、偏移阴影和彩色标签，桌面双栏、手机单栏 |
| 融合版 | `/memory?notes_ui=hybrid#notes` | 纸本的连续页面、宋体标题与日期页边，结合小面积彩色作品标签、墨线和醒目的操作按钮 |

各版均可阅读、编辑和保存，使用同一份真实数据，保存会生效。对比切换保留当前搜索、年份、珍藏筛选与分页，支持浏览器返回。瞬间等其他分类保持原有排版。阅读窗口和写作窗口也跟随选择；未选定前不把试用版式扩展成长期主题系统。融合版复用纸本布局，只增加局部视觉样式。

正文、图片、剧透保护和修订记录复用现有组件，未引入新 API、数据库迁移或生产依赖。关联作品通过当前页去重后的批量引用接口读取；没有逐卡片请求。无日期仍显示「日期未记」，只记年份时不会补出月份；列表仍按最近修改排序，纸本的日期页边不代表时间线排序。

纸本标题使用本机中文衬线字体（Linux Noto Serif CJK SC / Windows 宋体等），没有新增字体下载，因此不同系统字形略有差异。配图来自实际札记附件，未给无图札记填入无关海报。

## 验证与截图

在既有独立合成数据库 `animemo_ui_review_20261008` 上验证，API 使用 `127.0.0.1:18083`，Vite 使用 `127.0.0.1:5177`。原 Docker 验收镜像不随源码热更新，本轮没有替换该镜像或发布外网地址。

- `npm run check:web` PASS；Vite 生产构建 PASS，仍有既有主 JS 包超过 500 kB 的提示。
- `REVIEW_OUTPUT=.local/output/browser/notes-directions/verification node tooling/note-design-browser.mjs` PASS。三版均检查真实 API 的阅读、配图、剧透、日期精度、搜索/年份/珍藏筛选、空结果、创建与编辑持久化，以及 390/320 px 无横向溢出；9 次 axe 扫描无检出，0 页面脚本异常。
- `REVIEW_OUTPUT=.local/output/browser/notes-directions/default-regression node tooling/reading-browser.mjs` PASS。现有默认版与收藏/详情流程回归，7 组检查、7 次 axe 扫描无检出，0 页面脚本异常。
- 截图在 `.local/output/browser/notes-directions/`，每版包含 `*-desktop.png`、`*-reader.png`、`*-composer.png`、`*-mobile.png` 和 `*-composer-mobile.png`。截图账户只有合成内容；自动化另建独立测试账户，没有请求拦截或 mock API。
- 融合版追加验证：`REVIEW_DESIGNS=hybrid REVIEW_OUTPUT=.local/output/browser/notes-directions/hybrid-verification node tooling/note-design-browser.mjs` PASS，包含相同真实 API 流程、版式切换、390/320 px 布局，3 次 axe 扫描无检出、0 页面脚本异常。前端检查与生产构建通过；此次没有重跑其他版式的完整交互套件。融合版截图为 `hybrid-*.png`，原纸本与旧版风格仍可对照。
- 此处是 Linux Chromium 与手机视口检查，不代表 Safari 或真实手机软键盘验收；本轮未运行无关的 VM/长时负载检查。

复跑脚本可传 `REVIEW_ORIGIN` 指向本地真实开发服务；单独复查某版可传 `REVIEW_DESIGNS=classic`（或 `review,paper`）。脚本沿用已有 `.local/tools/browser` 的 Playwright、axe 与系统 Chromium。

用户选定后收敛到一版，删除未选样式和临时切换入口，继续精修所选方向；不复制三套业务编辑逻辑。
