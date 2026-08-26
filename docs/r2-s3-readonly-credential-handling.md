# R2 S3 最小只读凭据处理合同

状态：RC.14 Candidate Acceptance 的强制操作员边界。

## 权限与用途

Candidate Acceptance 只接受 Cloudflare R2 S3-compatible API 的单 Bucket `Object Read only`
凭据。固定 Bucket 为 `animemo-release-mirror`；canonical 入口只执行
`ListObjectsV2`、`HeadObject`，并保留 `GetObject` 作为唯一额外读取能力。它没有任何写入、
删除、复制、multipart、Bucket 或账户管理方法。

Cloudflare REST List Objects 使用 Bearer Token，而 Bucket-scoped `Object Read only` 凭据是
S3 凭据，两者不可互换。实现不在 S3 与 REST 之间自动切换，也不以公共 CDN 404、GitHub
Release 或其他 transport 代替 Origin 证明。

## 创建和注入边界

后续任务中，凭据必须由操作员在可信浏览器中手工创建，并直接写入受控 Secret Store 或
当前受控进程环境。Secret 显示页不得由浏览器自动化、DOM 读取工具或浏览器控制插件读取；
不得截图、录屏、保存页面正文或写入命令行历史。若人工复制不可避免，完成注入后立即清空
剪贴板。

程序只读取以下专用变量：

```text
ANIMEMO_R2_S3_ACCESS_KEY_ID
ANIMEMO_R2_S3_SECRET_ACCESS_KEY
ANIMEMO_R2_S3_SESSION_TOKEN    # 可选
ANIMEMO_R2_ACCOUNT_ID
ANIMEMO_R2_JURISDICTION
```

不得通过 CLI 参数、URL/query、stdin、JSON、配置文件或 Evidence 传入 Secret。不得复用已
撤销或曾暴露的凭据；不得从日志、历史记录、剪贴板历史、截图或浏览器状态恢复凭据。

## 身份与网络边界

Account ID 必须匹配仓库固定 identity；Bucket 与 RC.14 Prefix 来自代码中的 canonical
release contract。jurisdiction 只允许仓库枚举值，endpoint 只能由 Account ID 与该枚举构造，
使用 HTTPS、S3 service 和 `auto` region。没有 endpoint override、HTTP、localhost、私网、
代理或 `file://` 路径。

AWS profile、`~/.aws/credentials`、EC2/ECS metadata 和通用 `AWS_*` 凭据不会被用作 fallback。
认证失败、授权不足、时钟偏移、错误 Account/Bucket/endpoint、Prefix 非空或响应异常都稳定
失败关闭。

## 输出与销毁

Access Key ID 与 Secret Access Key 同等敏感。Receipt、stdout、stderr、异常、日志、诊断
JSON、测试报告和 Evidence 均不得包含 Access Key、Secret、Session Token、Authorization、
Cookie、签名、canonical request、SDK credential repr 或 signed URL。统一 sanitizer 在输出前
移除这些值和不可信请求/响应诊断；禁止显示前后字符、长度组合或凭据哈希。

使用完成后按后续获授权的操作员流程撤销凭据，并从临时进程环境和 Secret Store 的临时槽位
清除。本合同本身不授权创建、调用或撤销任何真实凭据。
