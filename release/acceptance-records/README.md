# RC Live Acceptance 记录

此目录只接收已公开 RC 的 `animemo.rc-live-acceptance/v2` 正式验收记录。文件名必须是精确 RC tag 加 `.json`，例如 `v1.1.0-rc.1.json`。

记录必须：

- 由正式 Formal 三 Profile producer 生成，嵌入 Fresh、Docker、Offline 三份 Profile receipt、aggregate receipt、execution receipt 与 RC live acceptance input，并通过 `release/rc-live-acceptance.schema.json` 和 `release.acceptance` 逐层重算校验；
- 逻辑 Authority 绑定 RC tag、commit/tree、三项发行材料、API/Web OCI 摘要、三个 Profile Authority、Formal aggregate Authority、Formal RC Authority 与固定 producer contract identity；
- 将 VM 基线/snapshot/clone 身份、时间、操作员、run/workflow/environment、实际工具摘要和 Formal execution/receipt 摘要仅保存在内嵌 Formal evidence 或 execution receipt；这些观察不得改变逻辑 Authority identity；
- Candidate、publication、Formal 输出、`SHA256SUMS` 与独立封印必须位于同一不可序列化 continuation lifetime authority 闭合的私有边界；只有内部逐文件回读验证完整清单及标准独立 seal 行后才可释放路径句柄；
- 经正常 Pull Request 审查合入受保护的 `main`，不得从工作流输入或任意本地上传直接取得 Stable 权限；
- 不含密码、令牌、环境转储或其他凭据。

`promote-release.yml` 只从当前受信 Git commit 的固定路径读取记录。记录缺失、未被 Git 跟踪、Formal evidence 无法闭合重算、身份不一致或任一 Profile/Doctor 未通过时，Stable 提升均 fail closed。
