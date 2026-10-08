# 新实例、备份、更新与回滚

本项目按新实例开发，不迁移 Django / SQLite / 旧版 Docker 布局。运维工具运行在部署主机，应用进程没有 Docker socket、系统命令或更新权限。Linux + Docker 是首批部署基线；当前运维工具需要 Node.js 24.12+ 和 Docker Compose 2.24.4+。Windows 保留 Docker Desktop Linux 容器下的本地开发与接手用法，不据此承诺原生生产支持。

本文描述 1.1 候选的当前可执行能力。实施范围见 [1.1 交付记录](v1.1-delivery.md)，官方身份验证与发行接手见 [发行说明](release.md)。本地 `--image` 安装只固定镜像身份；`--release` 才验证官方来源。

## 安装与服务配置

先按 README 构建镜像：`npm run db:start`、`npm run containers`。这会生成本地 `animemo-next-app` 镜像。也可在有网络的主机拉取经自己审核的发行镜像，或使用 `docker save` / `docker load` 离线转移。安装器只使用已经存在的本地镜像，不会自动从任意 URL 下载或执行脚本。

```sh
npm run instance -- doctor --port 18082
npm run instance -- install --name home --image animemo-next-app --port 18082
npm run instance -- status --name home
npm run instance -- setup-code --name home
```

打开 `http://127.0.0.1:18082/setup`，输入本地交互终端显示的初始化口令，创建第一位管理员。口令不能被管道或重定向输出；不要把口令、验证器密钥或恢复码发到聊天中。

每个实例拥有独立 Compose project、数据库卷、随机数据库密码、初始化口令、AES-256 实例密钥和固定的镜像 SHA-256 ID。配置在 `.local/instances/home/config.json`，写入权限 0600，目录 0700；Windows 文件系统应为当前用户设置相应 ACL。配置含密钥，不进 Git、不做公开附件。不要自行更换 secretKey，否则现有两步验证密钥无法解密。

默认端口只绑定回环地址，支持用 `--bind` 明确指定宿主 IP。外部部署用 `--origin https://anime.example.com` 并在前面配置同源 HTTPS 反向代理；不要把数据库端口暴露到公网。非回环 HTTP 来源会被拒绝。端口、来源、镜像与数据库配置以该实例配置为准，页面中的站点名、简介和注册开关在管理员“站点设置”维护。

```sh
npm run instance -- stop --name home
npm run instance -- start --name home
```

停止保留数据。安装不会覆盖同名实例。工具没有生产实例删除命令；测试清理函数仅接受随机 `probe-<12 hex>` 实例名。

## 诊断与配置预览

```sh
npm run instance -- doctor --name home
npm run instance -- configure --name home --port 18084 --origin https://anime.example.com
npm run instance -- configure --name home --port 18084 --origin https://anime.example.com --apply
```

不带 `--apply` 只显示前后监听/来源及重启影响，不写配置、不停服务、不打印凭据。`--bind 0.0.0.0` 会对所有 IPv4 接口监听，预览明确标出外部监听；使用 HTTPS 反向代理并自行维护防火墙。已有 HTTPS Origin 不因端口变化被改写；默认回环 Origin 随默认端口调整。工具不修改 DNS/TLS。

应用前检查新地址/端口能否绑定，配置用私有临时文件、同步写入及原子替换保存。运行实例的 Web/Worker 健康失败会恢复原配置；停止中的实例保持停止，报告健康检查推迟到启动时。`doctor` 的 FAIL 返回非零退出码；空间报告是当前项目文件系统的可用量，不代替数据库增长容量规划。它检查本机实例，不证明公网 DNS/TLS 可达。


## 整个实例备份与跨主机恢复

```sh
npm run instance -- backup --name home
```

输出目录位于 `.local/output/backups/`，包含：

- `database.dump`：PostgreSQL custom-format 一致性快照，包含账号、图片、观看记录、专栏、审计及持久任务。
- `secrets.json`：原始实例密钥、初始化口令及已配置的外部服务凭据；与数据库同级保护。
- `media.zip`：包含当前引用的图片和校验清单，恢复不需连接原 R2。此前本项目 v1/v2 快照仍可读取；与旧 Django 项目兼容无关。
- `manifest.json`：v3 格式记录产品版本、源码/平台、原镜像 ID、PostgreSQL 主版本、全部已应用迁移及摘要、扩展各版本/摘要/启用状态、媒体归档方式和每个文件字节数与 SHA-256。

备份会短暂停止应用，完成数据库及媒体导出后恢复原来的运行状态。失败目录带 `INCOMPLETE` 标记，不能用于恢复。本机快照仍是 0700 目录/0600 文件的明文工作副本，保存在受保护的磁盘；跨主机转移使用下文的 age 加密入口。校验和检测损坏，不证明备份来源可信，只恢复自己信任的备份。

先按下文加密转移并解密到私有目录，同时转移原应用镜像；数据库镜像为 PostgreSQL 17。先载入相同应用镜像、查看恢复计划，再恢复到一个**新的实例名和空白卷**：

