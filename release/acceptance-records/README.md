# RC Live Acceptance 记录

此目录只接收已公开 RC 的 `RC_LIVE_ACCEPTANCE_RECORD_V1`。文件名必须是精确 RC tag 加 `.json`，例如 `v1.1.0-rc.1.json`。

记录必须：

- 由 Fresh Base live acceptance 工具生成，并通过 `release/rc-live-acceptance.schema.json` 和 `release.acceptance` 校验；
- 绑定 RC tag、commit、三项发行材料、API/Web OCI 摘要和三类 VM 基线身份；
- 经正常 Pull Request 审查合入受保护的 `main`，不得从工作流输入或任意本地上传直接取得 Stable 权限；
- 不含密码、令牌、环境转储或其他凭据。

`promote-release.yml` 只从当前受信 Git commit 的固定路径读取记录。记录缺失、未被 Git 跟踪、身份不一致或 Doctor 未通过时，Stable 提升均 fail closed。
