# AniMemo v1.1 Distribution 运输与发布权威

AniMemo 将“字节从哪里取得”与“哪些字节被授权为 Release”分开处理。

## 唯一 Release Authority

当前唯一发布权威由以下闭合证据共同构成：

- 固定 GitHub 仓库 `yanyuhanyue/AniMemo` 的 Release metadata；
- 固定四项 Release asset inventory；
- Release Manifest、Deployment Contract 与 checksums 的交叉绑定；
- tag 到 source commit 的精确绑定；
- GitHub Actions / Sigstore provenance；
- canonical OCI `repository@sha256:digest`。

`ReleaseAuthorityVerifier` 是唯一能够产生 `VerifiedReleaseMaterials` 的模块。Transport receipt 只能证明取得了哪些字节，不能声明这些字节已发布、可信或稳定。

## 显式 Transport Policy

生产 transport policy 是闭合集合：

- `github`（默认）；
- `official-mirror`（明确选择）；
- `local-bundle` 仅保留为 fail-closed 的 portable boundary。

不存在 `auto`、地区识别、延迟竞速、任意 URL、任意仓库或错误后的跨 transport fallback。改变 transport 必须形成新的显式 policy identity。

Official Mirror 只负责运输固定 Release assets。在线模式下，GitHub 仍提供 Release metadata、tag/commit 与 provenance 权威证据。OCI runtime acquisition 始终验证 canonical digest，并在 pull/import 后读取实际 RepoDigest；tag-only 或同名镜像不能替代 digest identity。

## Portable / Offline Bundle

v1.1 已实现 portable bundle 的闭合布局、canonical JSON、路径与文件类型防护、递归摘要、OCI descriptor DAG 验证和本地 acquisition plan foundation。

Portable Publication Authority 与 trust bootstrap 尚未冻结。因此解析、摘要和 OCI 验证可以通过，但任何生产安装或升级授权必须返回：

```text
BLOCKED_PORTABLE_PUBLICATION_AUTHORITY
```

bundle 内自带 checksum、公钥或自声明 trust root 不构成 Release Authority。后续治理任务是 `V1_1_PORTABLE_RELEASE_AUTHORITY_DECISION`。

## 安装入口

公共入口源代码位于 `install.animemo.cc/`。页面不是 Release Authority，也不得提供在权威验证前执行的 AniMemo 脚本。历史 `install.sh` 已退役并固定 fail closed。在线 Stage‑0 必须先从 GitHub 官方签名 APT 仓库安装固定版本的 GitHub CLI，再对 exact tag 与本地资产执行 GitHub Immutable Release 验证；离线 Stage‑0 必须由 operator 或可信镜像独立预置信任材料。

本安全边界由 `docs/installer-contract-v2.md` 冻结；原 `docs/installer-contract-v1.md` 保持其历史冻结字节不变。

推荐验证步骤不会使用 `curl | sudo sh`，并且精确选择 tag：

```sh
gh release verify <EXACT_TAG> --repo yanyuhanyue/AniMemo
gh release download <EXACT_TAG> --repo yanyuhanyue/AniMemo \
  --pattern installer-materials.tar --dir ./animemo-stage0
gh release verify-asset <EXACT_TAG> ./animemo-stage0/installer-materials.tar \
  --repo yanyuhanyue/AniMemo
```

只有上述两项 authority gate 均通过后，操作员才可用系统工具完成固定的受保护交接（下列命令中的 `<INSTALLER_ARGS>` 必须替换为正常 Installer 参数）：

```sh
sudo /usr/bin/install -d -o root -g root -m 0700 /var/lib/animemo/bootstrap-authority/v1
sudo /usr/bin/install -o root -g root -m 0600 \
  ./animemo-stage0/installer-materials.tar \
  /var/lib/animemo/bootstrap-authority/v1/installer-materials.tar
sudo /usr/bin/env -i HOME=/root PATH=/usr/sbin:/usr/bin:/sbin:/bin \
  LANG=C.UTF-8 LC_ALL=C.UTF-8 GH_PROMPT_DISABLED=1 \
  /usr/bin/gh release verify-asset <EXACT_TAG> /var/lib/animemo/bootstrap-authority/v1/installer-materials.tar \
  --repo yanyuhanyue/AniMemo
sudo /usr/bin/install -d -o root -g root -m 0700 \
  /var/lib/animemo/bootstrap-authority/v1/materials
sudo /usr/bin/tar -xf /var/lib/animemo/bootstrap-authority/v1/installer-materials.tar \
  -C /var/lib/animemo/bootstrap-authority/v1/materials --no-same-owner
sudo /usr/bin/chown -R root:root /var/lib/animemo/bootstrap-authority/v1/materials
sudo /usr/bin/chmod -R go-w /var/lib/animemo/bootstrap-authority/v1/materials
sudo /usr/bin/env -i -C /var/lib/animemo/bootstrap-authority/v1/materials \
  HOME=/root PATH=/usr/sbin:/usr/bin:/sbin:/bin \
  LANG=C.UTF-8 LC_ALL=C.UTF-8 \
  PYTHONPATH=/var/lib/animemo/bootstrap-authority/v1/materials \
  PYTHONSAFEPATH=1 /usr/bin/python3 -P -B -m installer <INSTALLER_ARGS>
```

这段流程不从 install.animemo.cc 获取可执行脚本。用户目录中的首次验证只决定候选是否可复制；root-owned 副本必须由独立安装的固定 `/usr/bin/gh` 2.97.0 再次验证，且该验证必须发生在解包和执行任何 AniMemo Python byte 之前，从而关闭 verified-path 到 protected-path 的替换窗口。`env -C`、`PYTHONSAFEPATH=1` 与 Python `-P` 同时把当前目录移出模块搜索前缀，防止用户目录中的同名 `installer` 包抢先获得 root 执行。Installer 随后在任何 Docker、APT、systemd 或 AniMemo 持久化动作前再次验证受保护副本，并把当前已载入的 Installer、Updater、Release 与 durability 核心模块逐字节绑定回该 tar 后，才消费闭合的 `BOOTSTRAP_PRIVILEGE_GATE`；第一项受权 mutation 是原子安装 TrustProfile、两套 trusted root 和两套 TUF root。受保护目录中存在额外、缺失或已替换的运行模块时必须失败关闭。

非交互执行必须显式提供 `--public-origin`。需要 Official Mirror 时，额外提供 `--source official-mirror`；失败不会自动调用 GitHub transport。Portable CTA 在 authority 冻结前不会提供绕过命令。

## Doctor

Doctor Basic 只读取本机的闭合 distribution snapshot，用于诊断 configured transport policy、最近 transport receipt、verified release identity、verified OCI identity 与 plan/receipt drift。Doctor 不访问 GitHub、mirror、DNS 或 Docker，也不会下载材料或切换来源。
