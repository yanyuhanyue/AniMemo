# AniMemo

一个以 Go、TypeScript 和 PostgreSQL 构建的私人番剧手账。

当前完成第一阶段：账号注册 / 登录 / 登出、番剧新增 / 编辑 / 删除、标签与评分、搜索筛选与排序、观看记录、进度统计、个人 JSON 导出，以及桌面和移动端界面。

项目源码沿用 [PolyForm Noncommercial 1.0.0](LICENSE)，适用范围见 [NOTICE](NOTICE)。

## 开发环境

推荐 Linux + Docker Engine。需要 Node.js 24.12+、Go 1.26.6+ 和 npm。

在项目根目录运行：

```sh
. ./tooling/env.sh
npm ci --ignore-scripts
npm run db:start
npm run dev
```

- 页面：`http://127.0.0.1:5177`
- API：`http://127.0.0.1:18081`
- PostgreSQL：`127.0.0.1:55432`

在页面创建自己的账号。应用不会生成默认用户或自动插入示例番剧。停止前后端用 `Ctrl+C`；停止本项目的开发数据库用 `npm run db:stop`。停止数据库保留数据。

`db:start` 使用 `deploy/compose.yaml` 启动 PostgreSQL，随机生成开发数据库密码并保存在忽略的 `.local/config.json` 中。数据绑定到 `.local/data/compose-postgres`。如果已有开发数据库，可通过 `DATABASE_URL` 指定；项目不会创建或重置外部数据库，但应用启动会应用自身迁移，因此请使用专用开发数据库。

支持通过 `ANIMEMO_GO` 指向已有 Go 可执行文件。Windows 中可先执行 `. ./tooling/env.ps1`，使手动命令的临时文件和包缓存也落在项目内。可选的本地 PostgreSQL 工具路径为 `.local/tools/pgsql/bin` 或 `ANIMEMO_PG_BIN`；Windows 原生 PostgreSQL 的非 ASCII 安装路径存在初始化限制，优先使用 Linux / Docker。

## 常用检查

```sh
npm run check:web    # 生成式 API 类型一致性 + TypeScript
npm run check:api    # Go vet + 依赖方向检查
npm test            # Go 业务规则 + 前端账号切换 + CI 选择测试
npm run test:api     # 真实 PostgreSQL HTTP、隔离、并发与迁移测试
npm run build       # 前端产物与 Go 可执行文件
```

`npm run check` 同时执行前后端静态检查。数据库集成测试只在指定开发数据库中创建随机 `test_*` schema，结束时删除该 schema；不会清空开发账号和番剧。

独立测试单个模块：

```sh
go -C server test ./internal/journal
node --test web/tests/session.test.mjs
```

改 API 后运行 `npm run contracts`。完整 HTTP 合同在 `contracts/openapi.json`；生成的前端类型位于 `web/src/api/schema.d.ts`。

## 容器运行

先执行 `npm run db:start` 创建本地开发配置，再执行：

```sh
npm run containers
```

页面和 API 同源运行在 `http://127.0.0.1:18081`。默认端口只绑定回环地址。设置 `PUBLIC_ORIGIN` 可以指定外部访问的 HTTPS 来源；非回环 HTTP 来源会被拒绝。容器中的应用使用非 root 用户、只读根文件系统、无额外 Linux capabilities。

容器镜像和完整 Linux 启动仍需在有 Docker Engine 的环境中实际验收；本地编译、真实数据库测试或 `docker compose config` 不代表镜像已经构建成功。

## 文件分类

```text
server/       Go 应用、业务模块、数据库迁移与测试
web/          React / TypeScript 界面、生成式 API 类型与前端测试
contracts/    OpenAPI 源合同
tooling/      开发命令、CI 选择与目录检查
deploy/       Dockerfile 和 Compose
docs/         架构、开发说明和阶段验收记录
.github/      CI 工作流
.local/       全部本地产物（不提交、不进入源码交接包）
  bin/        编译后的程序
  cache/      npm / Go / Vite / 浏览器缓存
  data/       本地数据库
  tools/      本工作区使用的辅助工具
  logs/       运行日志
  output/     构建产物、测试结果、截图、交接包
  tmp/        临时文件
```

`node_modules/` 是 npm 管理的依赖目录，已忽略。源码包不包含本地配置、数据库、浏览器会话或下载的工具链。

## 当前产品规则

- 番剧和观看记录仅属于当前账号，不存在匿名公开入口。
- 登录使用 HttpOnly、SameSite=Lax Cookie；HTTPS 来源启用 Secure。写操作校验 Origin。
- 编辑提交已读取的版本号。记录被其他请求修改后返回 409，用户重新打开后再编辑。
- 观看记录以请求 ID 防重复提交；同一 ID 携带不同内容会被拒绝。
- 观看进度为已经记录的最远话数，重看不会倒退。总话数为 0 表示未知；标记看完且总话数已知时，进度更新到总话数。
- 观看日期保留用户选择的日历日期，创建和修改时间另存 UTC 时间戳。
- 最近观看界面最多显示 100 条；JSON 导出包含该账号的完整记录，上限为 5000 部番剧 / 20000 条观看记录。

## 后续阶段

下一阶段是媒体、外部资料、导入和真实长任务。独立 Worker 在第一个长任务接入时实现。实例备份恢复、Go Agent、插件隔离和发行验收属于后续独立交付。

当前账号功能尚不包含邮箱验证、找回密码或管理后台；个人 JSON 导出不是实例备份。此阶段供开发和功能验证使用。

云端交接见 [docs/cloud-development.md](docs/cloud-development.md)，模块设计见 [docs/architecture.md](docs/architecture.md)。
