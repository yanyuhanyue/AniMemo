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

公共入口组件源代码位于与域名无关的 `sites/install-portal/`。当前公开入口仍为 `https://install.animemo.cc`。页面不是 Release Authority，也不得提供在权威验证前执行的 AniMemo 脚本。历史 `install.sh` 已退役并固定 fail closed。在线 Stage‑0 必须先从 GitHub 官方签名 APT 仓库安装固定版本的 GitHub CLI，再对 exact tag 与本地资产执行 GitHub Immutable Release 验证；离线 Stage‑0 必须由 operator 或可信镜像独立预置信任材料。

本安全边界由 `docs/installer-contract-v2.md` 冻结；原 `docs/installer-contract-v1.md` 保持其历史冻结字节不变。

Official Mirror 的固定身份如下；它只运输原始字节，不发布 Release，也不提供 authority fallback：

- provider：Cloudflare R2；
- bucket：`animemo-release-mirror`；
- origin：`https://download.animemo.cc`；
- prefix：`yanyuhanyue/AniMemo/releases/download`；
- 完整性 marker：`<prefix>/<EXACT_TAG>/mirror-receipt.json`，且只能在五项资产完成后写入。

下列 Ubuntu 24.04 Stage‑0 是正式的 Official Mirror 首装入口。它从 GitHub 官方固定 v2.97.0 Release 下载 checksum manifest 与 amd64 Debian package，先将 manifest 和 package 分别绑定到仓库内冻结的 SHA256，再用 manifest 对 package 做第二次交叉校验，随后安装精确 `/usr/bin/gh` 2.97.0。这样不会依赖 GitHub CLI 滚动 APT 仓库继续保留旧版本。GitHub Release 资产端点只允许一次 HTTPS 重定向；重定向目标返回的字节仍必须通过上述固定摘要。Stage‑0 先验证 GitHub Immutable Release，再以固定 URL 下载 installer materials；Official Mirror 的 `curl --location --max-redirs 0` 继续拒绝任何重定向。所有 retry、连接时间和总时间均有上限。APT 和 Python 运行时依赖属于主机前置条件，不是 AniMemo 产品 mutation。

