import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Icon } from "../Icon.jsx";
import { api, readableApiError } from "../../lib/api.js";

const EMPTY = {
  plugins: [],
  can_install: false,
  policy: { max_package_bytes: 25 * 1024 * 1024 },
  summary: { installed: 0, enabled: 0, attention: 0 },
};

function validateArchive(file, maxArchiveBytes) {
  if (!file) return "";
  if (!file.name.toLocaleLowerCase("en-US").endsWith(".ajplugin")) return "只支持 .ajplugin 格式的插件包。";
  if (file.size > maxArchiveBytes) return `插件压缩包不能超过 ${Math.round(maxArchiveBytes / 1024 / 1024)} MB。`;
  return "";
}

function SecurityReport({ report = {} }) {
  const warnings = [...(report.dangerous_findings || []), ...(report.css_global_selectors || []).map((item) => `全局 CSS 选择器：${item}`)];
  return <details className="admin-plugin-scan"><summary>安全扫描 · {warnings.length} 项提示</summary><dl><div><dt>文件</dt><dd>{report.file_count ?? 0}</dd></div><div><dt>包体</dt><dd>{report.package_size ?? 0} B</dd></div><div><dt>解压</dt><dd>{report.uncompressed_size ?? 0} B</dd></div><div><dt>后端</dt><dd>{report.contains_backend ? "是" : "否"}</dd></div></dl>{warnings.map((warning) => <p key={warning}><Icon name="warning" />{warning}</p>)}</details>;
}

function policyLabel(key, value) {
  const labels = {
    storesPersonalData: "存储个人数据",
    usesExternalNetwork: "访问外部网络",
    acceptsFileUploads: "接收文件上传",
    retainsDataOnDisable: "停用后保留数据",
  };
  return `${labels[key] || key}：${value ? "是" : "否"}`;
}

function statusText(plugin) {
  if (plugin.effective_enabled) return "运行中";
  if (plugin.enabled) return "已启用但不可用";
  if (!plugin.ready) return "需要处理";
  return "已停用";
}

function SettingField({ definition, pluginSlug, value, disabled, onChange }) {
  const id = `plugin-setting-${pluginSlug}-${definition.key}`;
  const common = {
    id,
    disabled,
    value: value ?? "",
    onChange: (event) => onChange(definition.key, event.target.value),
  };

  if (definition.type === "boolean") {
    return (
      <label className="admin-plugin-setting admin-plugin-setting--toggle" htmlFor={id}>
        <input
          id={id}
          type="checkbox"
          disabled={disabled}
          checked={Boolean(value)}
          onChange={(event) => onChange(definition.key, event.target.checked)}
        />
        <span aria-hidden="true"><i /></span>
        <strong>{definition.label}<small>{definition.description}</small></strong>
      </label>
    );
  }

  if (definition.type === "textarea") {
    return <label className="admin-plugin-setting" htmlFor={id}><span>{definition.label}</span><textarea {...common} rows="3" /><small>{definition.description}</small></label>;
  }

  if (definition.type === "number") {
    return <label className="admin-plugin-setting" htmlFor={id}><span>{definition.label}</span><input {...common} type="number" min={definition.min ?? undefined} max={definition.max ?? undefined} onChange={(event) => onChange(definition.key, event.target.value === "" ? "" : Number(event.target.value))} /><small>{definition.description}</small></label>;
  }

  if (definition.type === "select") {
    return (
      <label className="admin-plugin-setting" htmlFor={id}>
        <span>{definition.label}</span>
        <select {...common}>
          {definition.choices.map((choice) => {
            const option = typeof choice === "object" ? choice : { value: choice, label: choice };
            return <option key={option.value} value={option.value}>{option.label}</option>;
          })}
        </select>
        <small>{definition.description}</small>
      </label>
    );
  }

  return <label className="admin-plugin-setting" htmlFor={id}><span>{definition.label}</span><input {...common} type="text" /><small>{definition.description}</small></label>;
}

