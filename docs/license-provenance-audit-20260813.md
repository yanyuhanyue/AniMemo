# AniMemo 许可证与历史标识审计

审计日期：2026-08-13（Asia/Shanghai）
审计工作树：`E:\番剧记录\anime-journal-license`
审计基线：`8727aa97dc092d12e4a4abb15b85ce1f46d1020d`（与 `origin/main` 一致）
分支：`rc/license-docs`

本记录是可复核的仓库证据和范围决策记录，不是法律意见、权利保证或对任何姓名/实体权利人的推断。源码重新许可的范围依据用户在 2026-08-13 作出的最终决定；Git 历史只用于记录贡献连续性、初始导入和排除第三方来源。

## 结论摘要

- 产品身份统一为 **AniMemo / My Anime Memory / 我的动漫记忆库**。
- 根 `LICENSE` 与 `PolyForm-Noncommercial-1.0.0.md` 使用同一份已核验的 PolyForm Noncommercial 1.0.0 原文；二者应保持字节相同。
- AniMemo Core、`plugins/_template`、`plugins/watch-history-importer` 当前源码范围，以及本仓库中的 `bridges/astrbot_plugin_animemo_bridge` 自有源码声明为 `PolyForm-Noncommercial-1.0.0`。
- `watch-history-importer` 的已发布 `0.4.2` 不可重写；本次许可证元数据通过新版本 `0.4.3` 承载。历史版本身份、包内容和回滚下限不被改写。
- PolyForm 不覆盖 Bangumi API、Bangumi 返回的条目/媒体、外部媒体、Noto Sans SC、Font Awesome、caniuse-lite、GSAP、Psycopg、AstrBot、httpx、React、Django、Django REST Framework、requests 或其他第三方依赖。各自条款继续有效，详见 `THIRD_PARTY_NOTICES`。
- `public/assets/avatar.png`、`public/assets/posters/poster-01.webp` 至 `poster-16.webp`、`src/data/anime.js` 和具体 `src/pages/**` UI/演示数据未在本工作树中修改。

## PolyForm 原文证据

| 文件 | Git blob | SHA-256 | 字节 | 审计行数 |
|---|---|---|---:|---:|
| `LICENSE` | `5ecc88cfc4b1cff608ed640efe913c9dd97935c3` | `c0ea4a896d2c8c394b29f9427589996db826cd501c512279ff0ed3ef48fabbe5` | 4563 | 74 |
| `PolyForm-Noncommercial-1.0.0.md` | `5ecc88cfc4b1cff608ed640efe913c9dd97935c3` | `c0ea4a896d2c8c394b29f9427589996db826cd501c512279ff0ed3ef48fabbe5` | 4563 | 74 |

核验命令：

```powershell
python scripts/check_license_docs.py 8727aa97dc092d12e4a4abb15b85ce1f46d1020d
```

校验器同时验证官方副本的 blob、SHA-256、大小、逻辑行数、LF 字节和根 `LICENSE` 的字节相等性。

原文来源证据：PolyForm Project Noncommercial 1.0.0 官方文本，已核验官方分支提交 `76a278c402bc43b8d2b561da140b0f3e17263015`；本仓库不对文本自行改写。

## 组件范围与 Git 证据

### AniMemo Core

范围：除下方明确排除的第三方、媒体、素材和协调者路径外的核心仓库源码。
当前 HEAD 可达历史：114 个提交，单一根提交 `81d18f477b969cdd37cffd66585e0724a16ab83e`。
贡献身份快照：可达提交中源码贡献显示名为 `烟雨寒月`，使用两种邮箱身份：

- `15238461301@163.com`
- `111261350+yanyuhanyue@users.noreply.github.com`

此处不把显示名或邮箱解释为法律权利人姓名。初始根提交一次导入约 42,725 行，故 Git 不能证明导入前作品的权利链；这是明确保留的证据限制。

### `plugins/_template`

- 8 个已跟踪文件。
- 创建于根提交 `81d18f477b969cdd37cffd66585e0724a16ab83e`。
- 后续可达历史仍来自上述两种邮箱身份；未发现 `.gitmodules`、嵌入式第三方许可证头或 fork/derived-from 标记。
- Manifest 许可证改为 `PolyForm-Noncommercial-1.0.0`；React、宿主运行时和复制模板时加入的依赖/素材仍不受该声明覆盖。

### `plugins/watch-history-importer`

- 16 个已跟踪文件。
- 创建于根提交 `81d18f477b969cdd37cffd66585e0724a16ab83e`；后续演进包含 `ed32fa5294da65f5b2542e0fba826ca05af70dfa`、`3a5bd39edb1f06ab21a9df664dd93d6f8f2e96fd`、`a7c6eb3a73b5b3e26d58425567e1aa6dd3d33905`、`db85e08a77de5e6bf3a5719d96c9396ee44dc04f4` 等提交。
- 未发现 `.gitmodules`、嵌入式第三方许可证头或 fork/derived-from 标记。
- `manifest.json`、`backend/plugin.py`、`frontend/index.jsx`、生成的 `frontend/plugin.js` 和 `package-index.json` 更新到 `0.4.3`，只为承载许可证元数据变更；`0.4.2` 的历史不可变身份保留。原有 `author.name` 元数据保留，不被解释为或改写为法律权利人姓名。
- 该插件调用 Bangumi API，并依赖 React、Django、Django REST Framework、requests；这些边界不随插件自有源码声明改变。

