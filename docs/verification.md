# 开发验收记录

## 外部集成本地接手（2026-10-07，最新）

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