离线主机还需预先载入 `postgres:17.11-bookworm`，并准备 Node、Docker/Compose 和 age；1.1 不提供整套宿主依赖的离线安装包。

```sh
npm run instance -- restore-plan --name restored --backup .local/output/backups/home-实际目录
npm run instance -- restore --name restored --backup .local/output/backups/home-实际目录 --port 18083
npm run instance -- status --name restored
```

`restore-plan` 只检查快照和本地镜像，展示兼容性、迁移/扩展清单及会改变的状态，不创建实例或卷。恢复会再次执行计划检查，校验文件、大小、SHA-256、密钥和镜像/迁移身份，再创建资源；不接受覆盖现有实例，也不接受用不同应用镜像直接恢复。恢复完成后再单独更新应用。数据库导入后比较实际迁移和扩展清单，不匹配时拒绝启动应用。恢复会撤销旧登录和未完成的验证器设置；账号密码、已启用的验证器、未使用恢复码、原始图片和收藏仍保留。

新实例恢复会把远端图片写回 PostgreSQL 并建立新对象目录，不清理源存储桶；同时撤销邮件令牌和待发邮件、OAuth 连接、未完成收藏同步。外部凭据默认不激活，需重新配置并授权。个人 ZIP 备份只含一个用户的手账与封面，不能代替这里的实例备份。普通导入任务仍可能继续执行，迁移生产实例时应先停止旧实例。

## 加密跨主机转移

新备份额外记录可移植的镜像配置摘要 `image_config`。经典 Docker 与 containerd 可为同一镜像显示不同 ID：在目标主机加载原归档后，将加载结果中的 `local_image` 同时传给 `restore-plan` 和 `restore` 的 `--image`。工具核对配置摘要、应用来源和迁移清单；不接受其他版本镜像。旧经典 Docker 备份可用原 `image` 作为配置摘要核对。

使用可信的 age / age-keygen 1.2.1 或兼容版本。工具查找 `ANIMEMO_AGE`、项目 `.local/tools/age/age`，最后才使用 PATH；Windows 为相应 `.exe`。有 Go 的开发环境可以安装到项目内，不需要全局软件或常驻服务：

```sh
source tooling/env.sh
mkdir -p .local/tools/age
GOBIN="$ANIMEMO_LOCAL/tools/age" go install filippo.io/age/cmd/age@v1.2.1 filippo.io/age/cmd/age-keygen@v1.2.1
```

Windows 本地的等效入口（本轮未在 Windows 执行）：

```powershell
. .\tooling\env.ps1
$env:GOBIN = Join-Path (Get-Location).Path '.local\tools\age'
New-Item -ItemType Directory -Force -Path $env:GOBIN | Out-Null
go install filippo.io/age/cmd/age@v1.2.1 filippo.io/age/cmd/age-keygen@v1.2.1
```

接收方使用 `age-keygen -o 私钥文件` 生成并妥善保存身份，再用 `age-keygen -y 私钥文件` 导出公钥至 recipients 文件。私钥不能与加密包一起交付或放进 Git；丢失私钥无法解密。支持 age recipients 文件中的多个接收方。

```sh
npm run instance -- backup-export --backup .local/output/backups/home-实际目录 --recipients-file .local/recipients.txt --output .local/output/home-transfer
npm run instance -- backup-open --archive .local/output/home-transfer --identity-file .local/identity.txt --output .local/output/backups/home-opened
npm run instance -- restore-plan --name restored --backup .local/output/backups/home-opened
npm run instance -- restore --name restored --backup .local/output/backups/home-opened --port 18083
```

转移整个加密目录，其中包括加密清单、数据库、媒体、实例及外部凭据；文件名固定，不能借归档写到任意路径。输出目录必须不存在，不覆盖既有备份。错误私钥、密文篡改或校验失败时拒绝恢复并清理本次解密目录。正常解密后的目录保持私有，恢复完成后按自己的保留策略处理；进程被强杀时未完成目录也需人工检查，不把部分文件当有效快照。

## 更新与自动故障恢复

先在开发或测试实例验证新镜像，保留本地旧镜像，再执行：

```sh
npm run instance -- update --name home --image 已审核的新本地镜像名
```

更新的行为是：停止应用并等待退出 → 备份旧数据库与实例密钥 → 创建新的数据库副本并还原 → 撤销旧登录 → 候选镜像只读检查已启用插件兼容性与完整性 → 使用新镜像启动并运行迁移 → 等待健康检查。

旧数据库不运行新迁移。新镜像启动或健康检查失败时，工具自动恢复原配置、原镜像和原数据库。若 Docker 本身不可用，自动恢复也可能失败；错误会明确提示，原数据库与备份仍保留，需要恢复 Docker 后重新启动。操作期间应用短暂停机，不提供不停机升级。

