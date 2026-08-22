# AniMemo Release Contract v1

## Authority and identity

AniMemo 的权威发布身份由 Git tag 与 `release-manifest.json` 共同给出：

- `VERSION` 是人类可读的 SemVer tag；
- `COMMIT` 是构建输入的完整 Git SHA；
- `CHANNEL` 是 `beta`、`rc` 或 `stable`；
- API/Web 的 `sha256` digest 是机器部署身份。

`package.json` 的私有 `0.0.0` 不再作为应用发布版本。构建会把 Manifest 的 VERSION、COMMIT 与 CHANNEL 注入运行时；生产切换只接受 Manifest 中的 `repository@sha256:digest`，不得以 `latest` 或其他 mutable tag 作为部署身份。

## Channels and version resolution

所有通道都在 `yanyuhanyue/AniMemo`。Release Consumer 同时验证 tag 语义与 GitHub Release metadata：Stable 必须 `prerelease=false`，Beta/RC 必须 `prerelease=true`，draft 或 metadata/channel 不一致都拒绝：

```text
v1.1.0-beta.1  GitHub Pre-release，可继续功能调整
v1.1.0-rc.1    GitHub Pre-release，feature freeze，生产验收候选
v1.1.0         GitHub Release，只能从同版本 RC 晋升
```

日常 manual release workflow 接受 `patch|minor|major` 和 `beta|rc`。工具以最新 Stable 为基线计算目标版本，再对目标版本与通道选择最大现有序号加一，因此 `rc.10` 后是 `rc.11`，不做字符串排序。

仓库当前没有 Stable tag。首条发布线必须显式使用严格 SemVer `target_version_override`（预期 `v1.0.0`）；已有任意 Stable tag 后该 override 永久拒绝。创建 tag 前必须再次查询远端并确认目标 tag 与 Release 均不存在，以覆盖两个 manual run 的竞态窗口；任何碰撞都失败，不覆盖历史。

## Build once, promote many

Beta/RC workflow 对 main 的指定 commit 构建 API/Web 一次，向 GHCR 推送并记录 digest。Stable workflow 不运行 image build，只读取 RC Manifest、核验生产验收确认、检查 Stable 不存在，然后为相同 OCI digest 增加 Stable tag 并生成 Stable Manifest。

自动门禁必须证明：

```text
RC COMMIT == STABLE COMMIT
RC API DIGEST == STABLE API DIGEST
RC WEB DIGEST == STABLE WEB DIGEST
```

Stable Manifest 的 `promotedFrom` 指向 RC tag；发布说明范围是 previous Stable 到 current Stable，不是 RC 到 Stable。

Stable Promotion 固定 workflow dispatch 的 exact `github.sha`，并在首个外部 mutation 前重新验证该 SHA 仍是 `origin/main`、Stable tag/Release 仍不存在。该检查通过后，这个 SHA 成为不可变 publication transaction snapshot；后续普通 main 推进不会把已经开始的 exact RC transaction 中途终止。最终创建 Git tag/Release 前仍再次检查目标不存在，且 release producer concurrency 串行化 AniMemo 发布事务。

## Manifest schema

权威 JSON Schema 是 `release/release-manifest.schema.json`，v1.1 Installer 当前只接受 `schemaVersion` 为 `2`。Manifest 包含：

- release version/channel/commit/UTC timestamp/promotion source；
- API/Web 固定 GHCR repository、`linux/amd64` platform 与 digest；
- qualified PostgreSQL/Redis repository、platform 与不可变 digest；
- `v1.1-standard` deployment profile、Installer material archive/逐文件 identity 与 platform qualification identity；
- minimum updater version；
- database、configuration 与 Plugin SDK compatibility；
- release notes identity；
- GitHub Actions/SLSA provenance identity；
- Manifest 与 checksum 文件名。

Manifest 中两个 commit 身份有不同职责：

```text
release.commit          = 构建 API/Web 的应用 commit
provenance.sourceCommit = 实际运行签署该 Manifest workflow 的 commit
```

Beta/RC 通常由同一 Release workflow 产生，因此两者可相同。Stable 不重新 build，继续保留 RC 的 `release.commit` 与 API/Web digests，但 Stable Manifest 由 promotion workflow 签署，所以 `provenance.sourceCommit` 可以是后续 commit。消费者必须按各自语义验证，不能把 Stable promotion commit 冒充应用构建 commit。

Schema 拒绝 image `tag` 字段、缺失 digest、非 40 位 commit、未知字段和未知 schema version。v1.1 没有 schema-v1 dual reader。

## Compatibility model

数据库与配置使用显式的 application compatibility contract ID，不使用 Django migration 文件序号。每个应用 Manifest 同时声明：

- `contract`：运行/迁移后产生的状态契约；
- `appAccepts`：该应用能够读取的状态契约；
- migration `required` 与 `none|additive-backward-compatible|breaking-blocked`；
- `applicationRollback`：`safe|conditional|blocked`。

目标应用只有在接受当前数据库与配置 contract、支持所有 enabled plugin 的 `sdkApi`，且 updater 版本满足 minimum 时才是 Safe Switch。迁移后回退时重新用 Previous Manifest 对当前 contract 计算：接受则只回退 API/Web、数据库保持当前；不接受则 Unsafe Downgrade 并阻断。`breaking-blocked` 不允许自动 migration 或 switch。

