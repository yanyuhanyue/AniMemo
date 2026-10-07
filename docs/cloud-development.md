# 云端开发交接

## 代码位置和范围

本地源代码位于 `animemo-next`，在 [yanyuhanyue/AniMemo](https://github.com/yanyuhanyue/AniMemo/tree/codex/animemo-next) 的 `codex/animemo-next` 分支中位于仓库根目录。当前第一阶段覆盖账号、番剧手账、观看记录和导出。后续从这一实现继续。

云端环境选择仓库 `yanyuhanyue/AniMemo` 和分支 `codex/animemo-next`，然后在仓库根目录执行下述命令。本地 `.local/` 配置、数据库和 `node_modules/` 不随 Git 同步，云端会创建自己的开发数据。

## 云端环境要求

- Linux。
- Node.js 24.12+、npm。
- Go 1.26.6+，或在环境设置中准备兼容工具链。
- Docker Engine 与 Compose v2，可实际启动容器。
- 包源与镜像源访问权限：npm、Go modules、Docker Hub，以及 Dockerfile 使用的 `gcr.io`。

先检查：

```sh
node --version
npm --version
go version
docker info
docker compose version
```

如果 `docker info` 失败，应先修复云端环境，不能将安装了 Docker CLI 记作容器验证通过。

## 从源码开始

所有命令在本项目根目录执行：

```sh
. ./tooling/env.sh
npm ci --ignore-scripts
npm run db:start
npm run check
npm test
npm run test:api
npm run build
npm run containers
```

`npm run containers` 构建应用镜像，并使用开发数据库配置启动应用。检查：

```sh
curl --fail http://127.0.0.1:18081/api/ready
curl --fail http://127.0.0.1:18081/
```

在源码开发模式下运行 `npm run dev`。如果云端预览使用外部 HTTPS 域名，把 `PUBLIC_ORIGIN` 设置为准确的预览来源（不带路径），使 Cookie 与写请求来源校验保持一致。Vite 默认只监听回环地址；如环境需要端口代理，按该环境的实际路由方式设置监听地址和允许主机，保持允许范围明确。

## 文件管理

源码按 `server/`、`web/`、`contracts/`、`tooling/`、`deploy/`、`docs/` 分类。缓存、数据库、日志、截图和编译结果全部落在项目 `.local/`。开发脚本会设置 npm、Go、临时目录和 Docker 客户端配置路径；不要改成用户主目录或共享目录。

## 云端接手后的第一项工作

实际执行 Linux 镜像构建、Compose 启动、`/api/ready`、注册登录、创建番剧与记录观看，再验证容器重建后数据仍然存在。本地数据库测试不能代替这一组容器检查。

通过后再进入第二阶段的媒体、资料源和导入功能。

参考：[Codex Cloud 环境说明](https://learn.chatgpt.com/docs/environments/cloud-environments)。