```sh
# OFFICIAL_MIRROR_STAGE0_BEGIN
set -euo pipefail
EXACT_TAG='<EXACT_TAG>'
[[ "$EXACT_TAG" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-rc\.[1-9][0-9]*)?$ ]]

sudo /usr/bin/apt-get update
sudo /usr/bin/apt-get install --yes --no-install-recommends ca-certificates curl python3-venv
test "$(/usr/bin/dpkg --print-architecture)" = amd64
GH_VERSION=2.97.0
GH_CHECKSUMS_SHA256=61905c69ec8660f310814ec98395cdd0c2d07aabf024c597ec45813984a02334
GH_DEB_SHA256=7c7fa3bb890db0934baf65910d97b8c0fa437b2e590f7f7daf6bdf82c5c486d7
GH_BOOTSTRAP_DIRECTORY="$(/usr/bin/mktemp -d)"
GH_CHECKSUMS="$GH_BOOTSTRAP_DIRECTORY/gh_${GH_VERSION}_checksums.txt"
GH_DEB="$GH_BOOTSTRAP_DIRECTORY/gh_${GH_VERSION}_linux_amd64.deb"
cleanup_gh_bootstrap() {
  /usr/bin/rm -f -- "$GH_CHECKSUMS" "$GH_DEB"
  /usr/bin/rmdir -- "$GH_BOOTSTRAP_DIRECTORY"
}
trap cleanup_gh_bootstrap EXIT
for name in "gh_${GH_VERSION}_checksums.txt" "gh_${GH_VERSION}_linux_amd64.deb"; do
  /usr/bin/curl --proto '=https' --proto-redir '=https' --tlsv1.2 --location --max-redirs 1 \
    --fail --silent --show-error --connect-timeout 30 --max-time 300 \
    --retry 2 --retry-delay 10 --retry-max-time 240 \
    --output "$GH_BOOTSTRAP_DIRECTORY/$name" \
    "https://github.com/cli/cli/releases/download/v${GH_VERSION}/$name"
done
test "$(/usr/bin/sha256sum "$GH_CHECKSUMS" | /usr/bin/awk '{print $1}')" = "$GH_CHECKSUMS_SHA256"
/usr/bin/grep -Fxq "$GH_DEB_SHA256  gh_${GH_VERSION}_linux_amd64.deb" "$GH_CHECKSUMS"
test "$(/usr/bin/sha256sum "$GH_DEB" | /usr/bin/awk '{print $1}')" = "$GH_DEB_SHA256"
sudo /usr/bin/apt-get install --yes --no-install-recommends "$GH_DEB"
test "$(/usr/bin/gh --version | /usr/bin/sed -nE 's/^gh version ([0-9]+\.[0-9]+\.[0-9]+).*/\1/p' | /usr/bin/head -n 1)" = "$GH_VERSION"
cleanup_gh_bootstrap
trap - EXIT

STAGE0_DIRECTORY="$(/usr/bin/mktemp -d)"
MIRROR_CANDIDATE="$STAGE0_DIRECTORY/installer-materials.tar"
GH_TOKEN_PIPE="$STAGE0_DIRECTORY/gh-token.pipe"
GH_TOKEN_WRITER=''
cleanup_stage0() {
  if test -n "$GH_TOKEN_WRITER"; then
    wait "$GH_TOKEN_WRITER" 2>/dev/null || true
  fi
  /usr/bin/rm -f -- "$MIRROR_CANDIDATE"
  /usr/bin/rm -f -- "$GH_TOKEN_PIPE"
  /usr/bin/rmdir -- "$STAGE0_DIRECTORY"
}
trap cleanup_stage0 EXIT
MIRROR_URL="https://download.animemo.cc/yanyuhanyue/AniMemo/releases/download/$EXACT_TAG/installer-materials.tar"
/usr/bin/gh release verify "$EXACT_TAG" --repo yanyuhanyue/AniMemo
/usr/bin/curl --proto '=https' --tlsv1.2 --location --max-redirs 0 \
  --fail --silent --show-error --connect-timeout 30 --max-time 900 \
  --retry 2 --retry-delay 10 --retry-max-time 600 \
  --output "$MIRROR_CANDIDATE" "$MIRROR_URL"
/usr/bin/gh release verify-asset "$EXACT_TAG" "$MIRROR_CANDIDATE" \
  --repo yanyuhanyue/AniMemo

/usr/bin/mkfifo -m 0600 -- "$GH_TOKEN_PIPE"
/usr/bin/timeout --signal=TERM --kill-after=5s 30s \
  /bin/bash --noprofile --norc -c \
  'exec /usr/bin/gh auth token >"$1"' \
  animemo-gh-token-writer "$GH_TOKEN_PIPE" &
GH_TOKEN_WRITER=$!
sudo /usr/bin/env -i EXACT_TAG="$EXACT_TAG" MIRROR_CANDIDATE="$MIRROR_CANDIDATE" \
  GH_TOKEN_PIPE="$GH_TOKEN_PIPE" \
  HOME=/root PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
  GH_PROMPT_DISABLED=1 /bin/bash --noprofile --norc <<'ANIMEMO_PROTECTED_HANDOFF'
set -euo pipefail
umask 077
test -p "$GH_TOKEN_PIPE"
GH_TOKEN="$(
  /usr/bin/timeout --signal=TERM --kill-after=5s 35s \
    /bin/bash --noprofile --norc -c \
    'set -euo pipefail; IFS= read -r -t 30 token <"$1"; test -n "$token"; printf "%s" "$token"' \
    animemo-gh-token-reader "$GH_TOKEN_PIPE"
)"
test -n "$GH_TOKEN"
export GH_TOKEN
ANIMEMO_ROOT=/var/lib/animemo
AUTHORITY_PARENT="$ANIMEMO_ROOT/bootstrap-authority"
PROTECTED_ROOT=/var/lib/animemo/bootstrap-authority/v1
PROTECTED="$PROTECTED_ROOT/installer-materials.tar"
MATERIALS="$PROTECTED_ROOT/materials"
RUNTIME="$PROTECTED_ROOT/installer-runtime"
created_animemo_root=0
created_authority_parent=0
created_protected_root=0
created_protected=0
created_materials=0
created_runtime=0
completed=0
HANDOFF=''
cleanup() {
  test -z "$HANDOFF" || /usr/bin/rm -f -- "$HANDOFF"
  test "$completed" = 1 && return 0
  if test "$created_runtime" = 1 && test -e "$RUNTIME"; then
    /usr/bin/find "$RUNTIME" -xdev -depth -mindepth 1 -delete
    /usr/bin/rmdir -- "$RUNTIME"
  fi
  if test "$created_materials" = 1 && test -e "$MATERIALS"; then
    /usr/bin/find "$MATERIALS" -xdev -depth -mindepth 1 -delete
    /usr/bin/rmdir -- "$MATERIALS"
  fi
  test "$created_protected" = 0 || /usr/bin/rm -f -- "$PROTECTED"
  if test "$created_protected_root" = 1 && test -e "$PROTECTED_ROOT"; then
    /usr/bin/rmdir -- "$PROTECTED_ROOT"
  fi
  if test "$created_authority_parent" = 1 && test -e "$AUTHORITY_PARENT"; then
    /usr/bin/rmdir -- "$AUTHORITY_PARENT"
  fi
  if test "$created_animemo_root" = 1 && test -e "$ANIMEMO_ROOT"; then
    /usr/bin/rmdir -- "$ANIMEMO_ROOT"
  fi
}
trap cleanup EXIT

assert_safe_root_directory() {
  path="$1"
  exact_mode="$2"
  test -d "$path"
  test ! -L "$path"
  metadata="$(/usr/bin/stat -c '%u:%g:%a' -- "$path")"
  uid="${metadata%%:*}"
  remainder="${metadata#*:}"
  gid="${remainder%%:*}"
  mode="${remainder##*:}"
  test "$uid" = 0
  test "$gid" = 0
  test $((8#$mode & 022)) = 0
  test -z "$exact_mode" || test "$mode" = "$exact_mode"
}

ensure_safe_root_directory() {
  path="$1"
  mode="$2"
  created_flag="$3"
  if test -L "$path" || { test -e "$path" && test ! -d "$path"; }; then
    return 1
  fi
  if test ! -e "$path"; then
    printf -v "$created_flag" 1
    /usr/bin/install -d -o root -g root -m "0$mode" -- "$path"
  fi
  assert_safe_root_directory "$path" "$mode"
}

assert_safe_root_directory /var/lib ''
ensure_safe_root_directory "$ANIMEMO_ROOT" 700 created_animemo_root
ensure_safe_root_directory "$AUTHORITY_PARENT" 700 created_authority_parent
ensure_safe_root_directory "$PROTECTED_ROOT" 700 created_protected_root
test ! -e "$PROTECTED" && test ! -L "$PROTECTED"
test ! -e "$MATERIALS" && test ! -L "$MATERIALS"
test ! -e "$RUNTIME" && test ! -L "$RUNTIME"
HANDOFF="$(/usr/bin/mktemp -p "$PROTECTED_ROOT" .installer-materials.XXXXXXXX.candidate)"
/usr/bin/install -o root -g root -m 0600 "$MIRROR_CANDIDATE" "$HANDOFF"
/usr/bin/gh release verify-asset "$EXACT_TAG" "$HANDOFF" \
  --repo yanyuhanyue/AniMemo
created_protected=1
/usr/bin/ln "$HANDOFF" "$PROTECTED"
/usr/bin/rm -f -- "$HANDOFF"
HANDOFF=''
test -f "$PROTECTED" && test ! -L "$PROTECTED"
test "$(/usr/bin/stat -c '%u:%g:%a:%h' -- "$PROTECTED")" = '0:0:600:1'
/usr/bin/gh release verify-asset "$EXACT_TAG" "$PROTECTED" \
  --repo yanyuhanyue/AniMemo

created_materials=1
/usr/bin/install -d -o root -g root -m 0700 -- "$MATERIALS"
assert_safe_root_directory "$MATERIALS" 700
/usr/bin/tar -xf "$PROTECTED" -C "$MATERIALS" \
  --no-same-owner --no-same-permissions
/usr/bin/chown -R --no-dereference root:root "$MATERIALS"
/usr/bin/chmod -R go-w "$MATERIALS"
created_runtime=1
/usr/bin/python3 -P -B -m venv "$RUNTIME"
assert_safe_root_directory "$RUNTIME" 700
/usr/bin/env -i HOME=/root PATH=/usr/sbin:/usr/bin:/sbin:/bin \
  LANG=C.UTF-8 LC_ALL=C.UTF-8 \
  "$RUNTIME/bin/python" -P -B -m pip install \
  --disable-pip-version-check --no-cache-dir --no-index --only-binary=:all: \
  --find-links "$MATERIALS/wheelhouse" \
  -r "$MATERIALS/release/requirements.txt" \
  -r "$MATERIALS/durability/requirements.txt"
/usr/bin/chmod -R a+rX,go-w "$RUNTIME"
(
  cd "$MATERIALS"
  export HOME PATH LANG LC_ALL GH_PROMPT_DISABLED GH_TOKEN
  export PYTHONPATH="$MATERIALS" PYTHONSAFEPATH=1
  "$RUNTIME/bin/python" -P -B -m installer \
    install --version "$EXACT_TAG" --source official-mirror \
    --public-origin '<PUBLIC_ORIGIN>' --non-interactive --accept <INSTALLER_ARGS>
)
completed=1
ANIMEMO_PROTECTED_HANDOFF
wait "$GH_TOKEN_WRITER"
GH_TOKEN_WRITER=''
# OFFICIAL_MIRROR_STAGE0_END
```