function PluginCard({ plugin, busy, onUpdate, onNotice }) {
  const [draft, setDraft] = useState(plugin.config || {});

  useEffect(() => {
    setDraft(plugin.config || {});
  }, [plugin.config]);

  const updateDraft = (key, value) => {
    setDraft((current) => ({ ...current, [key]: value }));
  };

  const saveConfig = async (event) => {
    event.preventDefault();
    const updated = await onUpdate(plugin.slug, { config: draft });
    if (updated) onNotice(`${plugin.name}配置已保存`);
  };

  const toggle = async () => {
    const updated = await onUpdate(plugin.slug, { enabled: !plugin.enabled });
    if (updated) onNotice(`${plugin.name}已${updated.enabled ? "启用" : "停用"}`);
  };

  return (
    <article className={`admin-plugin-card${plugin.effective_enabled ? " is-running" : ""}${!plugin.ready ? " is-attention" : ""}`}>
      <header className="admin-plugin-card__header">
        <span className="admin-plugin-card__mark"><Icon name="puzzle" /></span>
        <div className="admin-plugin-card__identity">
          <div><h3>{plugin.name}</h3><code>{plugin.slug}</code></div>
          <p>{plugin.description || "该插件未提供说明。"}</p>
        </div>
        <span className={`admin-plugin-card__status${plugin.effective_enabled ? " is-running" : ""}`}>{statusText(plugin)}</span>
      </header>

      <div className="admin-plugin-card__meta">
        <span><b>VERSION</b>{plugin.version || "未知"}</span>
        <span><b>AUTHOR</b>{plugin.author?.name || "未知"}</span>
        <span><b>LICENSE</b>{plugin.license || "未声明"}</span>
        <span><b>APP RANGE</b>{plugin.app_compatibility?.min || "?"} – {plugin.app_compatibility?.maxExclusive || "?"}</span>
      </div>

      <div className="admin-plugin-health">
        <span className={plugin.sdk_compatible !== false ? "is-ok" : "is-error"}><Icon name={plugin.sdk_compatible !== false ? "check" : "close"} /> SDK API {plugin.sdk_api || 2}</span>
        <span className={plugin.compatible ? "is-ok" : "is-error"}><Icon name={plugin.compatible ? "check" : "close"} /> 版本兼容</span>
        <span className={plugin.backend.ready ? "is-ok" : "is-error"}><Icon name={plugin.backend.ready ? "check" : "close"} /> 后端入口</span>
        <span className={plugin.frontend.ready ? "is-ok" : "is-error"}><Icon name={plugin.frontend.ready ? "check" : "close"} /> 前端入口</span>
      </div>

      {plugin.errors.length > 0 && <div className="admin-plugin-card__errors" role="alert">{plugin.errors.map((message) => <p key={message}><Icon name="warning" /> {message}</p>)}</div>}

      <div className="admin-plugin-card__details">
        <section><h4>扩展</h4><div className="admin-plugin-chips">{plugin.extensions?.length ? plugin.extensions.map((item) => <span key={item}>{item}</span>) : <em>无</em>}</div></section>
        <section><h4>权限</h4><div className="admin-plugin-chips">{plugin.permissions.length ? plugin.permissions.map((item) => <span key={item.code}>{item.code}</span>) : <em>无</em>}</div></section>
        <section><h4>Hooks</h4><div className="admin-plugin-chips">{plugin.hooks?.length ? plugin.hooks.map((item) => <span key={item}>{item}</span>) : <em>无</em>}</div></section>
        <section><h4>数据策略</h4><div className="admin-plugin-policy">{Object.entries(plugin.data_policy).map(([key, value]) => <span key={key}>{policyLabel(key, value)}</span>)}</div></section>
      </div>

      {plugin.settings.length > 0 && (
        <form className="admin-plugin-settings" onSubmit={saveConfig}>
          <div className="admin-plugin-settings__title"><div><span>PLUGIN SETTINGS</span><h4>插件配置</h4></div><button type="submit" disabled={busy || plugin.errors.length > 0}><Icon name="save" /> 保存配置</button></div>
          <div className="admin-plugin-settings__grid">
            {plugin.settings.map((definition) => <SettingField key={definition.key} definition={definition} pluginSlug={plugin.slug} value={draft[definition.key]} disabled={busy || plugin.errors.length > 0} onChange={updateDraft} />)}
          </div>
        </form>
      )}

      <footer className="admin-plugin-card__footer">
        <div><strong>{plugin.updated_by ? `最近由 ${plugin.updated_by} 更新` : "尚未保存运行状态"}</strong><small>{plugin.backend?.enabled ? "插件接口和前端入口会立即停用；包含 Python 后端的插件需重启服务后才能从运行进程中完全卸载。" : "停用不会删除插件数据或服务器文件。"}</small></div>
        <div className="admin-plugin-card__commands">{plugin.effective_enabled && plugin.frontend?.routePrefix && <a href={plugin.frontend.routePrefix}><Icon name="arrow-right" /> 打开插件</a>}<button type="button" className={plugin.enabled ? "is-disable" : "is-enable"} disabled={busy || (!plugin.ready && !plugin.enabled)} onClick={toggle}><Icon name={plugin.enabled ? "close" : "check"} /> {busy ? "处理中..." : plugin.enabled ? "停用插件" : "启用插件"}</button></div>
      </footer>
    </article>
  );
}

