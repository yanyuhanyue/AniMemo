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

公共入口源代码位于 `install.animemo.cc/`。页面与 `install.sh` 不是 Release Authority。bootstrap 只取得、验证并启动 canonical Installer；兼容性、迁移、数据库操作和 Safe Switch 仍由版本化 Installer / Updater 所有。

推荐两步命令不会使用 `curl | sudo sh`：

```sh
curl -fsSLo /tmp/animemo-install.sh https://install.animemo.cc/install.sh
sudo sh /tmp/animemo-install.sh
```

非交互执行必须显式提供 `--public-origin`。需要 Official Mirror 时，额外提供 `--source official-mirror`；失败不会自动调用 GitHub transport。Portable CTA 在 authority 冻结前不会提供绕过命令。

## Doctor

Doctor Basic 只读取本机的闭合 distribution snapshot，用于诊断 configured transport policy、最近 transport receipt、verified release identity、verified OCI identity 与 plan/receipt drift。Doctor 不访问 GitHub、mirror、DNS 或 Docker，也不会下载材料或切换来源。
