# Plugins

插件开发规范见 [`docs/plugin-development.md`](../docs/plugin-development.md)。

新插件从 `_template` 复制，目录名称改为插件 slug。`_` 开头的目录是模板或内部工具，不会被 `npm run test:plugins` 当作已安装插件。

```powershell
Copy-Item -Recurse plugins\_template plugins\my-plugin
```

复制后必须替换模板中的以下标识：

```text
blank-plugin
Blank Plugin
空白插件
anime_journal_blank_plugin
com.example.anime-journal.blank
```

插件目前不会被自动发现。完成开发后必须经过审查，并在 Django `INSTALLED_APPS`、Django URL、前端插件 registry 和部署依赖中显式注册。
