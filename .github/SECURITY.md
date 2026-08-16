# Security Policy

## 项目状态

AniMemo v1.1 当前仍处于 pre-production / stabilization 阶段。

在 v1.1.0 Stable 发布前，当前 `main` 与 v1.1 development 接受安全问题报告。历史开发 snapshot 仅用于工程记录，不构成长期支持合同。

## Supported Versions

正式 Stable support matrix 将在 v1.1.0 Stable 时冻结。在此之前，不对历史 snapshot 承诺正式支持周期；报告时请注明受影响的 exact SHA 或版本。

## Reporting a Vulnerability

请优先使用 GitHub Private Vulnerability Reporting 提交报告。不要在公开 Issue、讨论区或 PR 中披露可直接利用的漏洞细节。

报告建议包含：

- 受影响的组件或功能区域
- 受影响的 SHA、版本和运行环境
- 安全影响与攻击前提
- 最小化的复现步骤
- 已脱敏的最小日志
- 可选的缓解或修复建议

如果无法使用私有报告入口，请先联系维护者获取私下报告通道；在确认安全处理前不要公开漏洞细节。

## Sensitive Data

请勿在公开内容中提交 API keys、tokens、passwords、private user data、database dumps、production credentials、真实 session cookies 或其他 secret。日志必须在提交前完成脱敏。

## Disclosure

在修复、验证和协调完成前，请避免公开可能伤害用户的 exploit details。修复后的披露范围与时间将根据影响、修复状态和报告者协商确定。
