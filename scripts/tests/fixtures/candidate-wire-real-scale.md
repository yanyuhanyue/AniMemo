# 非权威完整回执回归输入

`candidate-wire-real-scale.json.xz` 的外层明确为 `NON_AUTHORITATIVE_LOCAL_REGRESSION`。
它保留 Q 34747979837、源码 ce31b79b80e956afa42f70c1c946dab2dae44008 的三份完整 Profile、
两份 Origin 及原计划；不含密码、凭据会话、Guest 文件、用户业务记录或启动器。

原控制器结果 SHA-256 为 `754f05cb90a5fe67e7931cfb6f946ddd8402eb00c1cc3aa7566cffeedb668c35`。
原执行整体 ERROR，未签发 Aggregate。回归中使用真实构造器生成的 Aggregate 属于新的本地
测试上下文，不是历史执行签发结果，也不证明当前提交通过正式 Candidate。

压缩仅用于保存 fixture，受独立 512 KiB fixture 读取上限及 8 MiB XZ 解压内存约束。
实际导出仍执行 384 KiB canonical JSON 和 48 KiB 完整 wire 预算。所有 Profile/Origin
字段保持完整，原观察 ID 和摘要不改写；测试不访问这些字段中描述的远端资源。
原结果没有保存完整 Candidate poststate；回归使用已保存的 prestate 作为测试预期后态，
不据此声称原执行完成了额外远端读取。实际 Origin POSTSTATE 原回执保持原样。
