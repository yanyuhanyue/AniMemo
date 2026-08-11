# AniMemo CI Pipeline Efficiency Report — 2026-08-12

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

`merge_group` 与手动 workflow_dispatch 强制完整门禁。PR 使用可取消 concurrency；main push 与 merge_group 不取消正在运行的验证。main push 的 CI product jobs 与 Release Gate 的昂贵 Docker/stateful jobs 均跳过，只运行轻量 sanity；昂贵验证留给 PR high-risk 与 merge queue。

## Fast-fail and duplicate-work controls

- changed-files 分类和 `ci-fast-fail` 在安装依赖、启动 PostgreSQL、Playwright、Docker 之前运行。
- 保留既有 frontend/backend/plugins/Bridge/PostgreSQL/Docker/stateful-upgrade job 名称，降低分支保护迁移风险。
- frontend build 仍只在 frontend job 执行一次；本轮没有引入重复 build job。
- PostgreSQL gate 只在并发、auth、API、migration、integration、shared contract、media storage 或 full gate 风险下启动。
- `pip-tools==7.6.0` 与 pip 26 的内部 API 不兼容已通过 `scripts/requirements-tools.txt` 的 `pip<26` 固定解决。

## Evidence

- PR #56 full-gate run：changed-files 7s、ci-fast-fail 7s、frontend 1m16s、backend 5m04s、PostgreSQL 58s、plugins 20s、Bridge 12s、runtime 1m06s/1m26s、Docker 1m13s、stateful-upgrade 1m23s，全部 PASS。
- 本次改 workflow 的 PR 必须 full gate，因此没有拿 full-gate 与 docs-only/backend-only 做同口径端到端对照。
- `PR end-to-end time improvement: N/A`；在后续至少采集一条 docs-only、backend-only、frontend-only 和 high-risk merge_group run 后再计算 P50/P95。
- `Duplicate build count: 1`（frontend job 内单次 build；无新增重复 build）。
- `Merge queue repository setting: NOT RUN`；本轮只在 workflow 中加入 `merge_group` 触发与完整门禁，没有改仓库保护设置。
- main post-merge evidence：CI `31514756504` PASS（changed-files PASS，backend/frontend/PostgreSQL/plugins/Bridge/runtime/bootstrap/fast-fail skipped）；Release Gate `31514756716` PASS（`post-merge-sanity` PASS，Docker/stateful skipped）。

## Operational boundary

CI/Release Gate 未执行生产部署、SSH、生产 smoke、远程 R2 cleanup 或 Cloudflare mutation。
