# 开发验收记录

## 当前：札记三版试用（2026-10-09，云端）

按用户要求同时实现「番剧影评 / 纸本札记 / 旧版风格」三个可操作预览，保留现有默认界面。列表、阅读、写作使用相同数据与保存逻辑；预览参数只在 URL 中生效。具体入口、差异与复跑命令见 [札记三版试用](notes-design-preview.md)。

前端检查、生产构建、三版真实 API 交互与现有默认版回归均通过；三版 9 次及默认流程 7 次 axe 扫描无检出，0 页面脚本异常。检查了桌面截图与 390/320 px 手机布局，修正旧版风格弹窗标题的对比度。截图和报告位于 `.local/output/browser/notes-directions/`。没有 API/迁移/依赖改动，未替换原 Docker 验收镜像，未发布外网预览。

同日根据纸本/旧版各有优劣的反馈，追加「融合版」：保留纸本阅读结构，以墨线、彩色作品标签和按钮强调操作。复用已有布局与保存逻辑；前端检查、生产构建、融合版真实 API 流程及 390/320 px 布局 PASS，3 次 axe 无检出、0 脚本异常。证据在上述目录的 `hybrid-verification/` 与 `hybrid-*.png`，原候选及默认版保留。

随后为融合版页边接入关联番剧已有海报，放在日期/珍藏下面；手机缩为日期旁的小图，无封面时不填无关图片。批量引用响应新增可选 `entry_id` / `cover_revision`，封面沿用现有私有读取接口。`npm run check`、生产构建与三项相关 PostgreSQL 集成测试 PASS，覆盖封面归属、删除封面/作品与旧记忆身份保留。浏览器证据在 `poster-verification/`，桌面/手机截图为 `hybrid-poster-*.png`；截图海报来自明确标注的合成示例。修正了之前菜单直接引用 UI 引擎导致的依赖检查失败，没有新增生产依赖或数据库迁移。

## 从编辑表单到记忆阅读（2026-10-08，云端）

先将云端分支从 `12755de` 快进到 GitHub 的 `c3ec9ce`，接入 7 个提交，保留本地交接后的字体、评分、Bangumi 录入、作品布局、站点品牌与预设标签修改。本轮只改前端交互与样式，没有新增 API、数据库迁移或生产依赖。

- 收藏小册拆分为阅读与编辑：书封进入目录，札记和瞬间在小册中阅读，作品/角色进入关联记忆；保留剧透折叠、失效内容提示、排序、分享与移除确认。移除小册不删除作品或札记。
- 作品详情先展示短评与观看足迹，资料来源/分享按需展开，保留直接修改评分、封面管理和观看记录入口。未补记观看细节不显示误导性的 `0 / —` 进度。
- 札记使用书写页排版，日期/图片等仍按需展开，保留输入标签、焦点提示及私密/未知日期默认值。记忆库、收藏书封与年度册采用统一的阅读视觉，记忆页品牌复用站点配置。
- 根据用户对左栏的反馈，删除占用正文宽度的图标侧栏，改为顶部文字分类、墨色下划线和少量暖色标记；低频工具收进现有 Base UI 的菜单，保留键盘导航、焦点恢复、当前工具提示及 URL 历史。
- `npm run check:web`、`node --test web/tests/*.test.mjs`（4 项）、Vite 生产构建 PASS。仍有既有主 JS 包超过 500 kB 的提示，本轮没有扩大为全站打包重构。
- `REVIEW_ORIGIN=http://127.0.0.1:5177 node tooling/reading-browser.mjs` PASS：7 组真实 API 交互检查、7 次 axe 扫描无检出、0 页面异常。覆盖导航切换/历史、更多工具菜单、阅读不改写数据、剧透保护、失效引用、编辑排序持久化、未知日期札记、详情编辑、删除小册保留原记录、Enter 打开与 Escape 恢复焦点。
- 桌面 1440 × 1000、手机视口 390/320 × 844 验证无横向溢出，并实际检查截图。修正了无封面占位文字的对比度。此为 Linux Chromium 的视口与自动化检查，不代表真实手机软键盘或 Safari 验收。

开发预览使用独立数据库 `animemo_ui_review_20261008`、API `127.0.0.1:18083` 和 Vite `127.0.0.1:5177`，仅放合成数据。原 `stage3-review` 验收镜像/数据库没有替换；该镜像不会随源码热更新。本轮没有重建容器、重跑无关服务端全套测试，也没有签发新版本。

截图与对照：`.local/output/browser/ui-refresh/before/`、`after/`；交互报告与无障碍检查：`.local/output/browser/ui-refresh/validation/report.json`；构建日志：`.local/logs/ui-refresh-build.log`。浏览器脚本复用 `.local/tools/browser/` 中现有的 Playwright 与 axe，不加入默认开发门禁。

## 作品札记、集数整理与旧项目字体/评分（2026-10-08，本地）

对照旧项目 `src/styles.css`、`src/lib/rating.js` 与字体依赖，统一自托管 Noto Sans SC、数字/日文原名字体栈，以及 9/9.5/9.9 的评分分色边界。重排记忆工作区、札记列表/写作与集数目录/范围记录；沿用现有接口和数据库模型。

- `npm run check:web`、`node --test web/tests/*.test.mjs` PASS，覆盖评分边界和会话隔离。
- 内置浏览器 + 隔离 PostgreSQL/真实 API 实测 PASS：年份与珍藏组合筛选，正文编辑保留原年份/标签/珍藏，新建札记默认私密/未知日期，更换作品同步 URL/标题/查询，单集添加、批量添加和两集近似观看范围保存。读取 API 核对结果与操作一致，观看记录仍为 0。
- 桌面 1280 × 720、手机 390 × 844、窄屏 320 × 720 检查；修复最小页面宽度引起的横向溢出。检查有内容和空白状态、写作弹窗及四档评分。页面正文实际为 Noto Sans SC，字体资源同源加载。
- 截图、API 核对和部署记录保存在 `.local/output/memory-ui/`。没有重跑不受本轮影响的服务端全套测试、全站 axe 或 iOS Safari；不以当前检查代表这些范围。

## 番剧录入与资料弹窗修复（2026-10-08）

新建可在保存前搜索和选择 Bangumi，选择结果回填到原草稿；最终创建在同一事务保存作品、来源、封面与个人字段。修复资料弹窗正文裁切，增加海报搜索结果，简化状态名称及作品卡片。

| 检查 | 实际结果 |
| --- | --- |
| 源码及合同 | `npm run check` PASS；OpenAPI 与 Go/TypeScript 生成物一致 |
| PostgreSQL | `TestBangumiMetadataIsolationAndConflicts`、`TestRecorded*`、`TestImport*` PASS；新建保留草稿字段，非法草稿无残留，既有条目拒绝混入新建内容 |
| 真实 Bangumi 与浏览器 | 隔离账号搜索夏目友人帐、选 subject 259、带回表单并保存 PASS；封面、13 话资料、8.5 分、标签及选片前的感想均保留；数据库仅新增一条、默认私密/recorded/0 已看话数 |
| 回退与既有入口 | 返回手动填写保留草稿；已有作品刷新资料保留个人内容；卡片/列表、详情关闭后的焦点恢复 PASS |
| 布局 | 桌面 1280 × 720、手机 390 × 844 与 320 × 640 实际检查；窄屏正文无横向溢出，标题/关闭按钮及预览底部操作可达；没有观察到页面脚本异常 |

截图与保存结果在 `.local/output/entry-ui/`，数据库结果在 `.local/output/entry-source-test-api.json`。本轮使用内置浏览器与真实 Go API，没有重新执行 axe 全站扫描，也不代表 iOS Safari 验收。用户实例升级与备份记录另存 `.local/output/entry-ui-local-review-update.json`；源码通过既有分支交接，不新建正式发行标签。

## 1.3 RC4 本机交接（2026-10-08，Windows + Docker Desktop）

已核验新交接包的 332 项文件，并接入 `E:\番剧记录\animemo-next`。原 ZIP SHA-256 为 `d2489c2ded480e174d815a83867b3cc87a07868993326c0e3ac2cdd9faf354ad`。从本机 RC2 提交 `b61a63b` 应用累计补丁得到的 Git tree 与包内源码提交 `12755de` 的 tree 一致，均为 `58e0ae608e26a3e67e97688f37cf9339bd3f6fba`；合并冲突按这一完整结果核对，295 项源码逐一校验通过，保留两边提交历史。

应用仍使用包内 `8942721` 对应的原始 RC4 镜像，没有因本机验收脚本或文档修正重新构建。配置摘要为 `sha256:46b712f5cfe0c54552690993b6e360e7f204af5d766604eeafe0b5a344698055`，本机 containerd 标识为 `sha256:9e89de37b6caead780d399e7e41c537e5e68900bbc59a5f7e90b38607b6be4f0`，归档摘要与下方云端记录一致；开发加载明确返回 `trust_verified: false`。

环境：Windows PowerShell、Node 24.12.0、Go 1.26.6、Docker Linux engine 29.7.2、Compose 5.5.0、PostgreSQL 17.11、age 1.2.1、本机 Chrome。