export function PluginManagementPanel({ onNotice }) {
  const [data, setData] = useState(EMPTY);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busySlug, setBusySlug] = useState("");
  const [archive, setArchive] = useState(null);
  const [archiveError, setArchiveError] = useState("");
  const [dragActive, setDragActive] = useState(false);
  const [replaceExisting, setReplaceExisting] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [reviewQueue, setReviewQueue] = useState({ submissions: [], approved_versions: [], deployments: [], marketplace_versions: [] });
  const [lifecycleTab, setLifecycleTab] = useState("review");
  const [reviewBusy, setReviewBusy] = useState("");
  const archiveInputRef = useRef(null);

  const chooseArchive = useCallback((file) => {
    const nextError = validateArchive(file, Number(data.policy?.max_package_bytes || 25 * 1024 * 1024));
    setArchiveError(nextError);
    setArchive(nextError ? null : file || null);
    if (nextError && archiveInputRef.current) archiveInputRef.current.value = "";
  }, [data.policy?.max_package_bytes]);

  const dropArchive = useCallback((event) => {
    event.preventDefault();
    setDragActive(false);
    chooseArchive(event.dataTransfer.files?.[0] || null);
  }, [chooseArchive]);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [response, reviews] = await Promise.all([api.get("staff/plugins/"), api.get("staff/plugins/review/")]);
      setData({ ...EMPTY, ...(response.data || {}) });
      setReviewQueue({ submissions: [], approved_versions: [], deployments: [], marketplace_versions: [], ...(reviews.data || {}) });
    } catch (requestError) {
      setError(readableApiError(requestError, "插件中心加载失败。"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const updatePlugin = useCallback(async (slug, payload) => {
    setBusySlug(slug);
    setError("");
    try {
      const response = await api.patch(`staff/plugins/${slug}/`, payload);
      const updated = response.data;
      setData((current) => {
        const plugins = current.plugins.map((plugin) => plugin.slug === slug ? updated : plugin);
        return {
          ...current,
          plugins,
          summary: {
            installed: plugins.length,
            enabled: plugins.filter((plugin) => plugin.effective_enabled).length,
            attention: plugins.filter((plugin) => !plugin.ready).length,
          },
        };
      });
      return updated;
    } catch (requestError) {
      setError(readableApiError(requestError, "插件状态更新失败。"));
      return null;
    } finally {
      setBusySlug("");
    }
  }, []);

  const installPlugin = async (event) => {
    event.preventDefault();
    if (!archive || installing) return;
    setInstalling(true);
    setError("");
    const payload = new FormData();
    payload.append("archive", archive);
    payload.append("replace", String(replaceExisting));
    try {
      const response = await api.post("staff/plugins/install/", payload);
      setArchive(null);
      setArchiveError("");
      setReplaceExisting(false);
      if (archiveInputRef.current) archiveInputRef.current.value = "";
      onNotice(response.data?.detail || "插件部署已完成");
      await load();
    } catch (requestError) {
      setError(readableApiError(requestError, "插件安装失败。"));
    } finally {
      setInstalling(false);
    }
  };

  const sortedPlugins = useMemo(() => [...data.plugins].sort((a, b) => Number(b.effective_enabled) - Number(a.effective_enabled) || a.name.localeCompare(b.name, "zh-CN")), [data.plugins]);

  const review = async (submission, approve) => {
    setReviewBusy(`review:${submission.id}`);
    setError("");
    try {
      await api.post(`staff/plugins/review/${submission.id}/`, { approve, note: approve ? "审核通过" : "审核拒绝" });
      onNotice(approve ? "插件版本已审核通过" : "插件版本已拒绝");
      await load();
    } catch (requestError) {
      setError(readableApiError(requestError, "审核操作失败。"));
    } finally { setReviewBusy(""); }
  };

  const publish = async (version) => {
    setReviewBusy(`publish:${version.id}`);
    setError("");
    try {
      await api.post(`staff/plugins/versions/${version.id}/publish/`);
      onNotice(`${version.project} ${version.version} 已发布并部署`);
      await load();
    } catch (requestError) {
      setError(readableApiError(requestError, "插件发布失败。"));
    } finally { setReviewBusy(""); }
  };

  const versionAction = async (version, action) => {
    if (action === "revoke" && !window.confirm(`确定撤销 ${version.project || version.slug} v${version.version} 的 Runtime？`)) return;
    setReviewBusy(`${action}:${version.id || version.version_id}`);
    setError("");
    try {
      await api.post(`staff/plugins/versions/${version.id || version.version_id}/${action}/`);
      onNotice(action === "unpublish" ? "插件版本已从市场下架" : "插件版本已撤销");
      await load();
    } catch (requestError) {
      setError(readableApiError(requestError, action === "unpublish" ? "市场下架失败。" : "版本撤销失败。"));
    } finally { setReviewBusy(""); }
  };

  const rollback = async (deployment) => {
    setReviewBusy(`rollback:${deployment.slug}`);
    setError("");
    try {
      await api.post(`staff/plugins/${deployment.slug}/rollback/`);
      onNotice(`${deployment.name} 已回滚`);
      await load();
    } catch (requestError) {
      setError(readableApiError(requestError, "插件回滚失败。"));
    } finally { setReviewBusy(""); }
  };

  if (loading) return <div className="admin-dashboard-loading"><span /><span /><span /> 正在检查插件清单</div>;

  return (
    <section className="admin-plugin-center">
      <div className="admin-plugin-summary">
        <article className="is-yellow"><Icon name="puzzle" /><div><strong>{data.summary.installed}</strong><span>已部署插件</span></div></article>
        <article className="is-teal"><Icon name="check" /><div><strong>{data.summary.enabled}</strong><span>实际运行</span></div></article>
        <article className="is-pink"><Icon name="warning" /><div><strong>{data.summary.attention}</strong><span>需要处理</span></div></article>
      </div>

      <section className="admin-plugin-review-queue">
        <header><div><span>PLUGIN LIFECYCLE</span><h3>插件生命周期</h3></div><strong>{reviewQueue.submissions.length} 待审核 / {reviewQueue.deployments.length} 已部署</strong></header>
        <nav className="admin-plugin-lifecycle-tabs" aria-label="插件生命周期视图">
          <button type="button" className={lifecycleTab === "review" ? "is-active" : ""} onClick={() => setLifecycleTab("review")}>审核</button>
          <button type="button" className={lifecycleTab === "deployed" ? "is-active" : ""} onClick={() => setLifecycleTab("deployed")}>部署</button>
          <button type="button" className={lifecycleTab === "market" ? "is-active" : ""} onClick={() => setLifecycleTab("market")}>市场</button>
        </nav>
        {lifecycleTab === "review" && reviewQueue.submissions.map((submission) => <article key={submission.id}>
          <div><b>{submission.project} v{submission.version}</b><small>{submission.submitter} · {(submission.runtime_types || []).join(" + ")}</small></div>
          <SecurityReport report={submission.security_report} />
          <div><button type="button" className="is-reject" disabled={reviewBusy === `review:${submission.id}`} onClick={() => review(submission, false)}><Icon name="close" />拒绝</button><button type="button" className="is-approve" disabled={reviewBusy === `review:${submission.id}`} onClick={() => review(submission, true)}><Icon name="check" />通过</button></div>
        </article>)}
        {lifecycleTab === "review" && reviewQueue.approved_versions.map((version) => <article key={`approved:${version.id}`}>
          <div><b>{version.project} v{version.version}</b><small>审核通过 · {(version.runtime_types || []).join(" + ")}</small></div>
          <SecurityReport report={version.security_report} />
          <div><button type="button" className="is-publish" disabled={!data.can_install && (version.runtime_types || []).includes("backend") || reviewBusy === `publish:${version.id}`} onClick={() => publish(version)}><Icon name="rocket" />发布</button></div>
        </article>)}
        {lifecycleTab === "review" && reviewQueue.submissions.length === 0 && reviewQueue.approved_versions.length === 0 && <p>当前没有待处理版本。</p>}
        {lifecycleTab === "deployed" && reviewQueue.deployments.map((deployment) => <article key={`deployment:${deployment.slug}`}>
          <div><b>{deployment.name} v{deployment.version}</b><small>{deployment.status} · {deployment.install_count} 位用户安装 · {deployment.disk_bytes} B</small>{deployment.last_error && <small>{deployment.last_error}</small>}</div>
          <div><button type="button" disabled={!deployment.previous_version || reviewBusy === `rollback:${deployment.slug}`} onClick={() => rollback(deployment)}><Icon name="reset" />回滚</button><button type="button" className="is-reject" disabled={!data.can_install || reviewBusy === `revoke:${deployment.version_id}`} onClick={() => versionAction(deployment, "revoke")}><Icon name="close" />撤销</button></div>
        </article>)}
        {lifecycleTab === "deployed" && reviewQueue.deployments.length === 0 && <p>当前没有已部署插件。</p>}
        {lifecycleTab === "market" && reviewQueue.marketplace_versions.map((version) => <article key={`market:${version.id}`}>
          <div><b>{version.name} v{version.version}</b><small>{version.install_count} 位用户安装 · {(version.runtime_types || []).join(" + ")}</small></div>
          <SecurityReport report={version.security_report} />
          <div><button type="button" className="is-reject" disabled={reviewBusy === `unpublish:${version.id}`} onClick={() => versionAction(version, "unpublish")}><Icon name="close" />下架</button><button type="button" className="is-reject" disabled={!data.can_install || reviewBusy === `revoke:${version.id}`} onClick={() => versionAction(version, "revoke")}><Icon name="warning" />撤销</button></div>
        </article>)}
        {lifecycleTab === "market" && reviewQueue.marketplace_versions.length === 0 && <p>市场暂无已发布版本。</p>}
      </section>

      <div className="admin-plugin-deploy-note"><Icon name="shield" /><div><strong>受控插件安装与平滑升级</strong><p>新插件安装后默认停用；覆盖运行中的插件时，系统会自动阻断新请求、原子替换并恢复原启用状态。</p></div><button type="button" onClick={load} disabled={installing}><Icon name="reset" /> 重新扫描</button></div>

      {data.can_install ? (
        <form className="admin-plugin-installer" onSubmit={installPlugin}>
          <div className="admin-plugin-installer__heading"><span className="admin-plugin-installer__mark"><Icon name="upload" /></span><div><span>.AJPLUGIN INSTALLER</span><h3>上传插件包</h3><p>支持 Manifest v2 的 <code>.ajplugin</code> 包，根目录必须包含 <code>manifest.json</code>。</p></div></div>
          <div className="admin-plugin-installer__controls">
            <label
              className={`admin-plugin-archive-picker${dragActive ? " is-dragging" : ""}${archiveError ? " has-error" : ""}`}
              onDragEnter={(event) => { event.preventDefault(); setDragActive(true); }}
              onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; setDragActive(true); }}
              onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setDragActive(false); }}
              onDrop={dropArchive}
            >
              <input ref={archiveInputRef} type="file" accept=".ajplugin,application/zip" onChange={(event) => chooseArchive(event.target.files?.[0] || null)} />
              <Icon name="file-upload" />
              <span><b>{dragActive ? "松开即可读取 .AJPLUGIN" : archive ? archive.name : "拖拽 .AJPLUGIN 到这里，或点击选择"}</b><small>{archiveError || (archive ? `${(archive.size / 1024).toFixed(1)} KB` : `单个文件最大 ${Math.round((data.policy?.max_package_bytes || 0) / 1024 / 1024)} MB`)}</small></span>
            </label>
            <label className="admin-plugin-replace-switch">
              <input type="checkbox" checked={replaceExisting} onChange={(event) => setReplaceExisting(event.target.checked)} />
              <span aria-hidden="true"><i /></span>
              <strong>覆盖同名插件<small>运行中的插件会自动平滑升级</small></strong>
            </label>
            <button type="submit" disabled={!archive || installing}><Icon name={installing ? "spinner" : "upload"} spin={installing} /> {installing ? "正在校验与部署..." : replaceExisting ? "上传并平滑升级" : "上传并安装"}</button>
          </div>
          <p className="admin-plugin-installer__warning"><Icon name="warning" /> 插件可能包含可执行后端代码。只安装来源可信、已审查的插件包。</p>
        </form>
      ) : (
        <div className="admin-plugin-install-locked"><Icon name="lock" /><div><strong>插件安装仅限超级管理员</strong><p>当前账号可以查看和配置插件，但不能上传或替换服务器代码。</p></div></div>
      )}

      {error && <div className="admin-dashboard-alert admin-plugin-error" role="alert"><Icon name="bolt" /> {error}<button type="button" onClick={load}>重试</button></div>}

      {sortedPlugins.length === 0 ? (
        <div className="admin-plugin-empty"><Icon name="puzzle" /><h3>尚未安装插件</h3><p>可以上传符合规范的 .ajplugin 包，或从 <code>plugins/_template</code> 创建新插件。</p></div>
      ) : (
        <div className="admin-plugin-list">{sortedPlugins.map((plugin) => <PluginCard key={plugin.slug} plugin={plugin} busy={busySlug === plugin.slug} onUpdate={updatePlugin} onNotice={onNotice} />)}</div>
      )}
    </section>
  );
}
