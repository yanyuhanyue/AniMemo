# AniMemo AstrBot Runtime Compatibility Audit

## Baseline verdict before hardening

**NO-GO on audited `origin/main@1ad622c2` before this hardening.** The source
audit itself changed no Bridge, workflow, deployment, or production file; the
remediation described in the final section was implemented afterward on the
pre-production feature branch.

The diagnostics Web APIs are authenticated by AstrBot, and the active-message
signature is correct. The gate still has four material gaps:

1. The Bridge does not use AstrBot's plugin data-directory helper and can fall
   back to the plugin **code** directory (`data/plugins/...`) for mutable state.
2. `astrbot.api.message` does not exist in either audited baseline. The Bridge's
   fallback eventually imports the real `MessageChain`, but the primary import
   is not an AstrBot API.
3. `metadata.yaml` does not declare `astrbot_version`, although both baselines
   support and enforce it.
4. CI never installs or checks out AstrBot. Its broad `ImportError` fallback and
   fake `Context` allow all repository-side tests to pass without importing the
   real runtime.

The administrator check is functionally equivalent on a real event because
AstrBot's `is_admin()` is exactly `role == "admin"`, but it should use the
official method directly instead of probing invented method names.

## Audited baselines

Audit snapshot: **2026-08-09 (Asia/Shanghai)**.

