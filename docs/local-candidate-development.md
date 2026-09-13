# Installer / Runner 本地集中验证

本地流程为：冻结本轮开发源码 → 用已验证的 Q 材料执行完整 Installer / Runner →
修复并重跑受影响检查 → 同一源码下三 Profile 干净预验收 → 集中推送完整 PR →
受保护合并 → 最终准确 main 的正式 Q 与 Candidate。

本地提交只用于冻结和记录每轮源码。调试过程中不需要推送提交或派发 Q。

涉及 Candidate 汇总/运输/消费者的变更，pre-pr 还需运行完整输出链：

```text
python -X utf8 -m scripts.candidate_receipt_regression --fixture scripts/tests/fixtures/candidate-wire-real-scale.json.xz --output-directory <new-local-test-directory>
```

该入口不启动 VM、不捕获密码、不调用 Origin 或工作流。fixture 保留旧执行的完整三份
Profile、两份 Origin 和计划；新构造的 Aggregate、wire、文件回读及下游消费全部标为
`NON_AUTHORITATIVE_LOCAL_REGRESSION`，不能回填旧正式结果。可通过 `--state-root` 读取
实际已验证材料，在本地验证 Publish Candidate 子消费者；未提供该上下文时必须到达正确
的缺失材料拒绝门。Freshness 输入组只组装并计量，不 dispatch。
该真实规模链路也由现有 `scripts/tests` 覆盖，在实际 Windows/Python 与 GitHub Linux
消费环境验证。约 400 条命令和完整 VM inventory 不裁剪；增长输入超出预算必须稳定拒绝。
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

宿主重启后不恢复旧秘密。下一轮保留原账本、已用槽位和首次 reserve 的 UTC 截止时间；
单调时钟起点变化时，必须通过无凭据、禁止重定向及缓存复用的 GitHub HTTPS Date
核对当前时间，再将剩余时长转换为本次启动的单调时钟上限。可信时间不可得、时间
不一致或原截止时间已到均拒绝捕获。不会因此增加轮数、改写 scope 或延长任务期限。

2026-09-13 操作员另行确认该任务剩余三轮延长十二小时。源码仅对已确认的原始
scope 内容摘要识别这一次固定延期，截止为 `2026-09-13T14:35:10.207901Z`
（北京时间 22:35:10；保守采用确认前的记录时间作为起点）。原 scope 和三个已用
槽位保持，后续仍只能使用第四至第六槽。每次 reserve 必须通过新鲜 GitHub HTTPS
Date 校验，转换成 owner 的单调时钟期限；每轮秘密上限不变。此固定延期没有
任意授权文件或 CLI 续期入口，也不适用于其他账本、材料或新的首次 reserve。

随后操作员确认在同一账本追加最多六轮，总上限为十二轮，继续沿用上述固定截止。
这项追加同样只匹配已确认原 scope 的内容摘要，要求前六槽保持已消费，只能新建
第七至第十二槽；原 `scope.json` 的六轮声明及历史记录保持原字节。普通新开发账本
仍限六轮。实际 reservation 记录生效上限、此前已消费数和追加授权的 scope 摘要。

操作员随后明确要求同一控制器保留到剩余轮次结束，免去逐轮手输。对该已确认
scope，第九至第十二槽可以使用 `scripts.development_controller`：它在首台新 Guest
核验后捕获一次，只在本机进程内存保留秘密，并在各轮之间持续持有原账本的独占锁。
每轮仍单独 reserve，旧槽不退款，最多三个新 Guest、九个固定角色交付；每轮的
capability、连接、租约和源码持有均须收尾后才能更换源码。普通已认证执行失败
且完整清理后，内存 owner 可以等待下一轮修复；该等待不按旧单轮的三十分钟空闲
规则清除，仍受原固定 UTC 截止限制。身份、传输、交付或清理不确定会终止 owner。
同轮三 Profile 通过、十二槽用尽、到期或主动关闭均清除秘密并释放控制器。

常驻控制器只读取闭合的公开请求：owner ID、下一轮编号、上一结果摘要和新的干净
checkout / SHA / tree。它拒绝重放、跨会话、材料改变和稳定控制器源码改变；各轮
使用同一代新导入的项目模块，内存 owner 和 scope 锁的模块保持不变。后续轮次
记录捕获次数为零，并以 `ROUND_CAPABILITY_*` 状态记录本轮能力关闭；总捕获次数
及最终清除状态由独立 owner 报告记录。该接口仅适用于本地开发，不改变正式
Candidate 的单次捕获与权限边界。

公开状态快照采用原子替换。Windows 读句柄短暂阻止替换时，只对已写完并同步的
快照发布做最多五秒重试；取消或 owner 到期会结束等待。持续失败或其他错误仍
关闭 owner，并尽力记录固定错误类别和系统错误码，不记录异常正文。
若状态发布故障已关闭 owner，而下一轮尚未 reserve、捕获和交付均为零且 Guest
及临时资源已清理，恢复入口可在原账本剩余额度内建立新 owner。它校验旧 owner
终态与账本已消费前缀一致，不恢复已经清除的秘密；新 owner 仍需一次原生输入。

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
使用上述已授权常驻控制器时，先关闭本轮能力并收尾，再决定是否可以保留内存 owner。
只有同轮三个 Profile 全部通过、捕获和角色计数正确、源码和资源校验完成，才记录
干净预验收通过。

## 执行覆盖和材料边界

开发 Runner 调用共享的完整 Installer 流程，验证真实平台准备、运行发行身份、
Doctor、严格 canonical CRUD / API / Web、内部 Docker network、systemd 地址族、
禁用隐式 pull 与实际命令库存。报告在 Host 再次核验这些观察与本轮绑定。
Guest root 在导入开发代码前先通过固定的 no-follow fd 复制与完整库存检查，封闭
材料树和开发源码树；原 Q 的 Manifest、Verified Identity 和材料目录保持。

开发安装树由本轮 Git 提交中的代码，以及原 Q 中未受 Git 跟踪的 wheels、pretrust
和平台材料共同生成。已删除的旧源码不会回填；当前源码覆盖不可变材料会被拒绝。
Guest 完整安装本轮 `durability`、`installer`、`release`、`updater`，随后读取实际安装
目录和五项启动器 / systemd 资产的库存。Guest 和 Host 都对照本轮封存源码核验，
缺失观察或安装了旧代码都不能通过。

开发组合根还把这个封存源码能力交给 Bootstrap Gate：原 Q 归档及其摘要继续核验，
已加载核心模块必须位于本轮封存树内，完整库存必须保持一致。开发 Bootstrap 身份
单独绑定执行库存；正式入口继续要求运行代码与其同源归档一致。平台诊断分别标记
计划和执行阶段，并只输出固定错误码，不输出异常正文或凭据。

当前入口允许复用材料进行 Installer / Runner 及宿主控制器 Python 修订。涉及 OCI、
依赖锁或静态发行材料的更改会在捕获前要求本地材料重建，不能拿旧材料给未执行的
新字节记 PASS。开发报告不替代正式 Q、三 Profile Candidate、Origin 前后态或
Freshness / Publish 消费。正式阶段仍使用合并后的准确源码及重新生成的同源材料。
