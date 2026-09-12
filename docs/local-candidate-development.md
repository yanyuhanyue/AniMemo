# Installer / Runner 本地集中验证

本地流程为：冻结本轮开发源码 → 用已验证的 Q 材料执行完整 Installer / Runner →
修复并重跑受影响检查 → 同一源码下三 Profile 干净预验收 → 集中推送完整 PR →
受保护合并 → 最终准确 main 的正式 Q 与 Candidate。

本地提交只用于冻结和记录每轮源码。调试过程中不需要推送提交或派发 Q。
已经完成的 Q 可以提供四套 OCI、离线 wheels、平台资格和发行材料；这些原字节保持。
开发入口单独封闭当前源码，报告同时记录材料源码和执行源码。

## 固定开发会话预算

开发入口只识别 `ANIMEMO_V2_LOCAL_INSTALLER_DEVELOPMENT_V1`。它与正式 Candidate
的单次范围分别计数，不能互换。识别该 ID 不构成操作员授权；实际使用前须明确批准
本任务的下列预算与具体材料绑定。

| 项目 | 范围 |
| --- | --- |
| 任务期限 | 首次 reserve 起最多 12 小时；同时验证 UTC 和宿主单调时钟 |
| 捕获预算 | 最多 6 轮，每轮最多一次原生 Console 捕获尝试 |
| 每轮用途 | FRESH_BASE → DOCKER_BASE → RUNTIME_BASE_OFFLINE，固定三个角色各一次，最多九次交付 |
| 秘密期限 | 每轮最多 12 小时，空闲最多 30 分钟，并受任务总期限限制 |
| 材料 | 首轮锁定 Q / attempt、材料 source / tree、Candidate version、Input / Verified digest |
| 开发源码 | 每轮独立的干净本地 commit / tree 和受持有文件库存；运行中不得改变 |
| 修改代码 | 先关闭当前 owner、撤销 grant、完成 Clone 收尾，再修改和冻结下一轮 |

固定账本为 `E:/4d9046d98ee16d687ac9d8945a42a97e32bd87b7ea264d950d48356db9951cc8`，
即开发 ID ASCII 字节的 SHA-256。账本中的六个槽位是同一个有限预算，不能通过换
源码、session、参数、Q、重启或删除记录恢复。准备和合成测试不创建真实账本。
取消、输入错误、部分写入或崩溃均消费已 reserve 的轮次。一个进程持有预算锁，
同时只能有一个活动捕获 owner；异常退出也不会恢复已经登记的次数。

密码只由操作员在唯一的本机原生 Console 输入，保留遮罩和退格，不经聊天、文件、
环境、argv 或普通 stdin 捕获。每次交付继续独立验证准确 Guest、连接、租约、
源码、材料及固定用途。捕获失败不会自动再次提示。

## 本地入口

先完成本地提交，再从该冻结 checkout 的专用原生 Console 执行：

```text
python -I -S -B <经过核对并设置固定 sys.path 的本机启动器>
```

启动器只调用下面的固定模块入口，公开参数分别提供两套源码身份：

```text
python -B -m scripts.local_candidate_development
  --verified-candidate-digest sha256:<材料 Verified digest>
  --qualification-run-id <材料 Q>
  --material-source-sha <材料源码 SHA>
  --material-source-tree <材料源码 tree>
  --execution-source-sha <本轮开发源码 SHA>
  --execution-source-tree <本轮开发源码 tree>
  --execute
  --authorization-id ANIMEMO_V2_LOCAL_INSTALLER_DEVELOPMENT_V1
  --result <尚不存在的本轮公开结果文件>
```

省略 `--execute` 和 `--authorization-id` 为 plan-only；不启动 VM、不捕获密码。
正式 Candidate 入口拒绝开发 ID 和开发计划。开发结果使用
`animemo.local-installer-development-profile-report/v1` 及
`animemo.local-installer-development-batch-report/v1`，不产生可发布的 Candidate Receipt。

每轮都从原模板对应快照生成新的完整 Clone，不连接或恢复旧失败 Guest。共享阻断
出现后停止其余 Profile，保留首个错误、阶段和退出码，关闭秘密并收尾。
只有同轮三个 Profile 全部通过、捕获和角色计数正确、源码和资源校验完成，才记录
干净预验收通过。

## 执行覆盖和材料边界

开发 Runner 调用共享的完整 Installer 流程，验证真实平台准备、运行发行身份、
Doctor、严格 canonical CRUD / API / Web、内部 Docker network、systemd 地址族、
禁用隐式 pull 与实际命令库存。报告在 Host 再次核验这些观察与本轮绑定。
Guest root 在导入开发代码前先通过固定的 no-follow fd 复制与完整库存检查，封闭
材料树和开发源码树；原 Q 的 Manifest、Verified Identity 和材料目录保持。

当前入口允许复用材料进行 Installer / Runner 及宿主控制器 Python 修订。涉及 OCI、
依赖锁或静态发行材料的更改会在捕获前要求本地材料重建，不能拿旧材料给未执行的
新字节记 PASS。开发报告不替代正式 Q、三 Profile Candidate、Origin 前后态或
Freshness / Publish 消费。正式阶段仍使用合并后的准确源码及重新生成的同源材料。
