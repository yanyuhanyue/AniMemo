# Candidate 材料根与宿主交接

`release.candidate` 集中定义两个平台的规范材料根：Linux/POSIX 使用
`/var/lib/animemo/prepublication-candidates/v2`，受支持的 Windows 控制器使用固定
`E:/var/lib/animemo/prepublication-candidates/v2`。Windows 选择来自既有受信宿主布局，
不搜索盘符，不依赖当前目录、当前盘符或某个目录是否存在。

`VERIFIED_CANDIDATE_ROOT` 是当前宿主的 `Path`。verifier 写入和 loader 定位均经过
同一个 `_candidate_state_root` 边界，loader 返回的根可直接进入要求 fully qualified
路径的私有材料快照。显式内部/CLI state root 必须本来就是绝对路径；不为调用者补盘符，
也不调用 `resolve()` 掩盖 symlink、junction 或其他 reparse 组件。Windows 拒绝
drive-relative、无盘符 rooted、普通相对、UNC/device、父目录逃逸、ADS、DOS 保留名、
短名及末尾空格/点别名。词法校验后检查已有祖先，缺失尾部只由 verifier 正常创建。

这些检查不替代原有 owner/私有 ACL、文件类型和单链接、摘要、OS 文件身份及 held-source
检查。材料获取仍通过 `acquire_candidate_material_authority`，使用精确已验证的文件清单，
复制到私有根并持有原文件和私有副本；关闭后 authority 失效并释放临时材料。路径显示
一致或目录存在本身不能授予材料 authority。

Linux Guest 的路径明确引用 `POSIX_VERIFIED_CANDIDATE_ROOT`，不继承 Windows 宿主路径。
宿主绝对路径不进入 Candidate Input、Verified identity、Manifest 或 OCI 确定性内容，
修复不改变这些格式版本。旧 Qualification 只能证明原来源源码；不能因新 loader 能读取
旧材料就把它用作新源码的动态资格。

针对回归位于 `release/test_candidate_roots.py`、既有 Candidate verifier/loader identity
测试和 Guest 路径测试。真实 Windows 的 junction 拒绝、私有树双侧持有、ACL/owner 拒绝
与关闭后失效应分别保留实际证据。宿主集成回归使用已验证材料核对默认 verifier 与 loader
一致、同盘/跨盘当前目录无关、真实私有材料获取和清理；其开发 fixture 的原源码/Q 标签
不变。修复后 Qualification 的实际新材料仍须再次取得 canonical 材料 authority。

路径语义参考：[Python 3.12 pathlib](https://docs.python.org/3.12/library/pathlib.html#pathlib.PurePath.is_absolute)。