历史 v1.0 application contract 是：

```text
database: animemo-db-v1
configuration: animemo-config-v1
Plugin Manifest: 2
Plugin SDK API: 2
runtime: trusted-in-process
```

该 metadata 是 additive Core release metadata，不改变 Plugin Manifest v2，也不实现 Runtime v3。

## Provenance

Release workflow 使用 GitHub 官方 `actions/attest` 产生 SLSA provenance；OCI attestation 绑定 image repository 与 digest，文件 attestation 绑定 Manifest bytes。Update Agent 通过 `gh attestation verify` 固定：

```text
repository: yanyuhanyue/AniMemo
signer workflow: .github/workflows/release.yml
source digest: manifest commit
predicate: https://slsa.dev/provenance/v1
```

这里的 Manifest source digest 使用 `provenance.sourceCommit`；OCI build provenance 仍绑定 `release.commit` 和 exact image digest。Stable Manifest 由 `.github/workflows/promote-release.yml` 签署，同时仍保留并验证 RC images 的原 build provenance。仓库不保存长期签名私钥，不定义自制密码协议。GitHub 官方文档确认 container image 可用 `actions/attest` 的 `subject-name`/`subject-digest` 和 `push-to-registry`，并可用 `gh attestation verify oci://... -R yanyuhanyue/AniMemo` 验证。

Release Dry Run 只有 `contents: read`，会构建本地 OCI archive、生成 Manifest/checksum，并输出 `provenance-plan.unsigned.json` 来验证 SLSA subject、commit 与 workflow 输入。该文件明确不是密码学签名；只有非 Dry Run 的 publish job 才申请 `id-token: write` / `attestations: write`，调用 `actions/attest` 产生可验证证明。

## Tool interface

## Phase 3C Installer Material Profile

The pre-production v1.1 Release Manifest advances explicitly to schema v2 for
the canonical Installer. A Release publishes exactly:

```text
release-manifest.json
deployment-contract.json
installer-materials.tar
checksums.txt
```

`installer-materials.tar` is deterministic, uncompressed USTAR. The closed
deployment/material contract binds every regular member by canonical relative
path, SHA-256, byte size, mode, and semantic installation role. It includes the
Installer, Updater, Release verifier, shared durability runtime, fixed Compose
files, launcher/systemd/sysusers/tmpfiles assets, managed-config and operation
schema/runtime, platform qualification material, and a complete offline
wheelhouse. Symlinks, hard links, special files, absolute/parent paths,
duplicates, uncontracted members, and size/count excess fail closed. The
Installer never resolves packages online or fills missing bytes from a source
checkout.

Manifest v2 binds four `linux/amd64` exact images: API, Web, PostgreSQL, and
Redis. The qualified dependency baseline is:

```text
docker.io/library/postgres@sha256:075f7ba66bc9b3ce7d6b8b635208ff61cd7cf1a67d71ec530eec5d7ae0cbe571
docker.io/library/redis@sha256:9702d01c1f10c3ea9f48211b4362e44f154ff02d063e6f7268eba804059f53bf
```

Consumers receive `VerifiedReleaseMaterials`, which owns a durable verified
material root and exposes only role/path and four-image lookup Interfaces. Plan
may use cached verified evidence; execute refreshes GitHub Release metadata,
assets, checksum, tag, provenance, attestation, contract, archive, and aggregate
identity. Cache names and directories never become authority.

Stable remains build-once/promote-many: promotion creates the signed Stable
Manifest/checksums for the promoted tag while copying the RC deployment
contract and material tar byte-for-byte and retaining all four image
identities. It does not rebuild application or Installer material bytes.
Schema-v1/three-asset Releases are not accepted by the v1.1 Installer and no
dual reader exists.

### Partial prerelease sequence reservations

Once a prerelease sequence has produced public or immutable external material,
including GHCR image tags or provenance attestations, that sequence is consumed
even if the Release Producer stops before creating a Git tag or GitHub Release.
The partial material must not be deleted, overwritten, or relabeled to reuse the
sequence. `release/publication-reservations.json` records these non-reusable
identities, and version resolution selects the next sequence from the union of
actual prerelease Git tags and non-reusable reservations.

The reservation ledger is only a source-controlled version-sequence guard. It
is not a Git tag, Manifest, installable release, deployment authority, or
Release Authority. The next candidate uses the next sequence, while the Stable
baseline and previous-Stable boundary continue to derive only from actual
Stable Git tags. Stable promotion remains limited to a complete immutable RC.
Authoritative `resolve-version` calls must provide the canonical ledger through
`--publication-reservations-file`; a missing, duplicate-key, or invalid ledger
fails closed instead of falling back to Git tags alone.

工具入口为 `python -m release.cli`：

```text
resolve-version
generate-manifest
validate-manifest
generate-provenance-plan
previous-stable
promote-manifest
write-checksums
```

成功输出 JSON；契约错误退出 `2` 并输出 `{code, detail}`。Workflow 与 Update Agent 共用同一 Manifest validator，避免产生端与消费端各自猜测语义。
