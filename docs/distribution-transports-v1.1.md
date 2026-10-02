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

公共入口组件源代码位于与域名无关的 `sites/install-portal/`。当前公开入口仍为 `https://install.animemo.cc`。页面不是 Release Authority，也不得提供在权威验证前执行的 AniMemo 脚本。历史 `install.sh` 已退役并固定 fail closed。在线 Stage‑0 必须用独立可信的固定验证器匿名获取并验证 exact Release/tag/本地资产；离线 Stage‑0 必须由 operator 或可信镜像独立预置信任材料。

本安全边界由 `docs/installer-contract-v2.md` 冻结；原 `docs/installer-contract-v1.md` 保持其历史冻结字节不变。

Official Mirror 的固定身份如下；它只运输原始字节，不发布 Release，也不提供 authority fallback：

- provider：Cloudflare R2；
- bucket：`animemo-release-mirror`；
- origin：`https://download.animemo.cc`；
- prefix：`yanyuhanyue/AniMemo/releases/download`；
- 完整性 marker：`<prefix>/<EXACT_TAG>/mirror-receipt.json`，且只能在五项资产完成后写入。

在线 Stage-0 的无账户实现与签名职责见 `tokenless-stage0-contract.md`。当前独立首信任的干净主机交付尚未闭合，因此旧账户凭据交接命令已撤下。以下入口固定拒绝执行，待后续明确授权的信任分发与开发集成验证完成后再提供首装命令。

```sh
# OFFICIAL_MIRROR_STAGE0_BEGIN
printf '%s\n' 'BOOTSTRAP_TOKENLESS_INDEPENDENT_TRUST_REQUIRED' >&2
exit 1
# OFFICIAL_MIRROR_STAGE0_END
```

显式 offline 的独立预置信任要求保持不变。任何缓存、镜像回执或包内工具均不能消除此拒绝。当前修复为 DEVELOPMENT_ONLY，不改变已发布 rc.3 字节或验收结果。

## Doctor

Doctor Basic 只读取本机的闭合 distribution snapshot，用于诊断 configured transport policy、最近 transport receipt、verified release identity、verified OCI identity 与 plan/receipt drift。Doctor 不访问 GitHub、mirror、DNS 或 Docker，也不会下载材料或切换来源。