| 检查 | 本机实际结果 |
| --- | --- |
| 静态与单元 | `npm run check` PASS，13.5 秒；TypeScript、OpenAPI 生成一致性、Go vet、模块边界通过；16 项 Node 检查全部通过，Go 单元包含在下方集成运行中 |
| PostgreSQL 全量回归 | `npm run test:api` PASS，97.1 秒；Windows Go 连接独立 PostgreSQL 容器，涵盖新成就规则、并发发布、上传权限和 JSON/ZIP 图片历史恢复；本机未启用 race |
| RC2 → RC4 完整记忆恢复 | 5 项 PASS，101.1 秒；不同镜像升级保留记录、完整记忆关系、原图和旧成就修订；新发布图片成就自动授予，展示槽和图片经完整快照恢复；错误镜像拒绝 |
| 实例生命周期 | 12 项 PASS，269.6 秒；实际 19 → 21 迁移、配置与中断恢复、age 加密转移、错误密钥/损坏包拒绝、独立恢复、更新数据库副本、显式回滚、救援快照和坏候选自动恢复 |
| 真实浏览器 | 四套共 20 项 PASS：记录主链 7、记录成就 4、展示/撤回/编辑器 6、自定义图案和自动授予 3；全部使用最终镜像、真实 API/Worker 与隔离合成账号 |
| 布局与可访问性 | 成就页面/编辑器共 16 个宽度检查，覆盖 320/390/768/1440 px；记录主链另检查手机宽度；共 12 次 axe 扫描无检出、0 页面异常。已实际查看五档边框和上传后的获得卡片截图 |
| 用户实例升级 | `local-review` 原地址 `http://127.0.0.1:18082` 已升级 RC4，21 项迁移，doctor PASS，app/worker/db healthy；比较旧库与新库，原账号、密码、权限、条目、札记和观看记录保留。自动备份及旧库保留；需要使用原账号重新登录 |

两套旧成就浏览器脚本沿用开发页面的内联脚本注入方式；本次与已有记录/图片脚本统一，通过浏览器调试接口运行 axe，实际在最终容器上通过，生产 CSP 未改动。README 同步 RC4 版本及 v3 图片导出范围，交接指南修正备用端口。没有新增应用功能或生产依赖。

证据：`.local/output/rc4-package-verification.json`、`rc4-loaded.json`、`rc4-test-api.{json,log}`、`rc4-memory-instance-smoke.json`、`rc4-instance-smoke.json`、`rc4-browser.json`、`rc4-local-review-{update,doctor}.json`；浏览器截图与报告位于 `.local/output/browser/rc4-windows-{recording,achievement-content,achievement,achievement-art}/`。隔离验收实例及其数据卷已清理，用户实例继续运行。

源码通过既有 GitHub `codex/animemo-next` 分支交接，提交及远端 CI 以分支记录为准。未创建版本标签、签发证明或发布 Release。包内没有新增真实服务凭据；邮件收件、OAuth、R2、域名/TLS 仍未验收，继续默认关闭。VM、长时负载和 Safari 未运行；下方云端证据保留原环境范围。

## 历史：1.3 RC4 发行收尾（2026-10-08，云端 Linux）

应用源码已冻结为提交 `89427216c4c873ba9614d39c14c2366b83d7e2ae`，版本 `1.3.0-rc.4`，21 个迁移。本轮不扩张功能；补充发行基线选择、真实跨版本记忆库验收及交接文档。镜像仅构建一次，后续探针/文档修正不改变应用或构建输入，不重复构建。

镜像 `sha256:46b712f5cfe0c54552690993b6e360e7f204af5d766604eeafe0b5a344698055`；归档 29,877,248 字节，SHA-256 `55390dc099059a2fb55c6efea2aaf0f20c3d8c12ae824f7a5ec6cfc15eb325ed`。清单在 `.local/output/release-v13-rc4/release.json`，绑定干净源码提交，但仍明确标记 `development: true`，没有正式来源证明。

| 检查 | 实际结果 |
| --- | --- |
| 完整 PostgreSQL 回归 | 同一应用源码在发布评估时执行 `npm run test:api`，PASS，63.4 秒；包含 Go 单元与真实数据库集成，不启用真实外部服务/浏览器夹具，不冒充全量 race |
| 收尾工具与文档 | 16 项 Node 检查 PASS；修改后的 JS、发行 YAML 和 Bash 语法、文档链接检查通过；既有 RC4 静态、构建、专项 race 和浏览器证据按下节范围保留 |
| RC2 → RC4 完整记忆 | PASS，69.9 秒。先在旧镜像建立记录、札记、角色/集数、原图、收藏、年度册与旧成就，再升级；内容、图片字节及旧解锁修订保留；新上传徽章无需用户继续操作即可自动授予，展示槽与图案随完整实例快照恢复 |
| 实例生命周期 | 12 项 PASS，175.8 秒；确认不同镜像与 19 → 21 迁移，真实 age 加密往返、错误密钥/损坏包拒绝、配置失败恢复、更新新数据库、显式回滚、升级后写入救援和坏候选自动回退 |
| Worker / 随附扩展 | 5 项 PASS，86.0 秒；真实 WASI 子进程确认导入、停 Worker 后持久排队与续跑、恢复副本默认停用扩展、中断更新恢复、坏包隔离且核心仍可用 |
| 操作中断恢复 | 2 组 PASS；恢复中断在新数据库重试，已启动后的新写入保留；回滚中断恢复原数据库，回滚侧新写入保存在救援快照 |
| 原验收站升级 | `stage3-review` 已切换到上述 RC4 镜像，doctor PASS，app/worker/db healthy；原有 7 条记录、完整记忆库和 3 条历史成就核对保留，更新前快照与回滚镜像保留 |
| 最终容器浏览器 | 记录与回忆 7 项、成就图片上传/自动授予 3 项 PASS；记录页 320/390/768 px、成就编辑器 320/390/768/1440 px 布局通过，6 次 axe 扫描无检出，0 页面异常；使用合成数据，新增测试管理员结束后撤权停用，测试规则停用 |

完整记忆探针最初因 RC2 没有 `badge_image_id` 而 RC4 显式返回空字符串导致断言失败；第二次合成系列名超过既有 40 字符约束。只修正探针的空字段兼容和夹具名称，最终上述完整流程通过；没有修改应用来迁就测试，也没有隐藏首次失败。所有探针容器及其数据库卷均已清理。

发行流程现在消费显式选择且验证过的前版 Release，并执行完整记忆恢复。首个正式 Release 没有前版时只声明新安装/恢复；后续支持旧版升级需配置 `previous_release` 或仓库变量，不能将同镜像测试称为跨版本升级。云端 RC2 未签发基线仅用于本地真实升级证明。

证据：`.local/logs/rc4-release-assessment-api.log`、`rc4-closeout-{node,memory-instance,instance,stage3,recovery,recording-browser,art-browser}.log`；`.local/output/release-v13-rc4/` 中的四份实例报告；`.local/output/rc4-closeout-review-{update,doctor}.json` 与 `.local/output/browser/rc4-closeout-{recording,art}/`。首次失败日志和回执保留在 `.local/logs/rc4-closeout-memory-instance-{first,second}.log` 及 `.local/output/rc4-closeout-memory-instance-{first,second}.json`。

图案脚本首次在生产容器的 CSP 下被禁止注入内联 axe 脚本；改为与记录脚本一致的浏览器调试接口执行后通过，没有放松生产 CSP。既有验收站管理员不符合该脚本的 `@example.test` 合成账号约束，改用专门注册、短暂授予权限、最终撤权停用的测试账号。初次验收站回执误读 `revision` 字段，已使用真实 `source_revision` 和实际迁移表复核；实例升级与数据比较成功，没有重复执行升级。上述检查器修正均不改变镜像内应用。

验收站仍仅监听云端 `127.0.0.1:18082`，不是公网链接；用户侧通过端口转发或在本地启动本包访问。旧 RC2 包保持不变；新完整交接包包含当前源码、镜像、Git bundle、两种基线补丁及选定证据，不包含云端数据库或凭据。

云端 GitHub CLI 授权检查实际失败，提示 `GH_TOKEN` 无效；环境未配置可用的外部服务凭据/身份，HTTP 允许列表仍不包含全部 GitHub API/Sigstore 服务。未推送、打远端标签、签发证明或发布 Release。邮件真实收件、OAuth、R2、公网域名/TLS 不计为已验证；默认关闭的外部能力不阻塞核心候选。Windows RC4、Safari、VM、长时负载未执行；后两项仍可选。下一步及本地启动/升级命令见 [本地交接](local-package-handoff.md)、[发行说明](release.md)。

## 历史：1.3 RC4 自定义图案、等级边框与自动授予（2026-10-08，云端 Linux）

源码及 OpenAPI 版本 `1.3.0-rc.4`，迁移 021。后台可上传 PNG/JPG 中心主图（2 MiB），规范化为最大 256px 的 PNG；8 种内置主图，青铜/白银/黄金/铂金/幻彩五档边框及月桂、冠饰、宝石，前后台共享。详情见 [成就内容](achievement-content.md)。没有新增生产依赖。

发布、重启用及修改统计门槛会自动把已有正常账号加入原队列；Worker 每轮最多评估 10 个账号，文案/图案调整不重算全站。先锁队列代次、后读取规则，修复发布与 Worker 并发时可能使用旧规则消费新任务的问题。旧授予不重复，撤回不重授。

