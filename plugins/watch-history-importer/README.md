# 忆往昔观看记录导入器

Anime Journal 全栈插件，用于上传年度 TXT 观看记录，在正式写入前完成解析预览、Bangumi 匹配和人工确认。

## 导入流程

1. 当前用户上传 1-8 个属于自己的 TXT 文件。
2. 插件只生成只读预览，不会因为启用插件而自动导入。
3. Bangumi 分批解析标题、话数、海报、标签与动画制作公司。
4. 预览支持逐项勾选，以及对当前搜索结果批量导入或排除。
5. 低置信度、话数冲突和无法匹配的条目必须人工确认、主动排除或按插件设置跳过。
6. 确认后把标准化 WatchHistory DTO 交给 AniMemo Core，在单个数据库事务中写入番剧与观看历史。

插件保留每次观看日期与刷次；番剧列表标签只保留最新观看日期和最新刷次，不生成季度日期标签。同一目标账号重复导入时，番剧按 Bangumi 条目/规范化标题合并，观看事件由 Core 按日期、刷次和集数范围去重；源文件名与行号仅用于追溯。

TXT 排版、编码探测、来源标签、标题规范化、候选解析、批次状态和预览/确认工作流始终属于本插件。AniMemo Core 不理解此文档格式；未安装本插件的用户仍可通过 Core UI/API 完整管理观看记录。

## 验证

```powershell
python backend\manage.py test anime_journal_watch_history_importer
npm run test:plugins
npm run build
npm run qa:watch-history:selection
```