### `bridges/astrbot_plugin_animemo_bridge`

- 30 个已跟踪文件。
- Bridge 从 `8bfa22c365ec09227df94b9c6151ce7cbae44296` 开始形成；后续历史包括 `f2aaed4672ee14eb6a8fcfa26a69e79ce105255f`、`a2820fd58c5908915bb947250adb9b9ee6d20c97`、`7fb52021cadbbd206c7e6215c4e1b5003ae073cb` 等。
- 未发现 `.gitmodules`、嵌入式第三方许可证头或 fork/derived-from 标记；未发现不可变市场发布门禁，README 仍记载 Marketplace publication = NOT YET PUBLISHED。
- `metadata.yaml` 改为 `PolyForm-Noncommercial-1.0.0`；AstrBot 运行时和 httpx 仍适用各自条款。

## 历史标识清理

对仓库内发布/许可证/组件说明进行了只读搜索，并清理了 README、NOTICE、TRADEMARKS、THIRD_PARTY_NOTICES 及组件说明中的外部站点来源、复刻或 clone 定位。保留在兼容技术标识中的 `anime-journal` 字样不表示产品来源、关联或权利归属。

普通技术语境中的 `cloneElement`、PolyForm 条款中的 `based on`，以及测试夹具中的 MIT 示例没有被误当作历史来源定位；第三方许可证和锁文件没有被修改。

## 明确排除与阻塞

### 可独立完成并已完成

- 根许可证文件与官方 PolyForm 文本副本。
- README License 入口与 AniMemo 产品身份重写。
- NOTICE、TRADEMARKS、THIRD_PARTY_NOTICES 边界和依赖清单。
- 三个自有组件的当前许可证元数据；官方 importer 以 `0.4.3` 维持不可变版本纪律。
- 许可证专用校验器、单元测试和 importer 包索引准确性检查。

### 仍需协调者/用户决定

- `public/assets/avatar.png` 与 `public/assets/posters/poster-01.webp` 至 `poster-16.webp` 的授权、替换或移除决定；本工作树未编辑二进制素材。
- `src/data/anime.js` 中具体标题、描述、评分、图片引用和外部媒体链接的权利/来源处理；本工作树未编辑该文件。
- 具体 `src/pages/**` UI/演示数据中的媒体或历史文案若需清理，由协调者按页面冲突计划处理；本工作树未编辑页面。
- 初始大规模导入之前的权利链无法由 Git 单独证明；如需发行级权利证明，必须由用户/权利人提供外部证据，不能由本审计推断。

这些阻塞不应被 README 或 PolyForm 文本解释为已获授权。

## RC 计数（本工作树范围）

分级定义：RC0 = Final RC blocker；RC1 = must fix before Final RC；RC2 = safe to defer；RC3 = cosmetic/optional。

- 许可证文本/根入口/第三方边界/自有组件声明：RC0 open `0`；RC1 open `0`。
- 协调者负责的素材和具体演示内容：RC0 open `2` 类别（17 张图片；`src/data/anime.js` 与具体页面演示内容的来源/清理），RC1 open `0` 在本工作树不另行计数；它们仍是发行前阻塞。
- 权利链外部证据：RC1 open `1`（初始大规模导入前的权利链无法由 Git 证明），需用户决定是否提供外部证据。

本计数不把第三方依赖的正常 MIT/BSD/Apache/LGPL/OFL/CC 条款误计为 AniMemo 自有源码问题，也不把未修改的协调者路径伪装成已解决。

## 已执行验证

以下命令均在 2026-08-13（Asia/Shanghai）于本工作树执行：

- `python scripts/check_license_docs.py 8727aa97dc092d12e4a4abb15b85ce1f46d1020d`：PASS。
- `python -m unittest scripts.tests.test_license_docs -v`：7/7 PASS。
- `npm run test:plugins`：PASS。
- `python scripts/pluginctl.py validate watch-history-importer`：PASS。
- `python scripts/check_official_plugin_immutability.py --base 8727aa97dc092d12e4a4abb15b85ce1f46d1020d --head 8727aa97dc092d12e4a4abb15b85ce1f46d1020d --head-root .`：PASS；历史 `0.4.2` 与当前 `0.4.3` 身份分离。
- `python scripts/validate-astrbot-bridge.py`：PASS。
- `python -m unittest discover -s bridges/astrbot_plugin_animemo_bridge/tests -p "test_*.py" -v`：50/50 PASS。
- `DJANGO_SECRET_KEY=<临时测试值> DEBUG=true python manage.py test plugin_host.tests.test_official_sync --verbosity 1`：9 项通过，1 项按 Windows 压缩差异规则跳过。
- `git diff --check`：PASS；素材、`src/data/**`、`src/pages/**` 和锁文件相对基线未变化。