| 检查 | 本轮实际结果 |
| --- | --- |
| 静态、单元、构建 | `npm run check` PASS；Go 单元与 16 项 Node 检查 PASS（2.8 秒）；Vite 生产构建 PASS（0.42 秒），仍有既有单包超过 500 kB 的提示 |
| PostgreSQL 与 race | `TestAchievementPublicationDuringWorkerRead`、`TestAchievementArtPublicationAndPortableHistory`、`TestRecordingAchievement*`、`TestMemoryAchievementsAndSearch`、`TestMemoryLibraryPortableRoundTrip` 组合 PASS（63.8 秒），包含两个格式子用例；不是数据库全量回归 |
| 上传与权限 | 普通用户/匿名上传拒绝；SVG、坏图片、超过限制的请求拒绝；规范 PNG、重复去重、未发布预览隔离、历史拥有者读取旧图、非拥有者拒绝、图片元信息响应通过 |
| 自动授予 | 先记录、后启用规则的空闲用户自动获得；直接发布新等级、降低门槛、重新启用评估通过；纯图案编辑不入队；四 Worker 不重复授予；专门用数据库查询屏障复现发布/读取时序，确认不漏算 |
| 历史与恢复 | 修改主图不覆盖旧解锁；JSON/ZIP 携带两份历史图片，在另一随机 schema 恢复后字节、修订、获得日期、展示槽不变，陌生规则保持停用；篡改图片备份拒绝；超过一天但被历史引用的图片在清理时保留 |
| 真实浏览器 | 图案编辑器实际上传、保存，已有达标用户无需再写记录即可自动获得带自定义图片的成就；8 个内置主图、5 种不同边框及等级纹饰检查通过，合成规则在结束时停用 |
| 布局与可访问性 | 图案编辑器在 320/390/768/1440 px 无横向溢出，桌面和手机 axe 无检出；此前图鉴/内容、纪念架、撤回恢复、键盘操作两套脚本在 RC4 再次通过；共 8 次 axe 扫描无检出、0 页面异常 |

证据：`.local/logs/achievement-custom-api-final.log`、`achievement-art-cleanup-test.log`、`achievement-art-check-final.log`、`achievement-custom-unit.log`、`achievement-custom-build.log`、`achievement-art-browser.log`、`achievement-custom-{content,polish}-browser.log`。截图和报告在 `.local/output/browser/achievement-art/`、`achievement-custom-content/`、`achievement-custom-polish/`。已实际查看五档边框、八种图案和上传后的用户成就截图。

首次上传浏览器检查遇到 Chromium inspector 响应体缓存被回收；改为核对真实界面中的图片 ID，并通过 API 重新读取已保存规则和图片后通过，没有替换成模拟接口。增加引用/清理事务协调后，对上传与跨实例恢复再次定向验证。

本轮为源码和开发服务交付，未推送远端或重建旧 RC2 镜像/交接 ZIP。未重复全量集成、Windows、Docker 镜像升级/回滚、Safari、VM 或长时负载。数据库迁移与跨 schema 恢复结果不冒充容器实例升级证明。

## 历史：1.3 RC3 记录与回顾成就（2026-10-08，云端 Linux）

源码及 OpenAPI 版本 `1.3.0-rc.3`，迁移 19 → 20。新增 7 枚默认成就（共 6 系列、12 枚）、3 种统计条件，图鉴优先展示记录与回顾系列，增加筛选和条件说明；沿用五种 SVG 纪念章、纪念架及后台图案预览。完整口径见 [成就内容](achievement-content.md)。没有新增生产依赖。

| 检查 | 实际结果 |
| --- | --- |
| 静态、单元、构建 | `npm run check` PASS（8.8 秒）；Go 单元及 16 项 Node 检查 PASS（2.9 秒）；最终 `check:web` PASS（7.6 秒）；Vite 生产构建 PASS（0.36 秒），仍有既有单包超过 500 kB 的提示 |
| PostgreSQL 专项与 race | `npm run test:api -- -race -run 'TestRecordingAchievement\|TestMemoryAchievementsAndSearch\|TestMemoryLibraryPortableRoundTrip'` PASS（38.7 秒）；随机 schema 隔离并清理 |
| 统计与历史 | 仅作品名、未知日期、按作品身份去重、想看分类排除、空收藏/空册排除、重复修订不累加、阈值 5/20/10/3、删除后更新当前值但保留获得历史、软删除/恢复触发、账号隔离均通过 |
| 并发与补算 | 四 Worker 并发不重复解锁/授予事件；新收藏条件支持冻结补算，后续规则编辑不改变已冻结的修订；撤回不重授 |
| 19 → 20 升级 | 在真实 PostgreSQL 应用前 19 个迁移、建立记录与旧成就及自定义等级，再执行 020；已有内容自动得到新纪念，旧解锁不变，自定义等级不覆盖，重复迁移通过 |
| JSON / ZIP 恢复 | 两种格式均保留四类成就的规则、值、日期、授予、通知、展示槽及事件，未知观看事实不变；年度恢复触发器不产生重算队列 |
| 浏览器内容链路 | 真实 Worker 自动解锁：作品名＋未知日期回忆获得两枚，收藏＋年度选材获得另两枚；12 枚图鉴、四个记录系列筛选、单位和说明、重复保存保留原解锁均通过 |
| 图标和后台回归 | 既有脚本 6 项流程通过；通知、展示增删、撤回/恢复、5 图案实时预览、20 级、停用规则保存、键盘及关闭焦点返回通过 |
| 布局与可访问性 | 内容页 4 种宽度，图鉴/编辑器另各 4 种宽度（320/390/768/1440）通过；共 6 次桌面/手机 axe 扫描无检出，0 页面异常；已查看桌面与手机截图 |

首轮新增夹具误用了不含图片的“瞬间”和未存入数据库的系列名，已按真实模型修正，最终专项全部通过。浏览器夹具也改为只提交可写的收藏字段；发现系列选择器的可访问名称不稳定后补了明确名称并复测。

证据：`.local/logs/achievement-content-{check,unit,api-final,check-web-final,build-web-final,browser,polish}.log`；`.local/output/browser/achievement-content/report.json`、`catalog-1440.png`、`catalog-390.png`、`recording-keepsakes.png`；图案与后台回归在 `.local/output/browser/achievement-content-polish/`。所有浏览器数据均为合成账号，后台只新增停用的合成规则。

本轮更新的是源码和 `5177` 开发服务（API `18083`）。旧 RC2 ZIP、镜像与独立 `18082` 容器保留原版，没有推送 GitHub 或发布新镜像。未重复全量集成、Windows/Docker Desktop、实例镜像升级恢复、Safari、VM 或长时负载；上述数据库升级证据不等同于新容器镜像验收。

## 历史：1.3 RC2 成就展示打磨（2026-10-08，云端 Linux）

核对远端 `b61a63b` 并接入本机运维修复后，调整五种徽章、状态、纪念架及后台图案预览。产品版本仍为 `1.3.0-rc.2`；本轮为其后的未提交前端改动，不改变数据库/API 或默认成就规则，尚未进入原交接镜像。进度、截图审查与剩余内容缺口见 [成就审查](achievement-design-review.md)。

`npm run check:web` PASS（8.8 秒），模块边界检查 PASS。真实浏览器通过新账号未获得状态、领取通知、展示添加/移除、撤回与恢复、五种图案预览、20 级显示、停用规则保存、键盘选择及关闭焦点返回。图鉴/编辑器各 4 种宽度（320/390/768/1440）无横向溢出；4 次桌面/手机 axe 扫描无检出、0 页面异常。首次标签对比度问题已修正后复测。检查只使用合成账号和停用测试规则。

Vite 生产前端打包通过；后台表格修订换行检查及追加 axe 扫描通过。证据：`.local/logs/achievement-check-web.log`、`achievement-browser.log`、`achievement-build-web.log`，`.local/output/browser/achievement-polish/report.json`、`admin-table-check.json` 和同目录截图；审查前截图在 `.local/output/browser/achievement-audit/`。本轮未重新进行 Windows、Safari、全量数据库、VM 或长时负载验收；不以既有通过记录代替本轮执行。

## 1.3 RC2 本机交接（2026-10-08，Windows + Docker Desktop）

已在 `E:\番剧记录\animemo-next` 接入交接包：包内 305 项文件校验通过，完整补丁基线为 `4eb8b88732a5dc25bfb58bbc0008886fc624afe0`，应用源码与包内 279 项源码清单一致；本机新增改动仅涉及运维工具、验收脚本和交接文档。原 ZIP SHA-256 为 `014cd4bc6c133e86f90bedc20f24ca7a2be3442bc55018fd29b3311f8072c74b`。

环境：Windows PowerShell、Node 24.12.0、Go 1.26.6、Docker Desktop Linux engine 29.7.2、Compose 5.5.0、PostgreSQL 17.11、age 1.2.1、本机 Chrome。Docker Desktop 最初因运行目录的陈旧 socket 无法启动；保存原运行目录后重建空运行目录恢复启动，没有删除镜像、数据卷或虚拟磁盘。这是本机恢复操作，不代表 Docker Desktop 的长期缺陷已修复。

修复了实际阻止 Windows 安装的镜像标识差异：交接包配置摘要为 `sha256:44d8eb4ba3eec3771ae0d8e5f81706256adb541fc88a9adb7040f366c3952e47`，同一归档在本机 containerd 中显示为 `sha256:be795c551b753ffdbfdc880f052bd41c6f5f5040d7ce6e32b0a806c41532072e`。加载器现在验证归档中的摘要关系并返回本机标识；完整备份也记录可移植配置摘要。开发加载需明确 `--development`，不会变为正式来源验证。本次一直使用包内最终应用镜像，未重新编译或修改产品代码。

