# AniMemo CI Pipeline Efficiency Report — 2026-08-12

## CI authority correction

```text
PERSONAL REPOSITORY MODE: ACTIVE
MERGE QUEUE REPOSITORY SETTING: NOT ENABLED
CURRENT AUTHORITATIVE PRE-MERGE GATE: PRE-MERGE FULL GATE
MERGE_GROUP SUPPORT: READY FOR FUTURE ORGANIZATION MIGRATION
MAIN PUSH: LIGHTWEIGHT VERIFY ONLY
```

The previous documentation treated `merge_group` as though every ordinary PR
would enter a Merge Queue. The repository has no ruleset and no enabled Merge
Queue, so that event is not the current merge authority. The corrected active
flow is:

```text
PR Fast
-> Pre-Merge Full
-> Squash Merge
-> main Lightweight
```

The manually dispatched Pre-Merge workflow accepts a PR number and exact head
SHA, validates the live PR against current `main`, forces Full Regression and
Release Gate through reusable workflows, revalidates after both gates, and
writes `pre-merge-authority` to that exact commit. A later commit has a different
SHA and therefore has no authority status until the workflow is run again.

`merge_group` still forces the same complete gates and remains the future-ready
path if the repository later moves to an Organization with Merge Queue enabled.
It is compatibility support, not the active mechanism.

## Implemented pipeline

`.github/workflows/ci.yml` 与 `.github/workflows/release-gate.yml` 现在共享 `scripts/ci_classify.py` 的 changed-files 风险分类：

| Change class | Product gates |
| --- | --- |
| docs-only | `docs-only` fast path |
| frontend | frontend |
| backend | backend |
| auth / API contract / migration / media storage | backend + PostgreSQL |
| plugin | plugins |
| integration | backend + PostgreSQL + plugins + Bridge/runtime |
| bridge | Bridge + runtime |
| dependencies / CI / deployment / shared contract / mixed high-risk | full CI + Release Gate |

`merge_group`、显式 Full workflow dispatch 与 Pre-Merge reusable workflow call
均强制完整门禁。PR Fast 使用可取消 concurrency；Pre-Merge 使用独立的 per-PR
非取消 concurrency，不会被新的 PR Fast run 误取消。main push 的 CI product
jobs 与 Release Gate 的昂贵 Docker/stateful jobs 均跳过，只运行轻量 sanity。
昂贵验证留给 PR high-risk、当前权威 Pre-Merge Full，以及未来真实启用后的
Merge Queue。

## Fast-fail and duplicate-work controls

- changed-files 分类和 `ci-fast-fail` 在安装依赖、启动 PostgreSQL、Playwright、Docker 之前运行。
- 保留既有 frontend/backend/plugins/Bridge/PostgreSQL/Docker/stateful-upgrade job 名称，同时新增稳定聚合 `pr-fast-gate`；分支保护不再依赖会合法 skip 的子系统 job。
- frontend build 仍只在 frontend job 执行一次；本轮没有引入重复 build job。
- PostgreSQL gate 只在并发、auth、API、migration、integration、shared contract、media storage 或 full gate 风险下启动。
- Pre-Merge Full 通过 `candidate_sha` 把所有 AniMemo checkout 固定到最终 PR head；AstrBot 外部 runtime checkout 继续使用其独立 matrix ref。
- Pre-Merge 在 Full Gate 前后都校验 PR open/base/head/repository/current-main ancestry，并把 pending/success/failure 状态写到精确 candidate SHA。
- 同一最终 head 的权威 Full Gate 正常只执行一次；合并后的 main 不重复 Docker、stateful 或产品 Full Regression。
- `pip-tools==7.6.0` 与 pip 26 的内部 API 不兼容已通过 `scripts/requirements-tools.txt` 的 `pip<26` 固定解决。

## Evidence

- PR #56 full-gate run：changed-files 7s、ci-fast-fail 7s、frontend 1m16s、backend 5m04s、PostgreSQL 58s、plugins 20s、Bridge 12s、runtime 1m06s/1m26s、Docker 1m13s、stateful-upgrade 1m23s，全部 PASS。
- 本次改 workflow 的 PR 必须 full gate，因此没有拿 full-gate 与 docs-only/backend-only 做同口径端到端对照。
- `PR end-to-end time improvement: N/A`；在后续至少采集一条 docs-only、backend-only、frontend-only、high-risk PR 和 Pre-Merge Full run 后再计算 P50/P95。
- `Duplicate build count: 1`（frontend job 内单次 build；无新增重复 build）。
- `Merge queue repository setting: NOT ENABLED`；GitHub API 在 2026-08-12 确认当前仓库无 ruleset，个人仓库 main 保护没有 Merge Queue。
- `Current authoritative pre-merge gate: PRE-MERGE FULL GATE`。
- `Merge group support: READY FOR FUTURE ORGANIZATION MIGRATION`。
- main post-merge evidence：CI `31514756504` PASS（changed-files PASS，backend/frontend/PostgreSQL/plugins/Bridge/runtime/bootstrap/fast-fail skipped）；Release Gate `31514756716` PASS（`post-merge-sanity` PASS，Docker/stateful skipped）。

The correction PR itself is a CI change, so its existing PR-triggered classifier
must fan out to Full Regression and Release Gate before bootstrap merge. After
the workflow reaches `main`, branch protection can safely switch from the four
selective subsystem contexts to strict `pr-fast-gate` plus
`pre-merge-authority`. Enabling those new required contexts before the workflow
exists on `main` would deadlock the bootstrap PR.

## Operational boundary

CI/Release Gate 未执行生产部署、SSH、生产 smoke、远程 R2 cleanup 或 Cloudflare mutation。
