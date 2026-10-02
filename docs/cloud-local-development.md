# 云端与本地交替开发

## 分支与文件归属

- `main` 是受审查的集成基线；不在 `main` 直接开发，不强推，不自动合并。
- `codex/cloud-integration-20261002` 保存这次已脱敏开发快照的集成提案，使用草稿 PR。
- 后续每项任务单独建分支：云端 `codex/cloud/<task>`，本地 `codex/local/<task>`。
- 同一任务在同一时段只有一个写入方。接手前写明基准 commit、负责模块、会改的文件、测试和暂停点；交回后原写入方停止修改这些文件。
- 当前 Runtime baseline-only 控制器、授权边界和测试由云端维护。本地先做独立副本的源码验收；实际 VM 验收须另外获得批准。
- `bootstrap_kit/`、`installer/`、`updater/` 或同一测试文件有重叠时，先在 PR 中确认归属，再动代码。分支能隔离修改，不能保证同一行永不冲突。

## 接手到全新目录

以下 `HANDOFF_SHA` 必须替换为交接消息给出的、已回读验证的完整远端 commit；不要用本地旧快照 SHA 替代。

```sh
git clone --no-checkout https://github.com/yanyuhanyue/AniMemo.git AniMemo-local-task
cd AniMemo-local-task
git fetch origin codex/cloud-integration-20261002
git cat-file -e HANDOFF_SHA^{commit}
git switch -c codex/local/validation-YYYYMMDD HANDOFF_SHA
git status --porcelain=v1
git rev-parse HEAD
```

目录名必须不存在。`status` 必须为空，`HEAD` 必须等于交接 SHA；不符合就停止。保留旧开发目录和未提交内容，不用 `reset --hard`、`clean`、覆盖复制或自动 stash 来“修好”状态。

已有任务目录续接也要先检查 `git status --porcelain=v1`。先 `fetch`，再在自己的任务分支合入指定交接 commit；不要在双方都在写的同一分支执行 `pull`。遇到冲突，保留双方内容并返回路径和冲突说明给当前模块负责人，不选择全量 ours/theirs。

## 交回与草稿 PR

每次交回列明：任务分支、完整 commit、基准 commit、改动路径、已运行的准确命令与环境、通过/失败/未运行阶段、下一步和当前写入方。

```sh
git status --porcelain=v1
git diff --check
git fetch origin
git log --oneline --left-right HEAD...origin/codex/cloud-integration-20261002
```

只提交本任务文件，不 `git add .`，不提交私有回执、密钥、VM、凭据、个人绝对路径或机器缓存。推送自己的任务分支后创建草稿 PR。目标分支在测试后变化时，先合并/解决冲突，再重跑受影响测试；合并前核验最终 commit 的 CI、评审和合并条件。通过部分合成测试不能替代全量 CI、实际 Runtime 或 Formal 验收。

## 可重复的定向验证

CI 使用 Python 3.12.10、Node 20。当前云端快速验证使用 Python 3.12.14；这是已披露的版本差异，不是精确 CI 环境。当前五模块不需要 Node、Docker、数据库或 VM。

在已有且已批准的 Python 3.12.10 环境中，可用独立 venv 和原锁文件准备运行环境；不要混用工具锁的 packaging 版本，不改锁或跳过哈希校验：

```sh
python3.12 --version
python3.12 -m venv .venv-runtime
.venv-runtime/bin/python -m pip install --require-hashes -r release/requirements.lock
.venv-runtime/bin/python -B -m unittest scripts.tests.test_runtime_baseline_only scripts.tests.test_runtime_development_boundary scripts.tests.test_single_runtime_development scripts.tests.test_runtime_target_handoff scripts.tests.test_runtime_ui_retry_policy -v
```

Windows 可使用该 venv 的 `Scripts/python.exe`。解释器补丁版本须实际确认；如果版本不符，应报告差异，不能宣称复现了 3.12.10。