| 检查 | 本机实际结果 |
| --- | --- |
| 源码检查与单元 | `npm ci --ignore-scripts`、`npm run check`、`npm test` PASS；运维修复后重跑 Node 检查，16 项全部 PASS，包含配置/OCI 标识、篡改和错误镜像拒绝 |
| PostgreSQL 全量集成 | `npm run test:api` PASS，154.8 秒；Windows Go 连接隔离 PostgreSQL 容器，各测试独立 schema，完成后移除测试容器 |
| 最终镜像记忆库恢复 | PASS；真实 Worker、私人图片字节、完整关联、未知事实、冻结年度册、成就逐字段比较，模拟经典 Docker 快照在 containerd 恢复，错误配置摘要拒绝 |
| 实例运维 | 12 项 PASS；新建、诊断、配置变更/中断恢复、真实 age 加解密、错误密钥与损坏密文拒绝、独立恢复、旧会话撤销/TOTP 保留、更新、显式回滚、救援备份、坏候选自动恢复；更新路径使用同一 RC2 镜像，不算新的跨版本迁移证明 |
| 记录主链浏览器 | 7 项 PASS；仅作品名保存、未知日期回忆、精确事实不预填、折叠字段保留、工具直达、检索和 320/390/768 px；0 页面异常，扫描范围内 axe 0 检出 |
| 后台浏览器 | 9 页 × 4 种宽度 PASS（1440/390/768/320 px）；桌面/手机共 18 次 axe 扫描无检出，0 页面异常；用户操作取消、焦点返回、维护取消、内容检查、预设增改删、设置保存、扩展启停与包审阅取消通过 |
| 留给用户的实例 | `local-review` 在 `http://127.0.0.1:18082` 运行，doctor PASS，app/worker/db 全部 healthy，初始化仍可用；测试账号及记录只在已经清理的隔离实例中 |

恢复脚本的首次比较因先读取导出、后等成就补算完成而产生时间戳竞争；已等待队列完成、停止测试 Worker 后重新读取基准，保留全部字段比较并复测通过。后台脚本同步当前入口文案并补入成就页。原云端证据保留为下面的历史范围，未计入上述本机结果。

证据：`.local/output/rc2-test-api.{json,log}`、`memory-instance-smoke.json`、`instance-smoke.json`、`rc2-browser.json`、`rc2-local-review-doctor.json`；截图及扫描报告在 `.local/output/browser/rc2-windows-recording/` 和 `.local/output/browser/admin-redesign/verification.json`。桌面后台及手机新建截图已经实际查看。所有临时探针已清理，保留用户实例及本机验收备份。

仍缺真实 Resend 发信凭据/收件验证、Bangumi OAuth 应用/授权账号、R2 私有 bucket 凭据和部署域名/TLS。已复查旧项目配置与 SQLite：没有可用的对应密钥记录。源码通过 GitHub `codex/animemo-next` 分支交接，远端提交及 CI 状态以分支记录为准；未进行版本标签签发或 Release 发布。VM、长时负载、完整 race 和 iOS Safari 未在本机重跑。启动及初始化命令见 [本地交接指南](local-package-handoff.md)，外部服务参数见 [外部交接](local-network-handoff.md)。

## 历史：1.3 RC2 记录与回顾收敛（2026-10-08，云端 Linux）

按用户明确定位调整产品与路线：保存看过的番剧和回忆，不承担追番任务、排期或提醒。源码 `1.3.0-rc.2`，详细交互与数据语义见 [交付说明](v1.3-delivery.md)。新建只需作品名，默认 `recorded`（看过，细节未记）；自由回忆复用 MemoryNote，按需展开结构化工具。迁移 019 只扩展状态与筛选约束，不改写已有条目。

| 检查 | 实际结果 |
| --- | --- |
| 静态与单元 | `npm run check`、`npm test` PASS；TypeScript、OpenAPI 生成一致性、Go vet、模块边界、15 项 Node 检查及 Go 单元；产品/OpenAPI 版本一致性纳入构建前检查 |
| 全量 PostgreSQL 集成 | `npm run test:api` PASS，58.2 秒；含既有账号、权限、来源、同步、导入、媒体、分享、成就等回归。本轮未重复全量 race，既有竞态证据按其源码范围保留 |
| 新记录的真实性 | PASS：最小创建、未知日期、独立回忆不产生集数/完成事实，按新状态检索和保存筛选，跨账号拒绝，明确集数记录及撤回；资料元数据不推断观看经历 |
| 个人往返与外部边界 | JSON/ZIP 两种完整恢复均保留新状态、关系和未知事实；Bangumi 写回拒绝猜测不支持的状态。使用受控元数据与协议，未调用正式授权服务 |
| 真实浏览器 | 新建默认仅两个输入框；作品卡片写回忆并自动关联；日期、集数、刷次不预填；编辑折叠字段不覆盖原评分/标签/资料；文字检索、次级工具直达、焦点返回与 320/390/768 px 布局 PASS |
| 自动可访问性 | 作品页、新建弹窗、回忆弹窗和手机记忆检索 axe WCAG A/AA 0 检出、0 pageerror；修复无标签卡片提示文字的对比度。不代表完整无障碍认证 |
| 容器升级与恢复 | RC1 → RC2、18 → 19 迁移 PASS；新建无精确细节的记录在跨实例快照恢复后仍为 recorded、0 已记录话数且感想完整；原有记忆库深比较、私人原件字节、年度冻结和成就历史检查通过；两个探针均清理 |
| 最终验收实例 | 最终镜像更新完成；doctor PASS，app/worker/db healthy。运行中的 OpenAPI 版本与 recorded 枚举核对通过；同一浏览器主链在 18082 实际容器再次 PASS。原六条展示记录保留，补充一条私密合成旧番及未知日期回忆 |

证据：`.local/logs/recording-api-final.log`、`recording-check-final.log`、`recording-test-final.log`；`.local/output/browser/recording-dev/report.json`；`.local/output/memory-instance-smoke.json`。完整 API 回归最初指出旧夹具默认把作品当作已完结，已为该完成统计用例明确提供播出状态后重跑通过；实际产品的未知状态不再靠默认值补全。

容器恢复使用 `sha256:550e20ef77fdc8794764e39f9d3e436dc5b53ee9ff4d5373956dfef70d9a21fa`。之后仅统一嵌入 OpenAPI 的版本元数据，最终镜像为 `sha256:44d8eb4ba3eec3771ae0d8e5f81706256adb541fc88a9adb7040f366c3952e47`，产物清单在 `.local/output/release-v13-rc2-final/release.json`，相同业务代码与数据库迁移不重复全套恢复。最终实例和浏览器复核回执分别保存于 `.local/output/recording-review-doctor.json` 和 `.local/output/browser/recording-review/report.json`。新增可复用脚本为 `tooling/recording-browser.mjs`。

云端 vfs 构建缓存曾使可用空间降到约 611 MiB；只清理未使用构建缓存后恢复约 16 GiB，保留实例、备份和回滚镜像。维护方法已写入 [云端开发](cloud-development.md)，不增加自动清理服务或默认构建门禁。

本轮未增加第三方主题/Bridge/个人插件授权，优先完善记录主链。未推送、签发或正式发布；邮件实际收件、正式 OAuth/R2、公网 DNS/TLS 仍按既有交接处理。Windows 原生、VM 和长时负载未执行；后两项保持可选。

## 历史：1.3 RC1 个人记忆候选（2026-10-08，云端 Linux）

源码版本 `1.3.0-rc.1`，完成范围与容量见 [1.3 交付说明](v1.3-delivery.md)。本轮实现 v1.2 的首页身份、轻量 Universe、TXT 精细导入和主要交互，以及 v1.3 的独立札记、角色与集数、私人图片、收藏检索、年度冻结修订和受控成就。数据库由 15 个迁移升级到 18 个；没有新增生产运行语言、服务或 npm 依赖。

| 检查 | 本轮实际结果 |
| --- | --- |
| 静态与生成合同 | `check:web`、`check:api` PASS；OpenAPI 两端生成一致、TypeScript、Go vet 与模块边界；UI 最后调整后补跑 Web 检查 |
| 全量回归及修正 | 执行一次 `npm run verify`：15 项 Node 检查、全部真实 PostgreSQL API 集成及 race 通过，API 包约 306.6 秒；整体命令因 TXT 旧断言仍要求丢弃不同正文的第二条记录而失败。修正为保留三条有效记录后，`npm test`、TXT 与插件定向检查通过；**未把首次全量命令记为 PASS，也未重复已通过的全量数据库套件** |
| 记忆与权限定向 race | PASS，42.4 秒：跨账号拒绝、版本冲突、角色合并/拆分、明确集数快照、私人原件、导入往返、年度冻结与分享撤回、成就授予和幂等补算 |
| 最后受影响检查 | 媒体分页与清理、检索展示定向检查分别 PASS；插件 API 1–2 兼容和 TXT 1.1.1 检查 PASS，约 13.8 秒 |
| 个人 JSON/ZIP | v3 往返 PASS，核对日期精度、重映射后的关系与重定向、可见性、图片原件 SHA、年度内容/修订、成就时间；损坏原件原子拒绝；导入的个人成就不激活全站规则 |
| Linux 容器升级与恢复 | PASS，约 70 秒：从 1.1 升级并保留手账、真实 Worker 投影、图片原件、年度冻结内容、成就；快照恢复到另一实例后完整库深比较及原图字节比较；两个探针均清理 |
| 浏览器真实操作 | Playwright + 系统 Chromium，使用真实 API 与合成账号；札记/角色创建、1,200 集目录与固定快照、有序收藏、检索、图片上传、年度选材/阅读、徽章展示、管理规则/补算预览 PASS |
| 手机及可访问性 | 320/390/768 px 受影响页面无横向溢出；弹窗焦点归还、Universe 减少动效检查 PASS；记忆页与成就后台 axe WCAG A/AA 规则扫描 0 检出，0 pageerror；不是完整无障碍认证 |
| 最终验收容器 | `stage3-review` 已更新，app/worker/db healthy，doctor PASS；原有六条条目的身份、版本和观看进度未变，补充私密合成记忆示例；最终 TXT 1.1.1 在真实 WASI 子进程完成逐记录选择、来源行追溯与追加，保留原评分和笔记 |