| Baseline | Immutable revision | Evidence |
| --- | --- | --- |
| Release `v4.27.2` | `ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf` | [official release](https://github.com/AstrBotDevs/AstrBot/releases/tag/v4.27.2), [commit](https://github.com/AstrBotDevs/AstrBot/commit/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf) |
| `master` snapshot | `30e20318cbaaa2e1ba57f3e0eee265d9ee98115c` | [official commit](https://github.com/AstrBotDevs/AstrBot/commit/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c) |

The public plugin API definitions and workflow/metadata contracts cited below
are unchanged between those two revisions. Some containing files have unrelated
changes elsewhere (notably `dashboard/api/auth.py`); its cited scope-validation
logic is unchanged, with a two-line offset on `master`. Source links use both
immutable revisions where the compatibility claim matters. The
[official comparison](https://github.com/AstrBotDevs/AstrBot/compare/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf...30e20318cbaaa2e1ba57f3e0eee265d9ee98115c)
shows the full intervening change set.

## 1. Persistent plugin data directory

### Official plugin-facing helper

```python
from astrbot.api.star import StarTools

data_dir = StarTools.get_data_dir("astrbot_plugin_animemo_bridge")
```

Exact signature:

```python
@classmethod
def get_data_dir(cls, plugin_name: str | None = None) -> pathlib.Path
```

The helper:

- is publicly re-exported as `astrbot.api.star.StarTools`;
- uses the caller plugin's registered metadata name when `plugin_name` is
  omitted;
- creates `<AstrBot data>/plugin_data/<plugin_name>`;
- returns an absolute, resolved `Path`;
- raises rather than silently choosing another directory when caller metadata,
  the name, permissions, or directory creation fail.

Source evidence:

- `v4.27.2`: [`astrbot.api.star` export](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/api/star/__init__.py#L1-L7), [`StarTools.get_data_dir`](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/core/star/star_tools.py#L206-L260)
- `master@30e20318`: [`astrbot.api.star` export](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/api/star/__init__.py#L1-L7), [`StarTools.get_data_dir`](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/core/star/star_tools.py#L206-L260)

AstrBot also has a lower-level root helper:

```python
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

plugin_data_root: str = get_astrbot_plugin_data_path()
```

Its exact signature is `get_astrbot_plugin_data_path() -> str`; it returns the
absolute `<AstrBot data>/plugin_data` root, not a per-plugin directory, and does
not create the plugin subdirectory. The official Plugin Pages guide uses this
lower-level helper and then appends `request.plugin_name` and calls `mkdir()`.
For ordinary plugin state, `StarTools.get_data_dir()` is the deeper and safer
plugin-facing API.

Source evidence:

- `v4.27.2`: [`astrbot_path.py`](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/core/utils/astrbot_path.py#L29-L62)
- `master@30e20318`: [`astrbot_path.py`](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/core/utils/astrbot_path.py#L29-L62)
- Current official documentation: [Plugin Pages upload example](https://docs.astrbot.app/en/dev/star/guides/plugin-pages.html#request-object)

### AniMemo finding

`bridges/astrbot_plugin_animemo_bridge/main.py:109` probes
`Context.get_plugin_data_dir`, `Context.get_data_dir`, `plugin_data_dir`, and
`data_dir`. None is present on the audited `Context`. Its final fallback is
`data/plugins/astrbot_plugin_animemo_bridge`, but `data/plugins` is AstrBot's
plugin installation/code directory; persistent data belongs under
`data/plugin_data`.

**Required gate fix:** call
`StarTools.get_data_dir("astrbot_plugin_animemo_bridge")` and remove the
code-directory fallback. Prefer a hard startup failure to writing mutable route
and cursor state into an unknown directory.

## 2. Public API paths and exact signatures

All signatures below are identical in `v4.27.2@ad4fbfa9` and
`master@30e20318`.

### `Star`, `Context`, and `register`

```python
from astrbot.api.star import Context, Star, StarTools, register
```

```python
class Star(CommandParserMixin, PluginKVStoreMixin):
    def __init__(self, context: Context, config: dict | None = None) -> None
```

```python
class Context:
    def __init__(
        self,
        event_queue: Queue,
        config: AstrBotConfig,
        db: BaseDatabase,
        provider_manager: ProviderManager,
        platform_manager: PlatformManagerProtocol,
        conversation_manager: ConversationManager,
        message_history_manager: PlatformMessageHistoryManager,
        persona_manager: PersonaManager,
        astrbot_config_mgr: AstrBotConfigManager,
        knowledge_base_manager: KnowledgeBaseManager,
        cron_manager: CronJobManager,
        subagent_orchestrator: SubAgentOrchestrator | None = None,
    ) -> None
```

Plugins receive `Context` from the runtime; they do not construct it. The public
`register` name is an alias for the following decorator:

```python
def register_star(
    name: str,
    author: str,
    desc: str,
    version: str,
    repo: str | None = None,
)
```

`register` still works in both baselines, but its own source marks it deprecated:
since after `v3.5.19`, a `Star` subclass is auto-discovered and metadata should
come from `metadata.yaml`. Keeping the decorator is compatible today, but it is
not a future-facing dependency.

Source evidence:

- `v4.27.2`: [public exports](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/api/star/__init__.py#L1-L7), [`Star`](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/core/star/base.py#L19-L52), [`Context`](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/core/star/context.py#L123-L169), [`register_star`](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/core/star/register/star.py#L8-L66)
- `master@30e20318`: [public exports](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/api/star/__init__.py#L1-L7), [`Star`](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/core/star/base.py#L19-L52), [`Context`](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/core/star/context.py#L123-L169), [`register_star`](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/core/star/register/star.py#L8-L66)
- Current official documentation: [minimal plugin](https://docs.astrbot.app/en/dev/star/guides/simple.html)

### `filter.command`

```python
from astrbot.api.event import AstrMessageEvent, filter

@filter.command("animemo")
async def animemo(self, event: AstrMessageEvent): ...
```

`filter.command` is re-exported from `astrbot.api.event.filter` as an alias for:

```python
def register_command(
    command_name: str | None = None,
    sub_command: str | None = None,
    alias: set | None = None,
    **kwargs,
)
```

Source evidence:

- `v4.27.2`: [filter export](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/api/event/filter/__init__.py#L10-L18), [`register_command`](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/core/star/register/star_handler.py#L75-L126)
- `master@30e20318`: [filter export](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/api/event/filter/__init__.py#L10-L18), [`register_command`](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/core/star/register/star_handler.py#L75-L126)
- Current official documentation: [message-event command guide](https://docs.astrbot.app/dev/star/guides/listen-message-event.html#%E6%8C%87%E4%BB%A4)

### `MessageChain`

```python
from astrbot.api.event import MessageChain
```

`MessageChain` is a dataclass re-export. Its exact declared fields (and therefore
its generated constructor parameters) are:

```python
@dataclass
class MessageChain:
    chain: list[BaseMessageComponent] = field(default_factory=list)
    use_t2i_: bool | None = None
    use_markdown_: bool | None = None
    type: str | None = None

def message(self, message: str): ...  # appends Plain(message), returns self
```

There is **no** `astrbot/api/message.py` or `astrbot/api/message/__init__.py` in
either audited tree. Therefore `from astrbot.api.message import MessageChain` is
not valid at either baseline. `astrbot.api.event.MessageChain` is the source- and
documentation-backed path.

Source evidence:

- `v4.27.2`: [public re-export](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/api/event/__init__.py#L1-L16), [implementation](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/core/message/message_event_result.py#L17-L58), [complete `astrbot/api` tree](https://github.com/AstrBotDevs/AstrBot/tree/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/api)
- `master@30e20318`: [public re-export](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/api/event/__init__.py#L1-L16), [implementation](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/core/message/message_event_result.py#L17-L58), [complete `astrbot/api` tree](https://github.com/AstrBotDevs/AstrBot/tree/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/api)
- Current official documentation: [active-message example](https://docs.astrbot.app/en/dev/star/guides/send-message.html#active-messages)

**AniMemo finding:** both `main.py` and `animemo_bridge/events.py` try the invalid
path first, catch it, and then import the correct path. Runtime behavior can
succeed, but the gate should require the official path directly so a different
import failure cannot be mistaken for compatibility.

### `Context.send_message`

```python
async def send_message(
    self,
    session: str | MessageSesion,
    message_chain: MessageChain,
) -> bool
```

For a string, AstrBot parses the UMO into `MessageSesion`, locates the platform,
sends, and returns `True`; it returns `False` when no platform matches and raises
`ValueError` for an invalid session string. `qq_official` is documented in the
method as unsupported.

Source evidence:

- `v4.27.2`: [`Context.send_message`](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/core/star/context.py#L508-L571)
- `master@30e20318`: [`Context.send_message`](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/core/star/context.py#L508-L571)
- Current official documentation: [active messages and stored UMO](https://docs.astrbot.app/en/dev/star/guides/send-message.html#active-messages)

AniMemo's call shape, `await context.send_message(umo, message_chain)`, is
correct. It should use the official `MessageChain` import and check/log the
boolean result rather than treating a completed await as guaranteed delivery.

### `Context.register_web_api`

```python
WebApiHandler = Callable[..., Awaitable[Any]]

def register_web_api(
    self,
    route: str,
    view_handler: WebApiHandler,
    methods: list[str],
    desc: str,
) -> None
```

Registration replaces an existing entry only when both route and `methods`
match; otherwise it appends to the shared registry. AniMemo's four-argument call
shape and plugin-name-prefixed routes are correct.

Source evidence:

- `v4.27.2`: [handler type and method](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/core/star/context.py#L53-L54), [`register_web_api`](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/core/star/context.py#L599-L621)
- `master@30e20318`: [handler type and method](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/core/star/context.py#L53-L54), [`register_web_api`](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/core/star/context.py#L599-L621)
- Current official documentation: [Plugin Pages route registration](https://docs.astrbot.app/en/dev/star/guides/plugin-pages.html#route-registration)

## 3. Is `register_web_api` administrator-authenticated?

**It is authenticated and not public, but modern routes are not restricted to
an interactive WebUI administrator session only.**

The modern dispatcher exposes registered handlers at
`/api/v1/plugins/extensions/{plugin_path}`. Every GET/POST/PUT/PATCH/DELETE route
depends on `ScopeDependency("plugin")`. `require_scope()` accepts either:

- a valid Dashboard JWT, which receives `scopes=["*"]`; or
- a valid AstrBot API key that has `plugin`, `*`, or an including scope.

The legacy `/api/plug/{plugin_path}` dispatcher requires a valid Dashboard user
JWT. In both cases the authenticated username is bound to the plugin request.

Therefore:

- AniMemo diagnostics are **not anonymously callable** through AstrBot's normal
  dispatch routes;
- they are available to a Dashboard administrator session;
- they are also deliberately available to a scoped automation/API-key
  principal with `plugin` permission. Calling this “admin-only” without that
  qualification would be inaccurate.

Source evidence:

- `v4.27.2`: [modern extension routes](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/dashboard/api/plugins.py#L379-L421), [scope dependency and credential validation](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/dashboard/api/auth.py#L40-L63), [`require_scope`](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/dashboard/api/auth.py#L130-L193), [legacy route](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/dashboard/api/plugins.py#L1506-L1512)
- `master@30e20318`: [modern extension routes](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/dashboard/api/plugins.py#L379-L421), [scope dependency and credential validation](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/dashboard/api/auth.py#L40-L63), [`require_scope`](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/dashboard/api/auth.py#L132-L195), [legacy route](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/dashboard/api/plugins.py#L1506-L1512)
- Current official documentation: [OpenAPI key and scope model](https://docs.astrbot.app/dev/openapi.html#scope-%E6%9D%83%E9%99%90%E8%AF%B4%E6%98%8E), [Plugin Page iframe/bridge boundary](https://docs.astrbot.app/en/dev/star/guides/plugin-pages.html#bridge-api)

Security consequence for AniMemo: relying on AstrBot's dispatcher protection is
valid. The state-changing `restart` and `routes/clear` handlers must still keep
CSRF-safe methods, validate input, avoid secrets in output, and assume a
`plugin`-scoped API key can call them. AniMemo already uses POST for mutations
and returns masked routes.

## 4. Sender administrator API

The official event API is:

```python
def AstrMessageEvent.is_admin(self) -> bool:
    return self.role == "admin"
```

For a command that must be rejected before its body runs, AstrBot also supports
the official permission filter:

```python
@filter.permission_type(filter.PermissionType.ADMIN)
@filter.command("...")
async def command(self, event: AstrMessageEvent): ...
```

Source evidence:

- `v4.27.2`: [`AstrMessageEvent.is_admin`](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/core/platform/astr_message_event.py#L255-L265), [permission exports](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/api/event/filter/__init__.py#L1-L18)
- `master@30e20318`: [`AstrMessageEvent.is_admin`](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/core/platform/astr_message_event.py#L255-L265), [permission exports](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/api/event/filter/__init__.py#L1-L18)
- Current official documentation: [administrator command filter](https://docs.astrbot.app/dev/star/guides/listen-message-event.html#%E7%AE%A1%E7%90%86%E5%91%98%E6%8C%87%E4%BB%A4), [event `role` property](https://docs.astrbot.app/dev/star/resources/astr_message_event.html#%E5%B1%9E%E6%80%A7)

AniMemo's `_developer_allowed()` first compares `event.role`, so on a real
AstrBot event its result is equivalent. The probed names `is_sender_admin` and
`sender_is_admin` are not present in either baseline. The exact implementation
should call `event.is_admin()`; keep a fail-closed test double only in unit-test
code, not as runtime API discovery.

## 5. Minimum AstrBot version metadata

**Supported and enforced field:**

```yaml
astrbot_version: ">=4.27.2"
```

The exact key is `astrbot_version`. Its value is a PEP 440 specifier string, not
`min_astrbot_version`, `minimum_version`, or a value with a `v` prefix. A single
minimum is `>=4.27.2`; a bounded range is also valid.

The loader stores the field on `StarMetadata`, parses it with
`packaging.specifiers.SpecifierSet`, compares it with the running `VERSION`, and
raises `PluginVersionUnsupportedError` before plugin instantiation when it does
not match (unless the operator explicitly uses the ignore-version-check path).

Source evidence:

- `v4.27.2`: [metadata model](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/core/star/star.py#L61-L75), [YAML loading](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/core/star/star_manager.py#L486-L557), [PEP 440 validation](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/core/star/star_manager.py#L665-L697), [load-time enforcement](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/astrbot/core/star/star_manager.py#L1172-L1205)
- `master@30e20318`: [metadata model](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/core/star/star.py#L61-L75), [YAML loading](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/core/star/star_manager.py#L486-L557), [PEP 440 validation](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/core/star/star_manager.py#L665-L697), [load-time enforcement](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/astrbot/core/star/star_manager.py#L1172-L1205)
- Current official documentation: [declare an AstrBot version range](https://docs.astrbot.app/dev/star/plugin-new.html#%E5%A3%B0%E6%98%8E-astrbot-%E7%89%88%E6%9C%AC%E8%8C%83%E5%9B%B4-optional), [publishing metadata example](https://docs.astrbot.app/en/dev/star/plugin-publish.html)

**AniMemo finding:** `metadata.yaml` has no `astrbot_version`, and the repository
validator does not require it. For this audited contract, the minimum should be
`astrbot_version: ">=4.27.2"` and the packaging validator should reject an
absent or weaker value.

## 6. Real AstrBot runtime smoke in CI

### Current gap

`.github/workflows/ci.yml:140-153` installs only
`bridges/astrbot_plugin_animemo_bridge/requirements.txt` (`httpx>=0.27,<1`), then
runs standard-library tests and static packaging. It never imports a checked-out
AstrBot runtime. `main.py` catches one broad `ImportError` around all AstrBot
imports and defines fake `Star`, `Context`, `filter`, and `register`, so this job
can be green even when every real AstrBot import is broken.

### Reproducible checkout strategy

Use an ephemeral two-revision matrix. Do not use a moving `master` ref inside a
release gate; pin the audited master snapshot and update it deliberately:

```yaml
strategy:
  matrix:
    astrbot_ref:
      - v4.27.2
      - 30e20318cbaaa2e1ba57f3e0eee265d9ee98115c

steps:
  - uses: actions/checkout@v4
  - uses: actions/checkout@v4
    with:
      repository: AstrBotDevs/AstrBot
      ref: ${{ matrix.astrbot_ref }}
      path: .astrbot-runtime
  - uses: actions/setup-python@v5
    with:
      python-version: "3.12"
  - run: python -m pip install uv
  - run: uv pip install --system -r .astrbot-runtime/requirements.txt
  - run: uv pip install --system -r bridges/astrbot_plugin_animemo_bridge/requirements.txt
  - name: Import against the real runtime
    env:
      PYTHONPATH: ${{ github.workspace }}/.astrbot-runtime
      ASTRBOT_ROOT: ${{ runner.temp }}/animemo-astrbot-smoke
    run: python scripts/smoke-astrbot-bridge-runtime.py
```

This mirrors AstrBot's own supported smoke strategy: official source checkout,
official `requirements.txt`, then `scripts/smoke_startup_check.py`. AstrBot's
coverage workflow demonstrates the alternative installable strategy
`pip install --editable .`. For AniMemo, either is valid:

- **Checkout + `PYTHONPATH`**: fastest and avoids building a wheel; install the
  official requirements file exactly.
- **Editable install**: `pip install --editable .astrbot-runtime`; this consumes
  AstrBot's declared project dependencies and gives normal package metadata.

Do not use `pip install AstrBot` without an immutable VCS/ref provenance check,
and do not fetch moving `master` during a final gate.

Source evidence:

- `v4.27.2`: [official smoke workflow](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/.github/workflows/smoke_test.yml#L16-L58), [editable-install coverage workflow](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/.github/workflows/coverage_test.yml#L20-L40), [`pyproject.toml`](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/pyproject.toml#L1-L76), [`requirements.txt`](https://github.com/AstrBotDevs/AstrBot/blob/ad4fbfa90ca0c4ac2b30b3250e34dbf8fe7babbf/requirements.txt)
- `master@30e20318`: [official smoke workflow](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/.github/workflows/smoke_test.yml#L16-L58), [editable-install coverage workflow](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/.github/workflows/coverage_test.yml#L20-L40), [`pyproject.toml`](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/pyproject.toml#L1-L76), [`requirements.txt`](https://github.com/AstrBotDevs/AstrBot/blob/30e20318cbaaa2e1ba57f3e0eee265d9ee98115c/requirements.txt)
- Current official documentation: [official local plugin checkout layout](https://docs.astrbot.app/dev/star/plugin-new.html#%E5%85%8B%E9%9A%86%E9%A1%B9%E7%9B%AE%E5%88%B0%E6%9C%AC%E5%9C%B0)

### Minimum dependency statement

AstrBot declares Python `>=3.12` and one flat runtime dependency set; it does not
publish a supported `core`, `plugin-sdk`, or import-only extra. Consequently the
smallest **supported** dependency contract for a real runtime gate is:

1. Python 3.12 or newer (use 3.12 for the audited floor).
2. AstrBot's own `requirements.txt` at the same immutable revision, or an
   editable install of that revision which resolves the same declared project
   dependencies.
3. The Bridge's `requirements.txt`. Its only top-level dependency is `httpx`,
   already compatible with AstrBot's declared `httpx[socks]>=0.28.1`.
4. No third-party test runner is required for a plain import/startup smoke;
   `unittest` is in the standard library. Add `pytest` only if the smoke script
   is written as a pytest test.

A hand-maintained subset of AstrBot transitive imports would be smaller but is
not an official compatibility contract and can silently stop representing the
runtime as upstream imports evolve.

### Required smoke assertions

The new smoke must fail if AniMemo falls back to its repository stubs. At
minimum it should assert:

```python
from astrbot.api.event import MessageChain
from astrbot.api.star import Context, Star, StarTools

import main as bridge_main
from main import AniMemoBridge

assert bridge_main.Star is Star
assert bridge_main.Context is Context
assert issubclass(AniMemoBridge, Star)
assert MessageChain().message("probe").chain
assert StarTools.get_data_dir("astrbot_plugin_animemo_bridge").is_absolute()
```

It should additionally load the packaged plugin from an ephemeral
`ASTRBOT_ROOT/data/plugins/astrbot_plugin_animemo_bridge`, use disabled or empty
credentials so no AniMemo network call occurs, call initialize/terminate through
the AstrBot plugin loader, and verify that route/state files are under
`ASTRBOT_ROOT/data/plugin_data/astrbot_plugin_animemo_bridge`. This touches only
the CI runner's temporary directory, never production.

## Gate closure verification

- [x] Replace Context/data-path probing with `StarTools.get_data_dir(...)`.
- [x] Import `MessageChain` only from `astrbot.api.event`.
- [x] Use `event.is_admin()` (or the ADMIN permission filter where command
  structure permits it).
- [x] Add `astrbot_version: ">=4.27.2"` and validate it during packaging.
- [x] Add a pinned `v4.27.2` + `30e20318...` real-runtime CI matrix.
- [x] Assert that the real AstrBot classes were imported and that data lands in
  `data/plugin_data`, then exercise loader initialize/terminate without network.
- [x] Keep the existing authenticated diagnostics route model and document that
  `plugin`-scoped API keys, not only WebUI sessions, are authorized callers.

Local isolated smoke runs passed against both audited revisions. GitHub CI is
the authoritative repeat of this compatibility gate before merge; neither the
local smoke nor CI connects a real bot account, production AniMemo, or pairing
credential.
