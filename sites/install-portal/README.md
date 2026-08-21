# AniMemo Install Portal

```text
COMPONENT_ID=animemo-install-portal
SOURCE_DIRECTORY=sites/install-portal
DEFAULT_PUBLIC_ORIGIN=https://install.animemo.cc
SOURCE_DIRECTORY_DOMAIN_INDEPENDENT=YES
RELEASE_AUTHORITY=GITHUB_IMMUTABLE_RELEASE
PORTAL_ROLE=BOOTSTRAP_TRANSPORT_AND_INSTALLATION_UX
PORTAL_IS_RELEASE_AUTHORITY=NO
```

源码组件身份与当前部署域名彼此独立；未来更换公开域名时，不需要再次重命名源码目录。当前公开入口及 bootstrap transport URL 仍分别为 `https://install.animemo.cc` 与 `https://install.animemo.cc/install.sh`。

`release-state.mjs` 是部署前由可信 Release Producer 生成的闭合展示合同。仓库默认状态为 `NO_PUBLIC_RELEASE`；缺字段、额外字段、draft Release、非 immutable Release、非 canonical GHCR digest 或未资格认证的 mirror 都会失败关闭。浏览器不决定 latest，也不把该静态页面变成 Release Authority。

`_headers` 是 Cloudflare Pages 或等价静态托管必须应用的安全与缓存合同。本目录仅包含同源静态依赖；本任务不会执行生产部署。