完整 Installer/updater 组合验证需要 `release/requirements.lock` 中的 `cramjam==2.11.0`。曾经的顶层 production 导入使单纯的非法 profile 拒绝检查也依赖它；现已推迟到真正执行分支，五模块可先在已有开发环境中尝试，缺少真实依赖时再按锁补齐，不反复重建环境。更大范围的 scripts 测试按 CI 另需 durability lock 和 Node 依赖；不要把本节的五模块命令宣传为整个项目测试。

Ruff 使用单独工具 venv，按 `scripts/requirements-tools.lock` 安装，版本为 0.16.2。定向规则为 `E9,F63,F7,F82`；检查本任务改动的 Python 文件，并执行 `git diff --check`。前端工作才需要 Node 20 与 `npm ci`，安装或构建前先确认资源窗口。

## 2026-10-02 集成来源与验收边界

- 远端基线：`94f64e7fba10f4a013281d599e695c3d6da36208`，保留其 Git 历史。
- 本地原 V2 来源：`16453bc7f4b72d47a6ae9b13028d04da74ab7d4f`。
- 本地诊断修复 HEAD：`ddd894596884354172ed29cb330f74bd1e8f9baa`。
- 已审查但未提交的工作树：`ebc9bb32fd81d0def21707182aa71acbcbaa97b1`，含九个当前改动。
- 脱敏导出树：`f2b5a65274d0b4216e07c76cb0de615ae98410a7`。它省略了七个历史报告，省略不构成删除远端文件的指令。
- 上述两个本地 commit 不在当前 GitHub 可读取的历史中。此次导入是保留远端父提交的源码快照集成，不伪造这些 commit，也不把导出树当作实际执行身份。
- 云端修复了 baseline-only 嵌套目标的零捕获额度，以及停止/清理完成后的错误反馈。关闭过程的 `completion.json` 明示 `BEFORE_HOLD_RELEASE`、`final_result=false`；最终结果以全部清理后导出的运行报告为准。
- 本地验收仍为 `LOCAL_ACCEPTANCE_NOT_RUN`。已有 V2 入口与额度已消费，不能重跑或恢复。新的真实 baseline、源码冻结和 material rebind 仍需单独批准。
- 真实 Guest 缺少哪一项前置条件仍未查明。不得根据云端依赖、合成测试或公开分支推断 Guest 事实，也不得制造实际验收日志。

## 给接手模型的最小任务

先核对交接 SHA 和干净任务分支，读取本文件及 Runtime handoff 文档。只在新独立目录进行本任务指定的源码/合成验证，保留旧目录和私有材料不动。返回实际命令、环境版本、结果与失败；未经新的明确授权，不执行 Provider、VM、sudo/密码捕获、真实 baseline、旧 V2 入口、签名、发布或合并。涉及共享模块改动先报告给当前云端负责人，不并发覆盖。

## 私有 operator overlay 合同

公开 checkout 的 `runtime_baseline_only_policy.py`、`runtime_ui_retry_policy.py` 和 Runtime handoff 入口不包含真实 operator 绑定，相关字段为 `None`。实际执行请求在任何历史回执读取前以 `OPERATOR_BINDING_REQUIRED` 失败；通用导入、开发和合成测试不依赖本地电脑，也不需要这些值。

测试只通过临时内存 fixture 注入明确的 TEST_ONLY 标识、临时文件和合成回执，不生成或持久化执行授权。生产代码没有自动读取环境变量、扫描目录或加载任意配置的后门。

真实 task/permit、历史回执身份和入口绑定保存在已授权的私有交接包中。需要真实验收时，由负责人在单独的私有验收副本中整合经过哈希核验的三个完整绑定模块与最新通用修复；不能把私有值提交到公开分支，不能把公开 commit 当作已冻结的实际执行身份。该私有源仍需独立审查、真实源码冻结和 material rebind，以及新的 A/B 和运行批准。

公开测试与私有 overlay 测试分别记录结果。仅完成公共测试不表示 overlay 已验；本地仍为 LOCAL_ACCEPTANCE_NOT_RUN。任何旧入口都不能因为换成公开分支或补上 overlay 而恢复额度。