最终候选镜像 `sha256:a41b4a6a4d4b16a1c470f5d52cd7d9b54bc5010cf6e5837a718411a4cdbef6df`，完整清单见 `.local/output/release-v13-review-final/release.json`；源码基线 `4eb8b88732a5dc25bfb58bbc0008886fc624afe0` 加工作区修改，因此仍是 `development: true` / `-dirty` 候选，未提交、推送、签发或发布正式版本。

容器完整恢复使用同为 18 个迁移的中间候选 `sha256:a920d0eb8da030205e100be7761c5deabf641f74a7fbe617d11e195894f1bbef`；随后仅调整检索摘要、插件 API 兼容声明/TXT 包版本和前端展示，分别补做受影响检查与最终容器浏览器验证。没有把中间镜像的测试冒充最终镜像重新执行的全量恢复。

证据：`.local/logs/v13-verify.log`、`v13-node-test.log`、`v13-race-scoped.log`、`v13-plugins-final.log`、`v13-json-zip-final.log`、`v13-web-final.log`；`.local/output/v13-rc1-memory-instance-smoke.json`、`v13-review-doctor.json`；浏览器 `.local/output/browser/v13/{report,extended-report}.json` 与 `.local/output/browser/v13-review/report.json`，截图在对应目录。

实际发现并处理：TXT 同范围不同笔记的静默去重；冻结年度私人正文被后续公开状态带出的风险；个人成就导入污染全站规则的风险；媒体列表截断；随附包同版本不可变冲突。上述行为均已修复并补定向回归。一次恢复探针因 Docker vfs 构建缓存占满磁盘失败：只清理可回收构建缓存，保留实例卷、备份和回滚镜像，并修复中断探针的安全清理入口；重跑恢复通过，最终无遗留探针容器。生产 CSP 保持不变，axe 由浏览器调试接口执行。

运行入口为云端回环 `http://127.0.0.1:18082`；记忆库 `/memory`、成就管理 `/admin#achievements`。**该地址本身不是公网访问地址**，用户侧需要端口转发或在本地启动；未配置域名/TLS、公网预览或 VPN。邮件实际收件、正式 OAuth/R2 和 GitHub 签发仍受网络/凭据条件限制，按 [本地交接](local-network-handoff.md) 执行。Windows 原生、VM、约 25 分钟负载、iOS Safari/屏幕阅读器和完整暗色主题未在本轮验证；VM/长时负载继续可选。

## 历史：原路线覆盖复核与 UI 修正（2026-10-08，云端 Linux）

对照三份用户上传路线，使用 Product Design 审查流程，先观察六个真实页面，再修改前端。需求结论见 [覆盖表](roadmap-coverage.md)，完整计划见 [当前路线](refactor-next-steps.md)。本次补回主要产品目标，不恢复旧强制流程；没有实现角色、年度记忆、主题运行时或 PWA。

前端修改：统一受影响的辅助文字/徽标对比度；缩短手账横幅，统计改为轻量横排；私人手账的现有三个导航入口在手机显示为底栏，保留安全区与 52 px 高触控目标。未增加生产依赖、API、数据库表或新的插件权限。

| 检查 | 本次实际结果 |
| --- | --- |
| `npm run check:web` | PASS，5.1 秒；类型、合同及模块边界 |
| Docker 构建与现有实例更新 | PASS；新镜像 `sha256:392214092c4faca3f22a31c1fa365470b30d8bf3363da06aa4e63dcb012715e6`；`stage3-review` 更新完成，保留更新前备份和回滚数据库，六条展示记录仍可查看 |
| 六步真实浏览器审查 | 登录、手账、详情、设置、后台插件、390 px 手机手账，均保存前后截图；使用真实 API，无业务 mock；0 pageerror |
| 自动可访问性 | 修正前五个步骤有 `color-contrast` 发现；修正后六步 axe WCAG A/AA 扫描 0 检出。弹窗原扫描包含背景的重复节点，不按节点数宣称独立缺陷数量 |
| 定向交互 | 23 项检查 PASS：320/390/768/1440 px 页面宽度、手机底栏位置及触控尺寸、各宽度详情初始焦点/Shift+Tab 留在弹窗/Escape 关闭并归还焦点、手机手账与历史切换、减少动效、CSS 200% 放大重排、无浏览器异常 |
| 文档 | 核对当前路线、覆盖表与插件说明的本地链接、差异；未修改既有 52/62 功能统计 |

证据：`.local/output/browser/roadmap-audit/report.html`（自包含六步前后截图）、`capture.json`、`after/capture.json`、`interactions.json`；构建产物为 `.local/output/release-ui-audit/`，日志为 `.local/logs/roadmap-*.log`。本次仍是 `1.1.0-rc.1` / `-dirty` 开发镜像，不是已签发正式版本。

边界：CSS 200% 放大是本次重排检查方式，不等于已人工验收浏览器所有缩放模式；未实测 iOS Safari/屏幕阅读器、完整 WCAG 合规或所有后台状态。验收账号没有绑定真实封面，此次不把装饰回退图视为海报体验已完成。样式/文档改动未重跑全量 Go/PostgreSQL/race、VM 或长时负载；未推送或发布 GitHub 版本。原 1.1 镜像的完整后端/恢复证据保持下方原始记录的适用范围。

## 历史：1.1 预生产候选（2026-10-08，云端 Linux）

源码版本 `1.1.0-rc.1`，范围见 [1.1 交付](v1.1-delivery.md)。本轮实现诊断/配置、持久恢复、v3 快照与兼容性计划、age 加密转移和最小可信发行流程；没有扩大到 1.2 功能。

环境：Debian 13.6 的托管 Linux 容器、amd64、Docker 28.4.0（vfs）、Compose 2.40.3、Node 24.19.0；宿主 Go 1.26.8，生产镜像构建 Go 1.26.6，PostgreSQL 17.11，age 1.2.1，GitHub CLI 2.83.2。这里只证明该 Docker 环境的执行，不声称完成空白 Ubuntu/VM/Windows 宿主安装验收。

| 检查 | 实际结果 |
| --- | --- |
| 完整适用回归 | `npm run verify` PASS，287.4 秒；Web 类型/合同、Go vet、模块边界、15 项 Node 检查、真实 PostgreSQL 集成及 Go race |
| 候选构建与版本 | PASS；一次构建 `animemo-next-v11:20261008`；无数据库/无网络执行 `version` 返回版本、源码和 15 个迁移；镜像归档完整性通过 |
| 实例生命周期 | PASS，12 组检查：初始化、配置预览与冲突拒绝、实际端口切换/中断恢复、无效配置启动失败后恢复、v3 清单与计划、age 正确密钥往返/错误密钥/篡改拒绝、原图与 TOTP 跨实例恢复、损坏快照拒绝、更新副本、回滚、新写入救援、坏候选恢复；3 个探针容器/卷清理完成 |
| Worker / 扩展 / 更新中断 | `test:stage3` PASS，5 组检查；独立 Worker 停止/恢复、真实 WASI 转换、恢复后审阅、更新切换中断救援、坏包隔离；2 个探针清理完成 |
| 恢复与回滚中断 | `node tooling/recovery-smoke.mjs` PASS；恢复中断重新导入到空白数据库，启动阶段中断不覆盖新写入；回滚切换中断保存另一侧的新写入并实际还原验证；3 个探针清理完成 |
| 发行与工作流 | actionlint 1.7.7、YAML、差异与命令检查 PASS；篡改归档和开发候选被拒绝，已有实例的恢复计划返回非零；**未签发真实证明或发布** |
| 现有验收实例 | `stage3-review` 已用同一镜像更新，doctor PASS，app/worker/db 均 healthy；实际登录及 6 条原展示记录核对成功，原镜像/数据库及更新前备份保留 |

镜像 ID：`sha256:3a60c10a3a99ac5ff22552078a52588f4c023b937225eb5f8569ab4505904370`。镜像归档 29,024,768 字节，SHA-256 `17146f3746d54ce251675017718ba4a40dcda896c048a0703f0338b1b338daa4`。源码基线为 `4eb8b88732a5dc25bfb58bbc0008886fc624afe0` 加未提交修改，因此候选明确为 `development: true` / `-dirty`；官方安装入口拒绝把它当正式签发产物。

证据：`.local/logs/v11-*.log`、`.local/output/instance-smoke.json`、`stage3-smoke.json`、`recovery-smoke.json`、`v11-review-check.json`、`v11-review-doctor.json`、`v11-cli-negative-check.json`、`v11-artifact-check.json` 和 `release-v11-dev/`。恢复测试使用合成数据与持久阶段注入，不是断电/损坏磁盘保证。

本轮处理了构建时的磁盘空间不足风险：仅清理确认闲置的构建缓存，未删除实例卷、备份或回滚镜像，检查结束可用约 8 GB。未改 Docker 驱动或全局代理。

剩余限制：GitHub CLI 授权检查未通过，网络允许列表未覆盖全部 API/签名服务；未推送、远端 tag、GitHub Actions 运行、真实来源证明或公开下载验收。接手步骤见 [发行说明](release.md)。真实邮件/OAuth/R2、Windows/VM 和 25 分钟负载均未在本轮执行；后两项始终可选。本轮没有修改产品 UI，沿用前次浏览器验收，不重复截图或跑完整浏览器套件。

## 历史：Linux 基线与更新路线调整（2026-10-08）

当前版本目标和开发规则统一见 [新更新路线](refactor-next-steps.md)。旧上传路线不再作为执行要求；历史验证记录继续保留其日期、代码范围与限制。

本轮修改路线、`AGENTS.md`、README 和相关说明：Linux/Docker 作为主基线，Windows 保留轻量接手；VM 与约 25 分钟负载明确为可选手动专项，不进入默认开发、PR 必需检查或发布前置。已核对 `package.json`、`tooling/run.mjs` 和 `.github/workflows/ci.yml`，当前不存在这两类强制流程，因此没有新增禁用开关或削弱现有数据/权限测试。

