# AniMemo 许可证、素材来源与历史标识审计

审计日期：2026-08-13（Asia/Shanghai）

审计工作树：`E:\番剧记录\anime-journal-final-rc`

审计基线：`8727aa97dc092d12e4a4abb15b85ce1f46d1020d`

分支：`rc/final-readiness-closure`

本记录是可复核的仓库范围与兼容性决策，不是法律意见、权利保证或对任何姓名/实体权利人的推断。源码重新许可和素材处置依据用户于 2026-08-13 作出的最终决定；Git 历史仅用于检查贡献连续性、已发布身份和第三方边界。

## 结论摘要

- 用户可见产品身份统一为 **AniMemo / My Anime Memory / 我的动漫记忆库**。
- AniMemo 不再以任何外部网站作为正式品牌、视觉、文案或产品身份来源。
- 根 `LICENSE` 与 `PolyForm-Noncommercial-1.0.0.md` 保持为同一份已核验的 PolyForm Noncommercial 1.0.0 官方文本。校验器在 Windows 工作树中先规范化行尾，再核对官方 blob、SHA-256、大小和逻辑行数；Git 通过 `.gitattributes` 固定两个许可证文件为 LF。
- AniMemo Core、Plugin Template、Watch History Importer 当前源码和本仓库内未发布的 AniMemo Bridge 自有源码声明为 `PolyForm-Noncommercial-1.0.0`。Watch History Importer 已发布的 `0.4.2` 不被重写；当前许可证元数据由 `0.4.3` 承载。
- 第三方依赖、第三方源码、Bangumi API/数据/图片及其他外部媒体保留各自条款，不因 AniMemo 源码许可证被重新许可。
- 旧 `public/assets/avatar.png` 已替换为 AniMemo 原创品牌吉祥物；旧 `poster-01.webp` 已替换为 AniMemo 原创缺省封面。
- `poster-02.webp` 至 `poster-16.webp` 已从仓库删除。演示番剧不再通过静态文件序号绑定封面，而是使用稳定 Bangumi Subject 身份经现有 Provider/可信媒体契约解析，失败时统一回退到原创 `poster-01.webp`。
- Universe Hero 与 Featured Hero 的品牌装饰不再使用真实动画海报；品牌视觉与动漫内容封面已分离。

## 素材与外部内容边界

### AniMemo 自有品牌素材

- `public/assets/avatar.png`：AniMemo 原创品牌吉祥物，可用于默认站点头像、默认用户头像、favicon 和允许的品牌装饰场景。
- `public/assets/posters/poster-01.webp`：AniMemo 原创“番剧封面缺失 / 无封面”图，只作为安全降级 fallback。
- 两个文件均由仓库内 `scripts/generate_animemo_assets.py` 的项目专用图形生成逻辑产生；不得把它们与具体动画作品绑定。

### 动漫作品封面

- 16 条核心演示记录绑定以下 Bangumi Subject：`569161`、`543360`、`558296`、`587454`、`515759`、`604826`、`520842`、`501614`、`506677`、`514353`、`524707`、`512190`、`363957`、`485936`、`364844`、`531159`。
- Featured 的额外演示内容使用 Subject `295017` 和 `240828`。
- `src/lib/demoMedia.js` 复用现有 External Media/Bangumi Provider 路径，校验 Provider、Subject ID、日文标题和可信海报域；网络、身份、标题或媒体校验失败时使用 AniMemo fallback。
- Bangumi 返回的标题、元数据和图片是外部 Provider 内容，不是 AniMemo 自有或 PolyForm 授权素材。

### 已移除的旧素材

- `public/assets/posters/poster-02.webp` 至 `poster-16.webp`：REMOVED。
- 运行时代码中不得存在 `poster-02.webp` 至 `poster-16.webp` 的静态绑定，也不得使用 ``poster-${...}.webp`` 维持番剧与文件序号的映射。
- 测试可以保留一个旧编号路径作为兼容性输入，证明旧本地数据会安全收敛到 `poster-01.webp`；这不是生产资源引用。

## 历史来源标识清理

发布文档、页面文案、metadata、邮件显示名、默认站点名称、演示文案、可信媒体域、CSP、测试夹具及源码中的产品描述已完成独立重写或清理。现行产品代码和发布文档不再保存外部旧站域名、旧图片代理域名、旧站显示名、旧 Demo slug 或直接沿用的首页默认文案明文。

兼容与防回归能力仍然保留：

