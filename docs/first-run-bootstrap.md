# First-run Bootstrap

AniMemo 不携带默认管理员用户名、邮箱或密码，也不会把已有普通账号提升为管理员。全新安装通过一次性服务器初始化码和浏览器 `/setup` 创建唯一的首位管理员。

## 状态与不变量

数据库中的单例 `InstallationState` 是唯一权威状态：

- `uninitialized`：尚未创建首位管理员；普通注册关闭，前端所有路径转到 `/setup`。
- `initializing`：事务内的短暂锁定状态；并发提交不会创建第二位首装管理员。
- `initialized`：首装完成；初始化码被清空并删除，后续 bootstrap 不会重新开放入口。

用户数量、固定用户名和环境变量都不参与运行时状态判断。迁移已有数据库时，只要旧库已经存在账号，就保守地标记为 `initialized`，避免升级把公网首装入口重新打开。迁移不会修改任何已有账号权限。

## 全新安装

1. 用 `deploy/prepare-host.sh` 创建持久目录。`private` 必须由 API 进程 UID/GID 拥有并使用 `0700`。
2. 显式运行 migration，再运行 Compose `bootstrap` job。bootstrap 会同步幂等默认数据并生成或复用有效初始化码。
3. 在宿主机读取 `${ANIMEMO_DATA_ROOT}/private/setup-code`。生产默认路径为 `/data/animemo/private/setup-code`，文件权限为 `0600`。
4. 在同源站点打开 `/setup`，填写一次性初始化码、一个全新的用户名、必填邮箱和强密码。
5. 成功后进入 `/admin-login`。服务器同时创建 `UserSettings` 与审计记录，删除明文码、清空哈希并把安装状态锁为 `initialized`。

初始化码只保存在私有文件和数据库密码哈希中。状态 API 仅返回状态、是否可提交和过期时间，不返回明文码或哈希；前端不写入 Local Storage 或 Session Storage。

## 失效、重试与恢复

- 默认有效期为 3600 秒，可用 `FIRST_RUN_SETUP_CODE_TTL_SECONDS` 在 300–86400 秒内调整。
- 错误尝试由独立的 IP、账号和组合限流约束；数据库计数会在 `FIRST_RUN_SETUP_MAX_ATTEMPTS`（默认 8）处饱和，但远程错误请求不会消耗或删除服务器端有效初始化码，避免攻击者利用全局次数上限使真实管理员无法初始化。昂贵哈希校验在安装状态行锁之外完成，错误请求只执行一次短原子计数更新。只有初始化码过期或成功使用时才会清除活动哈希和明文文件。
- 有效码和文件都存在时，重复运行 bootstrap 会复用原码，不会无提示轮换。
- 活动哈希存在但私有文件丢失时，重新运行 bootstrap 会生成新码并替换旧哈希。这覆盖“文件已删除但数据库事务未提交”等中断窗口。
- 状态已是 `initialized` 时，bootstrap 只清理可能残留的安全文件，不会签发新码，也不会创建或重置管理员。
- 首次成功初始化会生成独立认证 epoch。数据库恢复后必须在启动 API 前运行 `python manage.py rotate_authentication_epoch --confirm-restore`，使恢复快照中曾经签发、撤销或轮换过的全部 JWT 失效；不得只依赖随数据库一起恢复的 token blacklist 或撤销表。命令不会打印新 epoch。
- 用户名或邮箱已存在时提交返回冲突；已有用户绝不会被复用或提权。改用新的身份重新提交有效码。
- 若安装状态记录缺失或私有路径不是受控普通文件，接口与命令都会 fail closed。先恢复数据库迁移或修正目录所有权/权限，再重新运行 bootstrap。

不要把初始化码复制到工单、聊天、CI artifact、shell history 或浏览器存储。不要用 `createsuperuser`、旧 `--create-admin` 参数或手工数据库更新绕过状态机。

## 本地开发

`scripts/dev.ps1` 和 `scripts/dev.sh` 会创建 `runtime/private`、运行 migration/bootstrap，并输出初始化码文件路径。读取 `runtime/private/setup-code` 后访问 `http://localhost:5173/setup`。使用同一个数据库再次启动时，已初始化状态保持不变。

## CI 与发布门禁

Fresh Docker Release Gate 和隔离性能门禁都必须通过真实 HTTP `/api/v1/setup/` 完成一次初始化，再证明 `/api/v1/setup/status/` 已锁定。CI 客户端只接受回环 HTTP 地址、`.example.test` Host 和显式 `--confirm-isolated`；合成密码与初始化码不会进入参数、日志或 artifact。

Stateful Upgrade Gate 使用带既有账号的基线数据库，验证迁移后状态保持 `initialized`，不会重新生成首装码。该门禁只操作临时 Compose 项目，不接触生产。
