# 本地交接指南：1.3.0-rc.4

当前完整交接包提供 `1.3.0-rc.4` 源码、Linux amd64 开发候选镜像、相对本机 RC2 提交的累计补丁、相对上一份 RC4 源码包的收尾补丁，以及验收记录。镜像绑定已提交源码；包内 `handoff.json` 分别记录镜像提交与文档/探针收尾提交。它仍是未签发的候选，不是正式 Release，也不包含全部离线依赖。原 RC2 包及此前成就补丁保留原样，不应混用旧镜像代替本包。

产品定位：记录看过的番剧与个人回忆，不做追番排期、更新提醒或观看目标。只填作品名、未知日期的回忆都是有效记录，不推测完成状态、日期、集数或刷次。不兼容旧项目数据。后续以 [当前路线](refactor-next-steps.md) 为准。

## 包内内容与放置

解压到一个新目录，例如 `E:\番剧记录\AniMemo-handoff-1.3.0-rc.4`，先保留原工作区。目录中的 `source/` 就是完整项目根目录；不要把外层交接目录当成项目，也不要直接覆盖仍有本地改动的旧目录。

- `source/`：源码、依赖锁文件、部署配置和文档；包含迁移 001–021，共 21 个迁移。
- `candidate/`：`release.json` 和 `animemo-image.tar`；已编译前端、Go 服务/Worker 与随附 TXT 扩展。
- `changes/`：基线信息、从 `b61a63b` 开始的累计补丁与上一份 RC4 源码包的增量补丁；补丁包含新增文件和删除文件。
- `source.bundle`：本轮提交及祖先的 Git bundle，用于保留准确提交身份；不包含工作区配置或 stash。
- `evidence/`：已有 Linux 验收日志、报告与合成数据截图。时间戳保持原始记录，不能当作 Windows 本机通过记录。
- `manifest.json`、`verify-package.mjs`：包内文件 SHA-256 校验；只证明完整性，不代表正式发行签名。

不包含 `.git`、依赖缓存、Go/Node 安装包、PostgreSQL 镜像、云端账号、数据库备份、会话或实例密钥。首次启动创建新实例，需要自行建立管理员账号；截图不是附赠数据库。源码和镜像保持同一应用实现，镜像构建后仅完善验收探针和交接文档，不为这些改动重建应用。

## Windows：先直接验收镜像

