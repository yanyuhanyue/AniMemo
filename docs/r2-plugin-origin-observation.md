# Cloudflare 插件 R2 Origin 观察

`scripts.isolated_guest_validation` 可显式选择
`--r2-origin-transport cloudflare-plugin`，用于操作员授权的单 FRESH_BASE
控制器验证。该模式使用已连接 Cloudflare 插件的 REST 读取能力。

## 信任和权限

可信观察生产者是执行固定收集器的 Codex Cloudflare 插件宿主。OAuth/token
保留在插件内，不交给本机验证进程或 Guest。收集器
`release/r2_plugin_collect.js` 只有固定 Bucket/Prefix/key 范围的 GET 请求，
不创建凭据、调整权限或写入对象。插件连接本身可能有其他权限；本模式只
声称这些观察请求为只读，不声称整个连接的权限仅为 Object Read only。

本模式输出 `animemo.cloudflare-plugin-origin-receipt/v1`，真实 auth_method
为 `CLOUDFLARE_PLUGIN_REST`，endpoint 为 `api.cloudflare.com`。它与 S3
receipt 分开，不能给正式 Candidate Aggregate 或发布授予权力。
默认 S3 模式及其合同继续适用其各自入口。

## 请求与响应

原入口在准确材料、provider、plan 校验后，为 PRESTATE 生成一次请求；在
会话和 Clone 收尾后再为 POSTSTATE 生成新请求。请求绑定 UUID、role、
source SHA/tree、Candidate digest/version、Qualification、plan/session、
固定 Account/Bucket/Prefix/六个 key、收集器摘要和五分钟期限。

固定 E: 卷根下的专用 `r2-plugin-origin-*` 目录使用现有 Windows private ACL/path holds；
`PRESTATE.request.json` 和 `POSTSTATE.request.json` 为只读请求。
已授权执行器读取当前请求，校验准确 checkout 中收集器的摘要，将其作为
Cloudflare `execute` 的 async 函数执行，参数仅包含公开请求和插件自带的
`cloudflare`/`accountId`。不得使用来自响应或外部网页的代码。

执行器保留真实工具输出，并把其中的 response JSON 原样写入同目录临时
文件，再原子改名为对应的 `*.response.json`。文件不含凭据；数据和关联
摘要不是 Cloudflare 签名。canonical 校验依赖本次操作员信任的插件宿主
确实执行了这些读取，不能把任意手写文件当成已验证的网络来源。

消费者拒绝重复字段、未知字段、超限文件、链接/多硬链、错请求、错作用域、
过期或未来观察。一次角色只能尝试一次，POSTSTATE 必须绑定同一 plan，
采用新的 request ID 和执行后的新时间。无响应最多等待五分钟，然后失败。
两次实际观察的原始数据及其 receipt 保留用于审计。

## 空前缀判定

先读取 Bucket 名称与 jurisdiction；然后从准确 Prefix 首轮列举，
`per_page=1000`，没有 delimiter 或 start_after。若存在 cursor/截断信息，
必须继续，最多 64 页，拒绝重复 cursor。任何对象或 common prefix 均失败。

Cloudflare List Objects 的 `result_info` 是可选字段。空响应省略它时，
保留该缺失事实并结束列举；还必须独立 GET 全部六个预期 key。
插件对不存在对象抛出的确切错误
`Cloudflare API error: 10007: The specified key does not exist.`
按 `KEY_NOT_FOUND` 记录，HTTP status 为 null，因为工具未提供该状态。
其他错误、认证或权限失败、非空结果均失败关闭。

PRESTATE 成功后才允许 Clone。POSTSTATE 仍由原入口 finally 路径调用；
超时或失败阻止动态 PASS。原 provider/material/lease、Guest 身份、
SessionSupervisor、单次捕获 ledger 和两个固定 sudo role 都继续生效。

接口依据：[Cloudflare R2 List Objects](https://developers.cloudflare.com/api/resources/r2/subresources/buckets/subresources/objects/methods/list/)
与执行时的插件 OpenAPI 和实际工具响应。这个模式是独立观察协议，修改其
源码后必须重新取得对应 exact-main Qualification，不能消费旧源码资格。