本轮验证仅检查文档差异、链接/锚点、命令与现有脚本的一致性；没有修改运行代码、数据库、npm 脚本或 CI 行为，没有重复执行镜像构建、Go/PostgreSQL、浏览器、VM 或长时负载，也没有发布版本。下方先前的运行验证仍按其原始范围解读。

结果：`git diff --check` 通过；11 份修改文档中的 52 处本地链接/锚点有效，引用的 18 个 npm 脚本名称均存在。检查回执保存在 `.local/output/roadmap-doc-check.json`。

## 历史：后台 UI 重设计（2026-10-08，云端）

后台改用独立侧栏、概览、紧凑表格、分区设置和扩展卡片。样式限制在 `admin-shell` 与后台弹窗内，复用既有 API、版本检查和 Base UI 基础组件；未增加生产依赖或修改数据库合同。

| 验证 | 实际结果 |
| --- | --- |
| TypeScript / API 合同 / 模块边界 | `npm run check:web`、`node tooling/check-layout.mjs` PASS |
| 生产镜像与实例更新 | PASS；镜像 `sha256:cd55e424af012b2cf9cb267f0f761fe3b62e1667af2ae36d80fb6b0476adf3ac`；`stage3-review` app、worker、db 均 healthy，更新前备份及回滚库保留 |
| 布局与脚本 | Chromium 151 + Playwright 1.62.1，8 个页面 × 320 / 390 / 768 / 1440 px 均无页面横向溢出；手机表格在自身区域内滚动；0 pageerror |
| 自动可访问性检查 | axe-core 4.11.1，桌面与手机共 16 次 WCAG A / AA 规则扫描，0 检出；不等同于完整 WCAG 合规认证 |
| 用户与内容 | 搜索空状态、账号操作显式选择、Escape 取消与焦点归还、真实内容预览、资源移除必填原因及取消 PASS；未停用真实账号或移除内容 |
| 导航与维护 | 刷新 / 后退保留当前页、跳转正文不切换页面、维护确认可取消 PASS |
| 设置与扩展 | 标签真实创建 / 改色 / 删除、站点原值保存、官方扩展停用 / 恢复、选包审阅并取消 PASS；测试标签清理，原扩展状态恢复 |
| 前台回归 | 桌面实际手账截图及手机宽度检查通过，后台样式未覆盖手账布局 |

本轮修复了自动检查发现的说明文字对比度、手机下拉框可访问名称、表格键盘滚动，以及跳转正文意外重置页面的问题。截图与回执：`.local/output/browser/admin-redesign/`；构建、更新与浏览器日志：`.local/logs/admin-ui-*.log`。本轮只变更前端与浏览器验收脚本，未重复运行此前已通过的全量 Go / PostgreSQL / race 套件，也未验证真实 R2 服务。

复跑浏览器验收时使用合成实例和管理员账号：

```sh
npm install --prefix .local/tools/browser --no-audit --no-fund --ignore-scripts --cache .local/cache/npm playwright@1.62.1 @axe-core/playwright@4.11.1
node tooling/admin-browser.mjs
```

脚本读取 `ANIMEMO_REVIEW_ACCESS` 或 `.local/output/stage3-review-access.json` 中的 `origin/email/password`，通过 `ANIMEMO_BROWSER` 指定系统 Chrome / Chromium 路径。实例需包含至少一条合成番剧和已激活的官方 TXT 扩展；`ANIMEMO_PLUGIN_PACKAGE` 可指定待审阅的本地扩展包，默认 `.local/output/watch-history-text.animemo-plugin`。验收会产生审计记录，短暂切换扩展状态并恢复，不应在真实生产实例上执行。

## 历史：阶段 1–3 实施验收（2026-10-07，云端）

基线为本地提交 `4eb8b88`，范围见 [stage-1-3-delivery.md](stage-1-3-delivery.md)。未把架构子任务算作原项目新增完整功能，62 项分母和本地已确认结果不变。

| 验证 | 本轮实际结果 |
| --- | --- |
| `npm run verify` | PASS，286.3 秒；最终补查 `npm run check` 4.7 秒通过；TypeScript、生成合同一致性、Go vet、Node 回归、真实 PostgreSQL 集成及 Go `-race` |
| 记忆 / 导入定向回归 | PASS；独立本地身份、失效来源映射、月份/未知时间、连载追平、跨账号拒绝、事务回滚不投递、过期 lease 拒绝提交、完整 v2 导出恢复（含移出清单的历史） |
| Docker 构建 | PASS；Core、Web、官方 WASI 包和精确二进制绑定清单生成成功 |
| 实例测试 | PASS；从原云端镜像升级、图片与加密 TOTP 备份恢复、坏快照拒绝、数据库副本迁移、回滚、更新后写入 rescue 再恢复、坏候选自动恢复；三个探针实例均清理 |
| `test:stage3` | PASS；真实插件子进程、停 Worker 后任务只排队、重启继续、恢复扩展默认停用、注入 switching 持久状态后 recover、坏包隔离且 Core 可用；两个探针实例均清理 |
| 真实 Chromium | PASS；1440px 手账、390px 手机布局、Base UI 表单/日期/弹窗、月份记录与追平、历史展示、管理官方身份；无 pageerror / 水平溢出 |
| 插件浏览器流程 | PASS；官方包启用、真实文件上传、TXT 日期/刷次预览、刷新恢复预览、确认导入、停用和手机界面；无 pageerror |
| 持久操作日志 | PASS；跨读取保留阶段、私有文件权限、公开回执不包含配置凭据 |
| 云端验收站 | `stage3-review` app / worker / db 均 healthy，云端回环 `http://127.0.0.1:18082`；未配置用户侧转发或公网预览 |

证据在 `.local/logs/stage3-final-verify.log`、`.local/output/instance-smoke.json`、`.local/output/stage3-smoke.json`、`.local/output/browser/stage3/report.json` 和 `.local/output/browser/plugin-report.json`。截图同目录。完整流程使用镜像 `sha256:d69e7081a248971c6003141b26bffc37d50f82fcf91c531cdc7f0168e206ad57`。最终补齐 OpenAPI 导出身份和日期精度说明后构建为 `sha256:c42e43d3e7dd91b401000bbeede517304d41b7666b1d7593c480acd2de7f2031`，通过真实实例更新；再次检查健康、六条展示数据、v2 导出和官方子进程转换成功（`stage3-final-runtime.json`）。最后这次合同说明变化未重复运行完整竞态套件。

过程中实际发现并修复：月份/未知时间统计错误、完整记忆导入静默去重风险、扩展重新激活缺少摘要复验、Compose profile 漏清理 Worker、Linux 0600 媒体备份无法被镜像默认 UID 读取。修复后重跑对应流程成功，未改备份文件为公开可读，未删除原开发库。

本次未重新验证 Windows 执行、真实 Resend/OAuth/R2 服务、GitHub CI 或公网可访问性。恢复状态测试是持久阶段注入和停止容器，不是断电可靠性证明。浏览器数据均为合成数据；不以截图替代服务端正确性测试。


## 外部集成本地接手（2026-10-07，Windows）

环境：Windows、Node 24.12.0、Go 1.26.6、原生 PostgreSQL 17.11、系统 Chrome。迁移 009–012 新增外部标识、邮件任务、OAuth/同步任务及图片存储。以下为本轮实际验证，后续章节保留云端历史记录。

| 检查 | 实际结果 |
| --- | --- |
| npm run check | PASS：TypeScript、OpenAPI 两端生成一致、Go vet、源码边界检查 |
| npm test | PASS：Go 单元及 Node 原有 9 项；新增环境配置解析回归另行通过 |
| npm run test:api | PASS：真实 PostgreSQL 全量集成，约 69.9 秒；未启用 Windows race |
| 修正后的定向回归 | PASS：资料字段、绑定备份恢复、OAuth、分页重启、R2 迁移及离线恢复 |
| npm run build | PASS：生产 Web 和原生 Go 二进制 |
| Linux / Compose 静态检查 | PASS：CGO=0 的 Linux amd64 二进制交叉编译、独立实例 Compose 配置解析；不等于容器执行验收 |
| 真实 Bangumi | PASS：中文搜索、详情、校验封面；subject 400602 通过 HTTP 导入为私密条目，话数为 28，不混入数据库内特别篇计数 |
| 邮件受控协议 | PASS：注册不预置可登录密码、加密 outbox、稳定幂等重试、均一响应、重复限流、单次使用、TOTP/恢复码、密码修改及旧会话撤销 |
| OAuth / 同步受控协议 | PASS：用户/会话 state 约束、重放拒绝、token 加密和刷新、分页游标恢复、稳定身份去重、双边冲突、小数评分基准、已写入但响应丢失的恢复、逐话进度核对、断开取消 |
| R2 受控 S3 + PostgreSQL | PASS：真实 SDK 签名和固定来源、三种图片、失败上传暂存、私有与分享撤销、原图回迁、重启清理重试、损坏图片拒绝、配额统计不因迁移降低、备份包含原图、无凭据离线恢复且不清理源对象 |
| 浏览器 | PASS：验证邮件链接设置密码及登录、真实搜索/建条目、合成 OAuth 回调/预览/确认导入/断开、管理员 R2 探测/迁移/回切；390px 手机页修复长 endpoint 溢出后文档宽度 390px |

浏览器使用真实 Go API 与隔离数据库；邮件、授权站点、OAuth token/收藏和 R2 为合成服务，不能证明正式服务联调。真实 Bangumi 一次临时失败显示可重试错误，重试后成功且未重复创建。未登录的 auth/me 401 为预期；没有观察到页面脚本异常。截图在 .local/output/external-browser/，只包含测试账号。夹具已停止并清理随机 schema。