- `backend/site_config/migrations/0001_initial.py` 是已经发布的不可变 Django 历史迁移，保留其中的历史默认值，不把迁移历史当作品牌展示；新安装和当前模型默认值均已收敛为 AniMemo。
- `backend/site_config/migrations/0005_animemo_identity_defaults.py` 使用既有默认值的 SHA-256 指纹识别待迁移数据；只更新完全匹配旧默认值的字段，不覆盖管理员自定义内容。
- `src/data/universe.js` 使用旧 Demo slug 的 Unicode code-point 序列承接已分享链接，canonical public slug 保持为 `animemo-demo`。
- `scripts/check_license_docs.py` 从十六进制重建禁止词并扫描仓库，防止历史来源域名、身份或文案重新进入产品与发布资料。

普通技术语境中的 `cloneElement`、许可证原文中的通用英文，以及第三方许可证名称不会被全局替换或误删。

## 品牌名与技术标识分类

### BRAND REFERENCES RENAMED

- 用户可见 UI、页面 title/metadata、默认站点名称、邮件产品显示名、README、产品文档、OpenAPI 展示名称、插件说明和纯品牌注释中的 `Anime Journal` / `ANIME JOURNAL` 已收敛为 AniMemo。

### TECHNICAL IDENTIFIERS PRESERVED

- `ANIME_JOURNAL_*` 环境变量和构建/发布兼容键。
- `/data/anime-journal`、Compose project/volume/container identity、production filesystem paths。
- updater、release、backup、restore 的持久化状态与契约标识。
- `anime_journal_*` localStorage/cookie key、`anime-journal:*` event/channel、Python package/import namespace。
- 已发布插件的 `com.anime-journal.*` ID、slug、module/package identity，以及 Integration Protocol/Release manifest compatibility key。

这些标识承载已有状态、包身份或协议兼容性；本轮没有在缺少迁移证明的情况下进行 breaking rename。

### TECHNICAL IDENTIFIERS MIGRATED WITH COMPATIBILITY

- Demo canonical public slug 已从旧值迁移为 `animemo-demo`，旧 URL 通过兼容别名解析。
- SiteSettings 默认品牌值通过 additive migration 更新；只转换精确匹配旧默认值的数据，保留管理员自定义值。
- `BANGUMI_IMAGE_PROXY_BASE_URL` 环境变量名称保留；旧代理默认值和可信域已移除，显式配置能力继续兼容。

### TECHNICAL IDENTIFIERS REVERTED

- 无。审计未发现需要回退的已实施 breaking technical rename。

## 组件重新许可与第三方边界

### AniMemo Core

根许可证适用于许可方有权许可的 AniMemo 自有源码；不覆盖 NOTICE/THIRD_PARTY_NOTICES 中排除的第三方内容和外部媒体。

### Plugin Template

`plugins/_template/manifest.json` 声明 `PolyForm-Noncommercial-1.0.0`。模板使用 React/宿主 SDK 时，相关第三方依赖继续适用自身条款；未来开放插件生态时是否另行采用宽松 SDK/template 许可证不属于本轮。

### Watch History Importer

当前源码与 `0.4.3` manifest 声明 PolyForm。已发布 `0.4.2` 的 package content identity、历史许可证元数据和回滚下限保持不可变。Bangumi、React、Django、Django REST Framework、requests 等边界不随插件自有源码声明改变。

### AniMemo Bridge

本仓库内未发布 Bridge 的自有源码 metadata 声明 PolyForm；AstrBot runtime 与 httpx 保持各自条款。

## PolyForm 原文证据

| 文件 | Git blob | SHA-256 | 字节 | 审计行数 |
|---|---|---|---:|---:|
| `LICENSE` | `5ecc88cfc4b1cff608ed640efe913c9dd97935c3` | `c0ea4a896d2c8c394b29f9427589996db826cd501c512279ff0ed3ef48fabbe5` | 4563 | 74 |
| `PolyForm-Noncommercial-1.0.0.md` | `5ecc88cfc4b1cff608ed640efe913c9dd97935c3` | `c0ea4a896d2c8c394b29f9427589996db826cd501c512279ff0ed3ef48fabbe5` | 4563 | 74 |

核验命令：

```powershell
python scripts/check_license_docs.py 8727aa97dc092d12e4a4abb15b85ce1f46d1020d
```

## 自动化验收范围

`scripts/check_license_docs.py` 现在验证：

- PolyForm 官方内容身份和根许可证一致性。
- README/NOTICE/TRADEMARKS/THIRD_PARTY_NOTICES 的许可与外部媒体边界。
- AniMemo avatar 与 fallback 文件存在且非空。
- `poster-02.webp` 至 `poster-16.webp` 不存在。
- 16 个 Demo Subject ID 完整、稳定且唯一。
- Demo Provider hydration、可信海报 URL 和 fallback 契约存在。
- 生产源码不存在已删除编号海报或模板字符串映射。
- 第三方依赖锁、许可证清单和自有组件声明保持准确。

Final RC 的页面交互、生产构建和浏览器验收必须由对应前端/后端/浏览器测试结果另行证明；本文件不把未运行的真实生产恢复或外部服务演练写成已完成。