这段流程不从 install.animemo.cc 执行脚本，不读取 mirror receipt 作为权威，也不使用 `latest`、query、任意 URL 或 GitHub/mirror 自动 fallback。候选资产在首次 `verify-asset` 前不会进入 root-owned 路径；受保护副本的任一次再验证失败都会由 trap 清除本次创建的候选和 final path，不留下 AniMemo 持久 mutation。只有两次受保护副本验证完成后才解包、创建 venv 并执行 AniMemo Python byte。隔离运行时只从同一副本绑定的 wheelhouse 安装两份固定 requirements，显式禁止索引、源码包与缓存，不继承系统或用户 site-packages。受限权限交接只通过 mode `0600` 的一次性 FIFO 传递当前 GitHub CLI 凭据，producer 与 reader 都有 deadline；凭据不进入 argv、持久文件或输出，并只在已由外层 `env -i` 清洗的 root shell 中导出。受保护材料目录的显式 `cd`、`PYTHONSAFEPATH=1` 与 Python `-P` 同时约束模块搜索前缀。Installer 随后在任何产品 mutation 前再次验证受保护副本，将已加载的核心模块逐字节绑定回该 tar，才消费闭合的 `BOOTSTRAP_PRIVILEGE_GATE`。

非交互执行必须显式提供 `--public-origin`。需要 Official Mirror 时，额外提供 `--source official-mirror`；失败不会自动调用 GitHub transport。Portable CTA 在 authority 冻结前不会提供绕过命令。

## Doctor

Doctor Basic 只读取本机的闭合 distribution snapshot，用于诊断 configured transport policy、最近 transport receipt、verified release identity、verified OCI identity 与 plan/receipt drift。Doctor 不访问 GitHub、mirror、DNS 或 Docker，也不会下载材料或切换来源。