浏览器与回归修复：Bangumi eps / total_episodes 区分；JSON/ZIP 导入保留来源身份并按身份去重；断开后不保留“已连接”提示；同步完成页标明预览快照；管理页长地址换行。

真实邮件收件、正式 OAuth/收藏写回、真实 R2 仍缺服务凭据。Docker Linux engine 未就绪，本轮不声称新增 v2 媒体实例备份/更新/回滚容器流程和 Linux race 已通过。已有单独媒体归档恢复测试；容器复测入口为 npm run test:instance。配置与协议来源见 [接入说明](local-network-handoff.md)。

## 插件第一阶段验收（2026-10-07，云端历史）

本阶段新增受限 WASI 插件、TXT 观看记录消费者、管理员本地包管理、导入明细预览及升级兼容检查。完整功能口径仍为 50 / 62（80.6%）；F57 / F59 只记部分完成。使用范围和隔离限制见 `plugins.md`，后续原版能力见 `refactor-next-steps.md`。

| 检查 | 实际结果 | 证据 |
| --- | --- | --- |
| 完整静态 / 合同 / Node / Go 单元与 PostgreSQL `-race` | PASS，197.5 秒；API 包 178.3 秒，插件包 27.4 秒，Node 9 项通过 | `.local/logs/plugin-verify.log` |
| 真实 WASI 沙箱 | PASS：无继承环境 / 文件 / 网络；死循环、取消、输出和内存超限、陷阱、响应协议错误拒绝；私有 stderr 不泄漏 | `server/internal/plugins/runtime_test.go` |
| 包与数据库流程 | PASS：摘要、不兼容声明、同版本不可改写、安装不自动启用、越权拒绝、并发 revision 冲突、版本切回、审计、真实 TXT 预览确认与跨账号隔离 | `server/internal/api/plugins_integration_test.go` |
| 最终插件定向 `-race` | PASS，32.6 秒；补充拒绝有效 WASM 中不存在的 WASI 导入及错误入口签名；真实插件生命周期 / PostgreSQL 回归通过 | `.local/logs/plugin-final-targeted.log` |
| 首个消费者 | PASS：Go WASI 实际编译执行；日期 / 话数 / 刷次 / 笔记保留、同文件事件去重；模糊输入报告错误 | `server/examples/watch-history-text/main_test.go`、`runtime_test.go` |
| 最终 Web 检查与生产镜像构建 | PASS；浏览器发现的选择器标签问题修正后重新构建 | `.local/logs/plugin-web-check.log`、`.local/logs/plugin-image-build.log` |
| 浏览器真实 API | PASS：管理员审阅安装 / 启用，TXT 解析、记录明细、确认前零写入、刷新续办、确认后私密记录、停用 / 恢复；390 px 无横向溢出，页面错误为 0 | `.local/output/browser/plugin-report.json` |
| 实例升级、插件快照和恢复 | PASS，约 63 秒；从无插件的 7 个迁移升级到 8 个迁移，恢复包和状态后实际运行，实例回滚恢复旧包清单和启用版本 | `.local/output/plugin-smoke.json` |
| 真正不兼容的候选镜像 | PASS：在 `.local/tmp` 独立源码副本构建 Host API 2 镜像；已启用插件仅支持 1，候选检查失败，原镜像 / 数据库 / 插件自动恢复健康 | 同上；`.local/logs/plugin-incompatible-build.log` |

最后一轮插件实例测试包含真实浏览器，共创建两个随机 `probe-*` 实例，结束后容器、网络和卷全部移除。此前浏览器定位与 ABI 检查修正期间的失败测试实例也已清理。没有公开预览链接或调度 GitHub CI，没有替换已有开发实例；插件代码仍在工作区，未推送 GitHub。

浏览器初轮发现：注册密码标签含提示文本，测试需按标签前缀定位；插件下拉框的嵌套 label 混入选项文本，已改用明确的 `htmlFor` / `id`，同时补齐移动端表单样式。完整 `verify` 在 Web 标签 / 样式修正及最终 WASI ABI 检查加强之前完成；随后运行最终静态检查、插件相关 `-race`、生产构建和完整浏览器 / 插件实例流程，不把早先的全量测试当作最终改动后重新执行。加强 ABI 检查时，定向测试捕获了错误调用 wazero 宿主模块执行接口导致的 panic；已改用只读函数定义元数据并重跑通过。

当前仍缺：独立 OS worker 隔离、插件持久状态与更多能力、个人插件安装 / 长期授权、在线市场 / 作者投稿、Bridge，以及原版 TXT 导入的多文件 / 编码探测 / 逐项选择 / 已有番剧事件合并。邮件、Bangumi、OAuth、R2 真实服务也未验证。它们不被本次沙箱与实例检查代表。

## 上一阶段：云端功能重构验收（2026-10-07）

以下是插件阶段之前的结果；后文 Windows 第一阶段和封面阶段记录也为历史证据，不代表当前功能上限。当前功能对照为 50 / 62（80.6%），详细范围与剩余 12 项见 `feature-parity.md`。

环境为 Linux、Go 1.26.8、Node 24.19.0、Docker 28.4、Compose 2.40.3、PostgreSQL 17.11、系统 Chromium 151。没有调度 GitHub CI，也没有将本地验收冒充远程状态。

| 检查 | 实际结果 | 证据 |
| --- | --- | --- |
| 完整静态、合同、Node、Go 单元与 PostgreSQL `-race` | 通过，159.4 秒；Go API 包 152.7 秒 | `.local/logs/final-verify.log` |
| 收尾改动静态与定向回归 | 通过；包含静态路由 / 内嵌合同、管理与历史场景，Node 9 项通过 | `.local/logs/final-check.log`、`final-targeted.log`、`final-node-tests.log` |
| 构建、真实容器、应用与数据库同时重建 | 通过，24.6 秒；覆盖原图、登录、记录、幂等、导出及持久导入 | `.local/output/container-smoke.json` |
| 独立实例运维 | 通过，约 72 秒；旧镜像 6 个迁移升级到新镜像 7 个迁移，回滚恢复原 schema | `.local/output/instance-smoke.json` |
| 浏览器账号与手账扩展 | 通过；资料 / 小数评分、批量标签、快捷筛选、重看编辑、日期统计、头像 / 偏好 / 改密 / 全退 / 注销 / 移动端 | `.local/output/browser/foundation-report.json` |
| 浏览器导入、分享、专栏与管理 | 通过；CSV 去重 / 刷新续办、ZIP 跨账号还原、公开审核、目录、链接轮换、私密附件过滤、专栏投稿 / 精选 / 撤回、回收站恢复、审计和移动端 | `.local/output/browser/publication-report.json` |
| 浏览器 JSON 正式导入 | 通过；实际新增条目、保留小数评分、所有导入项私密、不复制失效封面标识 | `.local/output/browser/json-import-report.json` |
| 浏览器安全与维护 | 通过；TOTP 配置、重新生成恢复码、带恢复码登录、恢复码重放拒绝、关闭两步验证、预设应用、健康清理、站点设置保存 / 还原 | `.local/output/browser/security-report.json` |
| 部署镜像内嵌 OpenAPI | 通过，与当前源合同逐项相同 | `.local/output/api-contract-report.json` |

新增集成测试覆盖：账号凭据生命周期、批量事务回滚、跨账号隔离、重看 / 日期边界、持久任务锁释放与恢复、并发 apply 幂等、注入数据库故障后的全批回滚、ZIP 篡改 / 路径 / 符号链接拒绝、撤回分享及原图权限、审核与末位管理员保护、回收站隔离、TOTP 标准向量 / 重放 / 恢复码以及安全维护。

实例验收实际创建独立卷，备份真实账号、图片、观看记录和加密 TOTP。还原后旧 Cookie 失效，密码加恢复码可以登录且原图字节一致。更新前保留原数据库，在副本上迁移；显式回滚先保存新写入，rescue 备份再还原为第三个实例可取回这些写入。故意使用不能运行应用的候选镜像后，旧应用与旧数据库自动恢复健康。三个测试实例的容器、卷、网络均已清理。

本轮发现并修复了带前端静态回退时 Go ServeMux 路由冲突导致的容器启动失败，新增同启回归；另修复了专栏保存关闭后列表未及时刷新和部分无障碍标签定位问题。导入、分享和安全浏览器验收使用真实 API 与合成账号，结束后删除该账号及级联数据；页面运行错误为 0。

当前开发应用及数据库容器均健康。针对 `vfs` 重复快照，按明确的本项目编译步骤和 cache ID 清理了 26 个旧快照，保留镜像、回滚镜像、数据卷和 Go / npm cache mount；可用空间由约 4.8 GiB 恢复到 13 GiB。没有更改 Docker 驱动或平台网络策略。

邮件、Bangumi、外部授权 / 同步、R2 真实网络流程未执行且相关适配尚未实现，原因和本地实施步骤已记录在 `local-network-handoff.md`。当时插件与 Bridge 尚未实现；插件后续进展见本文最新一节，不能计为外部网络验证通过。

## 历史：第一阶段 Windows 验收

日期：2026-10-07。

## 已验证

环境：Windows，Go 1.26.6，Node.js 24.12.0，真实 PostgreSQL 17.11，Chrome。

