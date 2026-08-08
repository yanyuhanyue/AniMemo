# 观看记录导入预览

> 本文命令仅适用于完整源码仓库。Core-only 发布包会有意排除 `qa/` 和浏览器测试产物，因此不要在 core-only 解压目录中执行这些命令。

`qa/watch_history_import.py` 是四份 `忆往昔/*.txt` 的只读解析器。它只生成预览 JSON，不会创建或修改 Django 番剧记录，也不会连接 AstrBot。

```bash
npm run qa:watch-history
```

输出文件：`qa/watch-history-preview.json`

## 当前规则

- 只把记录区中的明确观看条目标记为 `看过`；`断更`、`未看完`、`暂弃`、`暂无兴趣` 等条目进入剔除逻辑。
- `二刷`、`三刷`、`X刷` 全部保留；未写刷次的记录按 `首刷` 处理。
- 同一规范化标题、同一刷次只保留最新观看日期；详情数据保留来源文件、行号、日期范围、集数区间和备注。
- 外部列表可消费 `watch_date_label` 作为最新观看日期；详情页应消费同一番剧的全部观看记录。
- `anime_groups` 是导入器使用的暂定分组结果：外部卡片使用 `latest_watch_date_label`，详情页使用 `watch_history`；完成 Bangumi subject 匹配后应以 subject ID 重新合并同名异写记录。
- 写有 `共 N 集` 或明确总集数的条目可以通过 `--bangumi-matches` 注入 Bangumi 总话数进行校验；不一致的条目会进入 `excluded`。
- `无职转生` 的 `二刷无职第20-23集`、`2022年1月2日` 是例外，始终保留，不因话数校验剔除。
- 日期缺失或“忘了啥时候看的”的记录直接进入 `excluded`。
- `终物语 上` 的 12 集旧记录是用户确认的显式例外，即使 Bangumi 当前总话数为 13 也保留。

Bangumi 匹配文件格式是“规范化标题 -> 总话数”的 JSON 对象，例如：

```json
{
  "化物语": 15,
  "伪物语": 11
}
```

```bash
python qa/watch_history_import.py --bangumi-matches qa/bangumi-matches.json
```

仓库自带只读 Bangumi 校验命令，会生成匹配报告和总话数映射：

```bash
npm run qa:watch-history:bangumi
python qa/watch_history_import.py \
  --bangumi-matches qa/watch-history-bangumi-episodes.json \
  --output qa/watch-history-resolved-preview.json
```

这一步仍然只输出预览；人工核对 `review` 与 `excluded` 后，才允许接入正式导入器。标题匹配应优先使用 Bangumi subject ID，不应把标题字符串当作数据库唯一键。