需要 Node.js **24.12 或更新版本**、Docker Desktop 的 **Linux containers/WSL2 engine**、Docker Compose v2；只看网站无需安装 Go 或执行 `npm ci`。本镜像是 Linux amd64，其他架构未验收。历史 RC2 的 Windows 接手结果见 [验收记录](verification.md)，RC4 尚未在 Windows 本机重跑。请使用本包中的源码工具执行以下命令。

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
node tooling/instance.mjs install --name rc4-review --image $handoffLoaded.manifest.local_image --port 18084
node tooling/instance.mjs doctor --name rc4-review
node tooling/instance.mjs setup-code --name rc4-review
```

在同一台本地电脑打开 **http://127.0.0.1:18084/setup**，输入最后一条命令在交互终端显示的初始化码，创建自己的管理员。以后访问 **http://127.0.0.1:18084**。无需云端账号；地址只监听本机。`trust_verified: false` 是这个未签发开发候选的预期结果，因此使用明确的本地 `--image`，不能把它当正式 `--release` 安装。

加载器核验归档、配置摘要、平台和版本，并返回当前 Docker engine 可用的 `local_image`；经典 Docker 与 containerd 的镜像 ID 可能不同，不能直接将包内 `image` 当本机 ID 使用。

如果端口占用，可在首次安装时改为 `--port 18084`，访问地址同步调整；不要停用不明用途的现有服务。实例名已存在时使用原实例的 `start`，或为新实例换一个名字；`install` 不覆盖已有实例。数据库镜像下载失败时，在本地配置可用的 Docker 网络或预先加载相同标签镜像；不要依赖云端代理地址。

```powershell
# 以下命令均在 source 目录执行，停止不会删除数据
node tooling/instance.mjs stop --name rc4-review
node tooling/instance.mjs start --name rc4-review
node tooling/instance.mjs status --name rc4-review
```

实例配置位于 `source/.local/instances/rc4-review/`，数据库位于 Docker volume。以后需要转移实例时使用 [完整备份/恢复](instance-operations.md)，保留加密密钥；仅复制源码不会迁移用户数据。

## Linux / WSL2

可以在外层目录使用以下命令。Node 和 Docker 必须在执行命令的同一环境可用；在 WSL 内开发时建议放在其 Linux 文件系统。

```bash
node verify-package.mjs
docker info
docker compose version
docker pull postgres:17.11-bookworm
cd source
handoff_image=$(node --input-type=module -e 'import {loadDevelopmentRelease} from "./tooling/release.mjs"; console.log((await loadDevelopmentRelease("../candidate")).local_image)')
node tooling/instance.mjs install --name rc4-review --image "$handoff_image" --port 18084
node tooling/instance.mjs doctor --name rc4-review
node tooling/instance.mjs setup-code --name rc4-review
```

## 继续改代码

在 `source/` 安装原生 Node.js 24.12+、Go **1.26.6+**；保留 Docker 运行 PostgreSQL。Windows PowerShell 可先执行 `. .\tooling\env.ps1`，把缓存和临时文件放回项目 `.local/`，然后：

```text
npm ci --ignore-scripts
npm run db:start
npm run dev
```

开发页面 **http://127.0.0.1:5177**，API 默认端口 18081，数据库默认端口 55432。它与上述 18084 的验收实例使用独立数据；改源码不会自动更新验收镜像。首次管理员初始化按 [项目 README](../README.md) 中的开发说明操作。若本机端口冲突，先查看 [云端/本地开发说明](cloud-development.md)。

日常按改动选择 `npm run check`、`npm test`，涉及数据事务再运行 `npm run test:api`。无需每次跑 `npm run verify`；VM 和约 25 分钟负载均可选。浏览器自动化工具位置与指定本机 Chrome 的参数见 [1.3 交付](v1.3-delivery.md)，不为只查看网站安装 Playwright。

`source/` 不含 `.git`。需要精确 Git 身份时，可在外层目录执行 `git clone source.bundle checkout`，从 `checkout/` 继续开发；它包含的是本轮候选分支，远端 GitHub 尚未推送。仅在 `source/` 直接构建时，版本会明确标为未知来源开发构建。

继续使用原 GitHub 工作区时，先检查 `git status` 并保护本地改动。按 `changes/baseline.json` 选择一个补丁：`from-rc2.patch` 对应本机已推送的 `b61a63b`；`rc4-closeout.patch` 对应上一份 RC4 成就源码包。先执行 `git apply --check --binary <补丁路径>`，通过后再 `git apply --binary <补丁路径>` 并审阅提交。不要同时应用两份补丁；基线不一致或检查失败时对照 `source/`，不能强制覆盖。

### 升级已有 RC2 实例

在保存该实例 `.local/instances/` 的**原项目目录**操作。先接入上述工具/源码补丁，再加载本包镜像；以下 PowerShell 示例中的 `$rc4Candidate` 需填写解压后 `candidate` 的绝对路径，`local-review` 换成已有实例的真实名称：

```powershell
$rc4Candidate = 'E:\番剧记录\AniMemo-handoff-1.3.0-rc.4\candidate'
$rc4Loaded = node tooling/release.mjs load --directory $rc4Candidate --development | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'RC4 镜像加载失败' }
node tooling/instance.mjs update --name local-review --image $rc4Loaded.manifest.local_image
if ($LASTEXITCODE -ne 0) { throw '升级失败，请查看恢复回执；不要再次初始化实例' }
node tooling/instance.mjs doctor --name local-review
```

更新工具自动保存更新前快照，并在新数据库执行迁移；失败恢复原镜像与数据库。显式回滚和升级后新写入的保留方式见 [实例运维](instance-operations.md)。不要在解压后的另一个项目目录用相同实例名重新 `install`，也不要只复制实例配置来转移数据。

## 接手验收与下一步

1. 只填作品名保存：默认私密、状态为“看过，细节未记”，没有虚构观看日期或完成统计。
2. 卡片“留点回忆”：保存未知日期的文字，之后能搜索、编辑；详情可另补封面、Bangumi 资料或明确观看事实。
3. 按需试用札记、私人瞬间图片、收藏、年度册；角色/集数/徽章在更多工具中。核对分享撤销后不可再读取。
4. 使用合成记录验证 JSON/ZIP 带走和空手账导入；真正使用前按实例文档确认自己的备份存放和恢复路径。
5. 优先修复这条记录与回顾主链的体验问题。再按路线推进 1.4 的一个真实消费者，交付其需要的个人授权或有限主题插槽；当前还没有第三方主题运行时、通用 SDK/市场或完整 Bridge。

已有 Linux 证据：静态/契约检查、Node/Go 单测、真实 PostgreSQL 集成、定向 race、容器升级和完整恢复、最终候选的七项浏览器主链与手机检查。详细范围见包内 `evidence/README.md` 和 [验收记录](verification.md)。本轮收尾实际执行范围以最新验收记录为准；原 RC2 的 Windows 通过记录不能算作 RC4 的本机验证。

仍待本地完成：邮件真实收件、Bangumi OAuth 授权与收藏写回、真实 R2 存储桶、域名/TLS 和 GitHub 签发/发布。云端受限网络不覆盖全部服务，且没有正式凭据；受控接口通过不能替代上述验收。步骤、参数和记录要求见 [外部服务交接](local-network-handoff.md) 与 [发行说明](release.md)。未配置的外部能力继续关闭，不阻塞本地核心功能。
