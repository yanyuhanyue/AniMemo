# 忆往昔观看记录导入器

AniMemo 全栈插件，用于上传年度 TXT 观看记录，在正式写入前完成解析预览、Bangumi 匹配和人工确认。

## 许可证与外部边界

从 `0.4.3` 起，本插件中由许可方有权许可的 AniMemo 自有源码采用仓库根目录 `LICENSE` 所载的 PolyForm Noncommercial License 1.0.0，Manifest 标识为 `PolyForm-Noncommercial-1.0.0`。已发布的 `0.4.2` 是不可变历史包身份，本次变更不改写该版本。

Bangumi API、返回的条目数据与媒体不属于 AniMemo 源码许可证范围；React、Django、Django REST Framework、requests 及宿主 SDK 等第三方软件仍适用各自条款。此处只声明本插件自有源码的许可边界，不提供第三方内容授权或法律保证。

## 导入流程

1. 当前用户上传 1-8 个属于自己的 TXT 文件；单文件不超过 2 MiB，总大小默认不超过 4 MiB。
2. 插件只生成只读预览，不会因为启用插件而自动导入。
3. Bangumi 分批解析标题、话数、海报、标签与动画制作公司。
4. 预览支持逐项勾选，以及对当前搜索结果批量导入或排除。
5. 低置信度、话数冲突和无法匹配的条目必须人工确认、主动排除或按插件设置跳过。
6. 确认后把标准化 WatchHistory DTO 交给 AniMemo Core，在单个数据库事务中写入番剧与观看历史。

插件保留每次观看日期与刷次；番剧列表标签只保留最新观看日期和最新刷次，不生成季度日期标签。同一目标账号重复导入时，番剧按 Bangumi 条目/规范化标题合并，观看事件由 Core 按日期、刷次和集数范围去重；源文件名与行号仅用于追溯。

TXT 排版、编码探测、来源标签、标题规范化、候选解析、批次状态和预览/确认工作流始终属于本插件。每个用户默认最多保留 4 个批次，单批持久化上限为 40 MiB，7 天后由标准维护入口清理。AniMemo Core 不理解此文档格式；未安装本插件的用户仍可通过 Core UI/API 完整管理观看记录。

## 验证

```powershell
python backend\manage.py test anime_journal_watch_history_importer
npm run test:plugins
npm run build
npm run qa:watch-history:selection
```
