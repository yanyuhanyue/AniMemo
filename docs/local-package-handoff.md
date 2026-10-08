# 本地交接指南：1.3.0-rc.2

原始交接包交付 `1.3.0-rc.2` 完整源码、Linux amd64 开发候选镜像、相对 Git 基线的完整补丁，以及已有验收记录。原包生成时这些改动尚未提交；本机接手后的源码与运维修复通过 GitHub `codex/animemo-next` 分支交接。原始包不会随分支更新，继续开发时使用分支中的最新工具和文档。该包不是正式签发版本，也不是包含所有依赖的离线安装器。

产品定位：记录看过的番剧与个人回忆，不做追番排期、更新提醒或观看目标。只填作品名、未知日期的回忆都是有效记录，不推测完成状态、日期、集数或刷次。不兼容旧项目数据。后续以 [当前路线](refactor-next-steps.md) 为准。

## 包内内容与放置

解压到一个新目录，例如 `E:\番剧记录\AniMemo-handoff-1.3.0-rc.2`，先保留原工作区。目录中的 `source/` 就是完整项目根目录；不要把外层交接目录当成项目，也不要直接覆盖仍有本地改动的旧目录。

- `source/`：源码、依赖锁文件、部署配置和文档；包含未提交的新文件及迁移 013–019，共 19 个迁移。
- `candidate/`：`release.json` 和 `animemo-image.tar`；已编译前端、Go 服务/Worker 与随附 TXT 扩展。
- `changes/`：基线信息、完整二进制补丁、文件变动清单；补丁包含新增文件和删除文件。
- `evidence/`：已有 Linux 验收日志、报告与合成数据截图。时间戳保持原始记录，不能当作 Windows 本机通过记录。
- `manifest.json`、`verify-package.mjs`：包内文件 SHA-256 校验；只证明完整性，不代表正式发行签名。

不包含 `.git`、依赖缓存、Go/Node 安装包、PostgreSQL 镜像、云端账号、数据库备份、会话或实例密钥。首次启动创建新实例，需要自行建立管理员账号；截图不是附赠数据库。源码和镜像保持同一应用实现，本次打包仅补充交接文档。

## Windows：先直接验收镜像

需要 Node.js **24.12 或更新版本**、Docker Desktop 的 **Linux containers/WSL2 engine**、Docker Compose v2；只看网站无需安装 Go 或执行 `npm ci`。本镜像是 Linux amd64，其他架构未验收。Windows 本机接手结果见 [验收记录](verification.md)。以下加载命令需要本地接手修复后的源码工具。

在解压后的外层目录打开 PowerShell，逐条执行；命令报错时先解决该项：

```powershell
node --version
node .\verify-package.mjs
docker info
docker compose version
docker pull postgres:17.11-bookworm
Set-Location .\source
$handoffLoaded = node tooling/release.mjs load --directory ../candidate --development | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw '候选镜像加载失败' }
node tooling/instance.mjs install --name local-review --image $handoffLoaded.manifest.local_image --port 18082
node tooling/instance.mjs doctor --name local-review
node tooling/instance.mjs setup-code --name local-review
```

在同一台本地电脑打开 **http://127.0.0.1:18082/setup**，输入最后一条命令在交互终端显示的初始化码，创建自己的管理员。以后访问 **http://127.0.0.1:18082**。无需云端账号；地址只监听本机。`trust_verified: false` 是这个未签发开发候选的预期结果，因此使用明确的本地 `--image`，不能把它当正式 `--release` 安装。

加载器核验归档、配置摘要、平台和版本，并返回当前 Docker engine 可用的 `local_image`；经典 Docker 与 containerd 的镜像 ID 可能不同，不能直接将包内 `image` 当本机 ID 使用。

如果端口占用，可在首次安装时改为 `--port 18084`，访问地址同步调整；不要停用不明用途的现有服务。实例名已存在时使用原实例的 `start`，或为新实例换一个名字；`install` 不覆盖已有实例。数据库镜像下载失败时，在本地配置可用的 Docker 网络或预先加载相同标签镜像；不要依赖云端代理地址。

```powershell
# 以下命令均在 source 目录执行，停止不会删除数据
node tooling/instance.mjs stop --name local-review
node tooling/instance.mjs start --name local-review
node tooling/instance.mjs status --name local-review
```