成功后配置记录上一版镜像和数据库，以供回滚。镜像以不可变 ID 固定，不受同名 tag 覆盖影响。不要清理仍被实例配置或 rollback 引用的镜像。保留的旧数据库占用磁盘，当前不做自动删除；先核实备份与回滚不再需要，再由部署者安排清理。

## 显式回滚

回滚恢复到更新前的数据库时间点，更新后写入不会自动合并。工具会先备份当前版本及新增数据，再执行切换；必须显式带上参数：

```sh
npm run instance -- rollback --name home --confirm-discard-new-writes
```

回滚从更新前完整备份还原到新数据库，离线恢复图片并取消未完成外部任务，需要重新连接外部账号。它不会撤销已在外部服务完成的写入。输出的 rescue 目录可恢复成另一个新实例以取回更新后的数据。没有该参数时不会停止服务或切换数据库。只提供最近一次成功更新的快捷回滚；更早版本通过备份恢复为新实例处理。

## 验证与故障排查

```sh
npm run test:instance
```

独立随机实例验收会实际安装、创建账号 / 图片 / 观看记录 / 两步验证，备份后还原到第二个实例，校验恢复后的凭据与原图，拒绝损坏备份，更新到数据库副本，回滚，并从自动 rescue 备份取回更新后新增记录；还会使用一个不能启动应用的候选镜像验证自动恢复。仅清理自己的随机容器和卷，报告写入 `.local/output/instance-smoke.json`。

验收需先安装 age / age-keygen。默认用当前应用镜像测试，包含真实加密、错误密钥/篡改拒绝、配置预览/失败恢复、doctor、备份计划及中断恢复。要验证跨版本，设置 `ANIMEMO_PREVIOUS_IMAGE` 和 `ANIMEMO_CANDIDATE_IMAGE` 为本地已存在的两个镜像；若要严格验证新增迁移，另设 `ANIMEMO_EXPECT_MIGRATION=1`；只有代码变化的更新不要求虚构迁移。不要将测试实例接到正式外部服务。

操作被中断时使用下文的 `instance recover`。安装、恢复、备份、配置、更新和回滚已有持久阶段；未完成操作会阻止其他修改命令覆盖其恢复信息。失败实例、数据库副本和快照保留，不自动删除。进程仍在运行时不会抢锁；不要手删活动锁。

这些工具只管理本项目实例，不修改 Docker 全局代理、存储驱动或其他项目资源，也不自动发布 GitHub release。

日常只验证受影响路径，准备发行候选时完成适用的实例生命周期验收。VM 和 25 分钟负载测试是可选手动专项，未运行不妨碍继续开发；现有容器验收不能表述为空白宿主 OS 安装验收。

## 插件与实例状态

当前受限文件转换插件的不可变模块、清单、历史版本、摘要及启用状态全部在 PostgreSQL，因此包含于实例快照。更新工具在副本上运行候选镜像的 `plugins-check`；不兼容或损坏的已启用包阻止迁移，并恢复原实例。停用包不阻止恢复；工具不会自动停用插件。当前候选镜像必须支持该检查命令，返回旧版本使用明确的 rollback 或原镜像备份恢复。

插件版本切换只选择已安装包，用户转换结果仍要确认导入。本阶段插件没有外部 KV / 文件状态；未来增加这些能力时必须扩展快照和恢复合同。见 [plugins.md](plugins.md)。

## 持久恢复与独立 Worker

当前镜像带有 `org.animemo.worker-mode=separate-v1` 标记，实例工具显式启用 `runtime-worker` profile，安装/启动运行 app + worker；停止、备份、更新和清理都覆盖两者。旧镜像仍只启动它认识的命令。

上述操作把 ID、阶段、原配置、候选配置和备份路径写入 `.local/instances/<name>/operation.json`（0600）。它包含私有配置，不能复制到报告、源码包或聊天。公开操作回执只显示 ID、阶段和备份路径。

中断后执行：

```sh
npm run instance -- recover --name home
```

工具只会移除确认进程已退出的旧操作锁；仍在执行的操作不会被抢占。若中断发生在数据库切换阶段，会先将候选数据库另存 rescue 快照，再启动记录的原实例。安装中断会继续启动已选配置；恢复中断在导入完成前重新选择空白数据库，已进入启动阶段则直接继续启动。备份和配置中断恢复原配置及原来的运行状态；更新/回滚切换中断先救援候选库，再恢复原配置。显式 rollback 仍需原有确认参数，并先保存新写入。阶段注入测试不等于断电可靠性保证，磁盘/Docker 损坏仍需人工处理。

恢复副本默认撤销会话和外部授权，扩展包保留但停用（review_required）；重新审阅后激活。随附包重新激活也要校验摘要。普通启动隔离坏包以保留 Core 可用，候选升级预检继续严格拒绝不兼容的已启用包。

Linux 0600 媒体备份恢复：一次性 media-restore 容器使用归档文件的 UID/GID，只读挂载单个归档；Web/Worker 保持镜像的非 root UID。不 chmod 备份，不增加 capabilities。
