# Blank Plugin

这是 Anime Journal Plugin SDK v2 的空白 Runtime Plugin 模板。先复制整个目录，再统一替换模板标识，禁止直接在 `_template` 中开发正式插件。

## 后端接入

`backend/plugin.py` 必须导出统一入口：

```python
def create_plugin(host):
    return Plugin(host)
```

宿主通过固定路径分发 API：

```text
/api/plugins/blank-plugin/<path>
```

Runtime Plugin 不注册 Django App、URLConf、model 或 migration，也不需要重启 Django。后端接口必须通过 `host.api.get/post/put/patch/delete(path, handler=..., access="user"|"staff", permission=...)` 注册；`access=user` 要求当前用户已安装并启用插件，`access=staff` 的 `permission` 必须存在于 Manifest。插件通过 `host.system_settings`、`host.user_settings(user)`、`host.storage(...)`、`host.register_hook(...)` 和声明了外部网络权限后的 `host.request_json(...)` 使用宿主能力。

需要 Django model、migration、系统依赖或独立进程的 Extended Plugin 当前不受支持，安装器会拒绝相关 manifest 字段。

## 前端接入

`frontend/index.jsx` 导出 `createPlugin(host)`。宿主只加载后台返回的已启用、健康且当前用户有权限访问的版本；每个导航项通过 `area` 声明归属区域。

不要让插件自行创建 React root、BrowserRouter、Axios 实例或 token 存储。

## 验证

构建、验证并打包：

```powershell
python scripts\pluginctl.py build blank-plugin
python scripts\pluginctl.py validate blank-plugin
python scripts\pluginctl.py pack blank-plugin
```

用户从市场安装并启用插件后，模板会请求：

```text
GET /api/plugins/blank-plugin/status/
```

权限由 Manifest 的 `frontend.exposure` 和 handler 自己声明的 `permission` 在 metadata、asset、backend dispatch 三处统一执行。拥有某个插件权限不会自动获得该插件的其他接口；未授权请求由宿主拒绝。

## Integration Protocol v1

模板还展示了 provider-neutral 的 `host.integrations` 门面：Manifest 中的 `integrations.actions/events` 声明必须与 `integration.actions/events` 扩展一致，插件只注册本地 kebab-case 名称，公开动作由 Host 自动加上插件 slug 命名空间。事件路由只接受 AniMemo 用户，由 Host 根据已启用绑定投递，默认是私聊。

完成接入验证后，可以删除状态卡片，但保留命名空间、权限和清理约定。