实例配置位于 `source/.local/instances/local-review/`，数据库位于 Docker volume。以后需要转移实例时使用 [完整备份/恢复](instance-operations.md)，保留加密密钥；仅复制源码不会迁移用户数据。

## Linux / WSL2

可以在外层目录使用以下命令。Node 和 Docker 必须在执行命令的同一环境可用；在 WSL 内开发时建议放在其 Linux 文件系统。

```bash
node verify-package.mjs
docker info
docker compose version
docker pull postgres:17.11-bookworm
cd source
handoff_image=$(node --input-type=module -e 'import {loadDevelopmentRelease} from "./tooling/release.mjs"; console.log((await loadDevelopmentRelease("../candidate")).local_image)')
node tooling/instance.mjs install --name local-review --image "$handoff_image" --port 18082
node tooling/instance.mjs doctor --name local-review
node tooling/instance.mjs setup-code --name local-review
```

## 继续改代码

在 `source/` 安装原生 Node.js 24.12+、Go **1.26.6+**；保留 Docker 运行 PostgreSQL。Windows PowerShell 可先执行 `. .\tooling\env.ps1`，把缓存和临时文件放回项目 `.local/`，然后：

```text
npm ci --ignore-scripts
npm run db:start
npm run dev
```

开发页面 **http://127.0.0.1:5177**，API 默认端口 18081，数据库默认端口 55432。它与上述 18082 的验收实例使用独立数据；改源码不会自动更新验收镜像。首次管理员初始化按 [项目 README](../README.md) 中的开发说明操作。若本机端口冲突，先查看 [云端/本地开发说明](cloud-development.md)。

日常按改动选择 `npm run check`、`npm test`，涉及数据事务再运行 `npm run test:api`。无需每次跑 `npm run verify`；VM 和约 25 分钟负载均可选。浏览器自动化工具位置与指定本机 Chrome 的参数见 [1.3 交付](v1.3-delivery.md)，不为只查看网站安装 Playwright。

本包没有 `.git`，可直接继续开发，版本会明确标为未知来源开发构建。若要保留原仓库历史，使用包内 `changes/baseline.json` 指定的基线和 `all-changes.patch`，在已保护本地改动、基线一致的独立干净工作区执行 `git apply --check --binary <补丁路径>`，通过后再 `git apply --binary <补丁路径>` 并审阅提交。补丁已包含新增文件；若基线不同或检查失败，先对比 `source/`，不要强制覆盖。本包未包含 Git 历史；正式发行需要回到已审核提交，不能把 `unknown-dirty` 当正式源码身份。

## 接手验收与下一步

1. 只填作品名保存：默认私密、状态为“看过，细节未记”，没有虚构观看日期或完成统计。
2. 卡片“留点回忆”：保存未知日期的文字，之后能搜索、编辑；详情可另补封面、Bangumi 资料或明确观看事实。
3. 按需试用札记、私人瞬间图片、收藏、年度册；角色/集数/徽章在更多工具中。核对分享撤销后不可再读取。
4. 使用合成记录验证 JSON/ZIP 带走和空手账导入；真正使用前按实例文档做一次本机备份与新实例恢复。
5. 优先修复这条记录与回顾主链的体验问题。再按路线推进 1.4 的一个真实消费者，交付其需要的个人授权或有限主题插槽；当前还没有第三方主题运行时、通用 SDK/市场或完整 Bridge。

已有 Linux 证据：静态/契约检查、Node/Go 单测、真实 PostgreSQL 集成、定向 race、容器升级和完整恢复、最终候选的七项浏览器主链与手机检查。详细范围见包内 `evidence/README.md` 和 [验收记录](verification.md)。本次打包只复核文件、补丁、链接和镜像完整性，没有重复全量功能测试。

仍待本地完成：邮件真实收件、Bangumi OAuth 授权与收藏写回、真实 R2 存储桶、域名/TLS 和 GitHub 签发/发布。云端受限网络不覆盖全部服务，且没有正式凭据；受控接口通过不能替代上述验收。步骤、参数和记录要求见 [外部服务交接](local-network-handoff.md) 与 [发行说明](release.md)。未配置的外部能力继续关闭，不阻塞本地核心功能。
