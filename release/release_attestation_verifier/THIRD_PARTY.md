# 第三方验证依赖

生产验证器直接固定 `github.com/sigstore/sigstore-go v1.2.2`。该库由 Sigstore
项目维护，许可证为 Apache-2.0，并声明为稳定、可用于生产的 Sigstore bundle
验证库。依赖图由本目录 `go.mod` 与 `go.sum` 精确固定；打包阶段必须使用已审计、
已缓存的 module 内容构建静态二进制，运行时禁止下载 module、TUF metadata 或任何
网络资源。

Go 语言版本下限固定为 `1.25.8`，这是 `sigstore-go v1.2.2` 自身 `go.mod`
声明的最小版本；本地资格认证使用官方 Go `1.26.5` 构建，不把临时 toolchain
写入仓库。

GitHub Immutable Release 策略与 GitHub CLI v2.97.0 对齐：Release service SAN
必须是 `https://dotcom.releases.github.com`，issuer matcher 不增加 OIDC 限制，
并使用 GitHub 私有 TUF `trusted_root.json` 的 RFC3161 时间戳根验证至少一个签名
时间戳。Actions provenance 使用预置 Sigstore public-good `trusted_root.json`，
要求 SCT、Rekor transparency log 与 observer timestamp 各自满足 GitHub CLI
v2.97.0 的阈值。

许可证资格认证的最终输入必须由 release packaging lane 从 `go list -m all` 和
`go-licenses`（或等价维护工具）生成并留存。当前源码实现不在运行时发现、选择或
信任任何 bundle 内携带的根。

生产安装布局由 `INSTALLATION_CONTRACT.json` 关闭。Updater 只从固定绝对路径
`/usr/share/animemo/offline-trust/v1` 装载四个精确文件，逐一验证 profile 中的
SHA-256 identity；不会读取 PATH、环境变量或 bundle 自带根。发布材料打包器必须
把已构建 binary 和两份由各自 TUF 仓库预先取得并审核的单一 trusted-root JSON
安装到该布局，所有文件由 root 拥有，binary 为 0755，其余为 0644。

2026-08-19 使用 `google/go-licenses v2.0.1` 对实际 binary package graph 执行
report。工具识别出的依赖均为 Apache-2.0、MIT、BSD-2-Clause 或 BSD-3-Clause。
工具因许可证位于 module/repository 上层而对以下项目报告 Unknown，已逐一核对其
官方源码根许可证：`cyberphone/json-canonicalization`、`in-toto/attestation`、
`in-toto/in-toto-golang` 均为 Apache-2.0。AniMemo 自身遵循仓库根目录的
PolyForm Noncommercial 1.0.0。发布打包仍须携带这些 notice/license 文本；本次
审查不把分类工具的 Unknown 静默当作通过。
