# 候选与可信发行

当前源码版本为 `1.3.0-rc.4`，交付范围见 [1.3 交付记录](v1.3-delivery.md)。发行机制始于 [1.1](v1.1-delivery.md)，沿用到当前候选。本地开发候选没有正式发行身份；签名、GitHub 上传和公开下载验收必须真实执行后才记录通过。

## 版本与边界

`package.json` 是产品版本入口，lockfile 同步。`npm run build`、开发入口及发行构建把版本和源码提交注入 Go 二进制；`animemo version` 无需数据库即可输出版本、源码及内嵌迁移摘要。镜像使用同样的 OCI 版本/源码标签；`instance status`、备份清单及发行清单保留对应身份。API v1 和扩展协议版本独立，不随产品小版本机械递增。

直接使用 `docker build`/普通 Compose 开发构建时，默认标签是 `development/unknown`；这种镜像只用于本地开发。统一候选构建入口如下：

```sh
npm run release -- build --development --image animemo-local:1.3 --output .local/output/candidate
npm run release -- inspect --directory .local/output/candidate --development
```

目录必须尚不存在。构建生成 `release.json` 和 `animemo-image.tar`，固定 Linux amd64；使用已有 Docker 代理和可选的 `ANIMEMO_BUILD_CA` / `CODEX_PROXY_CERT`。只有构建阶段需要网络，镜像运行 `version` 时禁用网络。`inspect` 只检查格式与完整性，明确返回 `trust_verified: false`。

开发候选会标记 `development: true`，未提交源码的 revision 带 `-dirty`；正式验证/安装入口拒绝此类产物。不要手改清单冒充签发或把本地成功写成远端发布成功。

开发交接包可用 `node tooling/release.mjs load --directory 目录 --development` 核验并加载，输出仍为 `trust_verified: false`。仅明确标记 `development: true` 的包可走此入口。安装时使用输出的 `manifest.local_image`。正式 `load` 与实例 `--release` 继续要求来源证明。

新清单使用镜像配置摘要作为可移植的 `image`，同时记录 `image_config`。经典 Docker 与 containerd 可能为相同归档返回不同的本机 ID；加载器从校验过的归档解析摘要关系，再检查平台和来源标签，返回 `local_image`。不扫描或猜测其他本机镜像，也不因 ID 不同跳过内容校验。

## 正式发行流程

入口为 `.github/workflows/release.yml`，仅在 `v*` 标签或手动选择标签时运行；普通 PR 不触发发行。发布前把审核后的源码提交，并使标签严格等于 `v` + 产品版本。工作流要求 GitHub 托管 Ubuntu runner、可用的 Docker、GitHub CLI 和仓库的 Actions/产物证明权限。

流程：

1. 检查标签与版本对应，拒绝覆盖已有 release；执行一次适用完整回归。
2. 从干净提交只构建一次镜像，立即保存精确字节和 SHA-256；实际实例生命周期、加密转移、完整记忆库恢复、Worker/扩展验收使用该候选。升级基线通过手动运行的 `previous_release` 或仓库变量 `ANIMEMO_PREVIOUS_RELEASE` 指定；下载后先验证来源和摘要，拒绝同一版本/镜像冒充升级。
3. 用 GitHub `actions/attest-build-provenance` 为清单及镜像归档签发来源证明，保留 `attestations.jsonl`。
4. 上传草稿，重新下载，验证身份和完整性、加载相同镜像，执行新实例启动及 doctor；失败时草稿不发布。
5. 将同一草稿公开，不重新构建或替换文件，再验证公开下载的实际字节和来源。若此步失败，工作流失败，需要维护者调查，不能写成发布验收通过。

`v1.3.0-rc.4` 标签生成 prerelease；去掉产品版本的预发布后缀后生成正式版本。候选晋级指相同版本产物从验证环境进入使用环境；更改产品版本需要新的构建与证明，不能把 RC 改名伪装成另一版本。