| 检查 | 结果 | 本轮观察 |
| --- | --- | --- |
| OpenAPI 生成类型一致性、TypeScript、Go vet、模块依赖检查 | 通过 | `npm run check` 约 4.8 秒 |
| 5 个 Go 业务规则测试、6 个 Node 测试 | 通过 | `npm test` 约 1.5 秒，Go 命中缓存 |
| 2 个 PostgreSQL 集成场景及业务规则回归 | 通过 | `npm run test:api` 约 4.8 秒，显式关闭测试结果缓存 |
| 前端生产构建和 Windows Go 程序 | 通过 | 完整 build 约 19.3 秒 |
| Linux amd64 Go 程序交叉编译 | 通过 | 已生成可执行产物，未在 Linux 上运行 |
| Compose 配置 | 通过 | `docker compose config --quiet` |
| 容器基础镜像元数据 | 已读取 | PostgreSQL、Node、Go、distroless 镜像标签可解析 |
| 构建后的 Web 由 Go 提供 | 通过 | 页面 HTTP 200、`/api/ready` 和程序 healthcheck 通过 |

上述耗时是当前第一阶段的小规模单轮观察，不是完整产品的性能基线或云端 CI SLA。

数据库集成场景覆盖：未登录访问拒绝、注册 Cookie、私有响应缓存策略、禁止请求体选择 owner、跨账号读写与导出隔离、版本冲突、并发更新、观看记录幂等与并发重试、进度与统计、搜索通配符转义、分页校验、删除级联、登出令牌失效和再次登录。另验证迁移可重复执行、已应用迁移被修改时拒绝继续。

浏览器通过真实 API 完成：注册后立即进入、登录、退出、切换账号、番剧创建和颜色选择、观看记录、历史列表、编辑后刷新、搜索、卡片 / 列表切换、JSON 下载、删除确认。最后一轮生产构建页面的创建 / 删除 / 历史流程无失败 HTTP 响应，控制台 0 errors / 0 warnings。

桌面在 1440×1000、移动端在 390×844 检查。移动端文档宽度 390，无横向溢出。截图与合成数据导出位于 `.local/output/browser/`，不进入源码包。

本轮修复了浏览器验证发现的账号缓存订阅问题、颜色输入点击遮挡，以及删除后对已删除详情发起多余请求的问题。账号切换问题留有自动回归测试。

## Windows 交接时尚未验证

- 当时 Docker Engine 不可用，因此没有执行完整镜像构建、容器启动、Linux 容器重建留存验收；后续云端结果见下节。
- Linux 交叉编译不能代替 Linux 执行或 `-race` 检查。
- GitHub Actions 的结果以对应提交的工作流记录为准；上述记录只涵盖本地验收，未执行公网部署。
- 后续媒体、外部资料、导入、Worker、Agent 和插件阶段尚未实现。

## 文件与资源

源码、依赖缓存、工具、临时文件、数据库、截图和交接产物均位于项目目录。Windows PostgreSQL 初始化期间使用过指向同一项目目录的临时盘符别名；数据没有移动到其他目录。验收结束已停止本轮启动的应用、浏览器和数据库，并移除该别名；数据库和截图保留在 `.local/`。

## Linux 云端验收

日期：2026-10-07（北京时间）。源码基于 `codex/animemo-next` 的 `7cbe731`，本轮修复在 `codex/animemo-next-cloud-check` 工作区验证。

环境：Debian 13 / Linux amd64、Node.js 24.19.0、Go 1.26.8、Docker 28.4.0（vfs）、Compose 2.40.3、真实 PostgreSQL 17.11、系统 Chromium 151.0.7922.173。镜像构建使用 Node.js 24.12.0 和 Go 1.26.6，运行镜像为 distroless nonroot。

| 检查 | 结果 | 本轮观察 |
| --- | --- | --- |
| `npm run check`、`npm test`、`npm run build` | 通过 | 首轮 Linux 静态检查、原有业务规则及本机构建通过 |
| `npm run verify` | 通过 | 约 18.0 秒；9 个 Node 测试、5 个 Go 单元测试、2 个真实 PostgreSQL 集成场景，Go 开启 `-race` 和 `-count=1` |
| Linux 应用镜像构建 | 通过 | npm / Go 经云端代理及 CA 下载；最终镜像约 13.8 MB |
| `npm run containers` | 通过 | 复用构建缓存约 8.1 秒；应用与数据库均 healthy |
| `npm run test:containers` | 通过 | 复用构建缓存约 24.8 秒；独立 project、端口及数据卷，验收后自动清理 |
| 容器 HTTP 业务 | 通过 | readiness、HTML、JS、CSS、favicon、注册、登出、登录、创建番剧、观看记录 |
| 同时重建应用与数据库容器 | 通过 | 原会话、账号登录、番剧、观看进度与历史保留；幂等重试仍只有一条记录，导出一致 |
| 真实浏览器连接容器 API | 通过 | 注册、创建、记录观看、历史、导出、退出、再次登录、刷新；未 mock API |
| 桌面与移动端 | 通过 | 1440×1000、390×844；移动端文档宽度 390，无横向溢出；无页面异常、无非预期 HTTP / 控制台错误 |
| 失败与取消清理 | 通过 | 3 个回归测试覆盖构建失败、取消、清理失败；只操作本轮生成的 project，并保留错误信息 |

耗时来自当前单轮观测，缓存状态不同不能作为严格前后性能对比。`verify` 让 CI 中的静态、PostgreSQL 与 race 检查在开发环境直接运行；容器验收不需要先重复本机构建。

本轮修复与边界：

- 环境中的系统 `go` 原为围棋程序；已校验并安装项目内工具链，脚本自动识别 `.local/tools/go/bin/go`。
- 开发脚本原先覆盖 Docker 配置目录，丢弃云端已有代理与认证配置。现保留宿主配置；CA 只在构建期间挂载，最终镜像不包含 session CA 挂载。
- vfs 构建曾因旧诊断缓存和大型 Go bookworm 阶段耗尽 32GB 磁盘；已清理本任务无用缓存并使用较小的 Alpine Go 构建阶段，不改 Docker daemon 存储驱动。
- 浏览器发现 favicon 返回 403：云端 checkout 中静态文件权限为 `0600`，复制到最终镜像后原属主为 root。现将 Web 产物归属 nonroot 运行用户，并在容器验收中请求首页引用的所有资源；修复后 favicon HTTP 200。
- 浏览器下载域名 `cdn.playwright.dev` 未在云端 allowlist 中，本轮使用已安装系统 Chromium；指定版本的 Playwright 下载仍需环境配置放行。
- Windows 环境脚本本轮仅做相同的 Docker 配置保留和本地 Go 路径调整，未重新进行 Windows 执行验收；未触发 GitHub Actions 或公网部署，后续产品阶段未纳入本轮。

日志与机器可读结果分别保存在 `.local/logs/verify.log`、`.local/logs/container-smoke.log`、`.local/output/container-smoke.json` 和 `.local/output/browser/report.json`；桌面与移动端截图在 `.local/output/browser/`。隔离验收资源和浏览器合成账号已清理；开发应用与数据库保留运行，数据库位于 `.local/data/compose-postgres`。

## 第二阶段：私有封面

日期：2026-10-07。沿用上述 Linux 环境，新增 Go 标准库图片校验、PostgreSQL 原图存储、私有 API 和详情 / 卡片操作，无新增运行时依赖。

| 检查 | 结果 | 本轮观察 |
| --- | --- | --- |
| `npm run verify` | 通过 | 50.3 秒；9 个 Node 测试、6 个 Go 单元测试、6 个真实 PostgreSQL 集成场景；Go 开启 `-race` 和 `-count=1` |
| 图片输入边界 | 通过 | JPEG / PNG、原始字节保留、空文件、格式伪装、损坏内容、超过 2 MiB、超过单边及总像素限制 |
| 私有图片 API | 通过 | 匿名拒绝、跨账号 GET / PUT / DELETE 隔离、来源校验、私有缓存响应头、旧 revision 失效、退出后拒绝读取 |
| 事务与并发 | 通过 | 两个独立 HTTP handler 同版本上传仅一个成功；同账号不同条目争用最后一份容量时仅一个成功；失败替换保留原图和版本；移除释放配额；条目删除级联清图 |
| 上传并发上限 | 通过 | 两个处理名额占满时，新请求在读取图片前返回 503 / Retry-After，不释放其他请求的名额 |
| 第一阶段数据升级 | 通过 | 在随机 schema 中重建第一阶段结构后应用 002，原会话、条目、版本及观看记录保留，并可上传封面 |
| `npm run test:containers` | 通过 | 36.4 秒；完整镜像构建，重建应用和数据库后逐字节比较图片，验证会话、手账、历史、导出与幂等性；登出及移除后图片不可读；隔离资源清理完成 |
| `npm run containers` | 通过 | 8.3 秒；开发数据库自动升级，应用和数据库 healthy |
| 真实 Chromium 浏览器 | 通过 | 界面拒绝不支持的格式，PNG 上传、JPEG 替换、记录观看、导出、退出 / 登录 / 刷新后图片可见、取消及确认移除、历史和进度保留 |
| 桌面和手机 | 通过 | 1440×1000、390×844；文档及详情无横向溢出；浏览器无异常、无非预期 HTTP / 控制台错误 |

本次验证日志覆盖更新 `.local/logs/verify.log` 和 `.local/logs/container-smoke.log`。浏览器脚本、报告与截图分别是 `.local/output/browser/cover-check.mjs`、`cover-report.json`、`cover-desktop.png`、`cover-mobile.png`、`cover-detail-desktop.png` 和 `cover-detail-mobile.png`；全部仅使用合成账号及图片，结束后账号与关联数据已清理。

本阶段范围是手动上传私有封面。JSON 导出只有文字与封面 revision 元数据，不含图片，界面已提示；数据库备份包含图片。未实现外部资料源、导入、Worker 或完整备份恢复流程。本轮未重新验证 Windows，也未提交、推送或触发 GitHub Actions。