首次正式发行没有已签发的前版时，升级基线可留空，报告只声明新安装和恢复，不宣称跨版本升级。后续承诺支持旧版升级的发行需指定对应的已发布标签；当前完整记忆库探针要求 1.3 RC2 或更新的记录 API。云端旧 RC2 是未签发开发镜像，不能填成一个并不存在的正式 Release；本地用 `ANIMEMO_PREVIOUS_IMAGE` 和 `ANIMEMO_CANDIDATE_IMAGE` 选择两个真实镜像，运行 `npm run test:instance` 与 `node tooling/memory-instance-smoke.mjs`。涉及新增迁移时为前一命令设置 `ANIMEMO_EXPECT_MIGRATION=1`。记忆库探针先在旧镜像写入札记、原图、年度册和旧成就，再升级；候选上传的徽章、自动授予和展示槽也纳入完整快照恢复。

首发支持个人/小型自托管、Linux amd64、Docker Compose、PostgreSQL 17；核心记录、私人原图与成就不依赖外部存储或 OAuth。尚未真实验收的邮件、OAuth、R2 默认关闭，不列为已支持能力；公开部署需在目标域名核验 HTTPS、登录/分享和撤回。主题插件、第三方市场、PWA、旧项目数据迁移、多架构与大站容量不在首发承诺内。

VM 和 25 分钟负载均不在依赖链中。`timeout-minutes` 是防止失控的执行上限，不是要求等待的测试时长。无需自建发行服务、证据平台或多个镜像源。工作流创建草稿后失败时先检查失败步骤和已上传产物；不要自动覆盖或删除发行物来绕过检查。

## 验证和安装

使用支持下列 flags 的 GitHub CLI（本地核对版本 2.83.2）。`ANIMEMO_GH` 可指定可信 CLI 的绝对路径，也会优先寻找 `.local/tools/gh/gh`。工具不会静默下载或关闭证书验证。

下载同一 release 的 `release.json`、`animemo-image.tar`、`attestations.jsonl` 至一个目录后：

```sh
npm run release -- verify --directory .local/output/downloaded
npm run instance -- install --name home --release .local/output/downloaded --port 18082
npm run instance -- update --name home --release .local/output/downloaded
```

install/update 的 `--release` 会再次验证后加载镜像；不能同时给 `--image`。实例工具本身应来自同一可信仓库/标签。验证策略固定为 `yanyuhanyue/AniMemo`、`.github/workflows/release.yml`、清单版本对应的 tag、精确源码提交以及 GitHub 托管 runner；下载内容不能改写信任来源。清单和归档都验证，校验和不代替发布者身份。

保留已有 `--image 本地镜像`，用于自行构建/审核的镜像和离线开发；该方式只固定镜像 ID，不声明官方身份。`docker load` 也不会自动赋予官方身份。

## 网络受限与本地接手

离线可转移镜像和证明。GitHub CLI 的信任根获取也可能需要网络；有既有可信根时，`release verify/load --trusted-root FILE` 或实例命令的 `ANIMEMO_TRUSTED_ROOT` 可指定本地根。必须通过独立可信渠道取得根，不能把下载包自带的任意根作为信任依据。没有有效证明/可信根时，官方验证失败，不自动降级为仅 SHA 校验。

云端打包时可构建和执行 Linux/Docker 流程，但 GitHub CLI 授权及 API/Sigstore 网络受限，未执行远端发行。本机接手通过现有 `codex/animemo-next` 分支同步源码；标签签发和 Release 发布仍是独立步骤。正式发行步骤：

1. 核对接手包与自己的工作区改动，提交最终源码；确认产品版本、标签及仓库 Actions 权限。
2. 在可访问 GitHub 的本地正常配置 CLI，推送已审核提交和对应标签。不要把 token、age 私钥或实例配置提交进 Git。
3. 查看 Release 工作流的真实结果，下载真实发行物；用上述 `verify` 和新实例安装验证。保留源码提交、镜像/归档摘要、工作流 URL、下载验证及启动结果。
4. 把结果补入 [验收记录](verification.md)。只有这一步真实完成，1.1 的远端发行闭环才算通过；不影响继续开发其他已独立验证的内容。
