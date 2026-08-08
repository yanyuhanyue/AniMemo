import { useCallback, useEffect, useMemo, useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { Icon } from "../components/Icon.jsx";
import { api, readableApiError } from "../lib/api.js";

const TABS = [
  ["marketplace", "插件市场", "store"],
  ["installed", "已安装", "puzzle"],
  ["mine", "我的插件", "code"],
];

function VersionState({ version }) {
  const labels = { draft: "草稿", submitted: "审核中", approved: "已通过", rejected: "已拒绝", revoked: "已撤销" };
  return <span className={`plugin-platform-state is-${version.review_status}`}>{labels[version.review_status] || version.review_status}</span>;
}

export function PluginPlatformPage({ authUser }) {
  const navigate = useNavigate();
  const [tab, setTab] = useState("marketplace");
  const [marketplace, setMarketplace] = useState([]);
  const [projects, setProjects] = useState([]);
  const [installedRows, setInstalledRows] = useState([]);
  const [installed, setInstalled] = useState(new Map());
  const [policy, setPolicy] = useState(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [draft, setDraft] = useState({ plugin_id: "", slug: "", name: "", description: "" });

  const load = useCallback(async () => {
    setError("");
    try {
      const [marketResponse, installedResponse, mineResponse] = await Promise.all([
        api.get("plugins/marketplace/"),
        api.get("plugins/installed/"),
        api.get("plugins/my/"),
      ]);
      const marketRows = marketResponse.data?.plugins || [];
      const userInstalledRows = installedResponse.data?.plugins || [];
      setMarketplace(marketRows);
      setInstalledRows(userInstalledRows);
      setProjects(mineResponse.data?.projects || []);
      setPolicy(mineResponse.data?.policy || null);
      setInstalled(new Map(userInstalledRows.map((plugin) => [plugin.slug, plugin.installation])));
    } catch (requestError) {
      setError(readableApiError(requestError, "插件平台加载失败。"));
    }
  }, []);

  useEffect(() => { if (authUser) void load(); }, [authUser, load]);
  if (!authUser) return <Navigate to="/login" replace state={{ from: "/plugins" }} />;

  const flash = (message) => {
    setNotice(message);
    window.setTimeout(() => setNotice(""), 2600);
  };

  const install = async (plugin) => {
    setBusy(plugin.slug);
    try {
      const response = await api.post(`plugins/marketplace/${plugin.slug}/install/`);
      setInstalled((current) => new Map(current).set(plugin.slug, response.data));
      flash(`${plugin.name} 已安装`);
      await load();
      window.dispatchEvent(new Event("anime-journal:plugins-changed"));
    } catch (requestError) {
      setError(readableApiError(requestError, "插件安装失败。"));
    } finally { setBusy(""); }
  };

  const updateInstall = async (plugin, enabled) => {
    setBusy(plugin.slug);
    try {
      const response = await api.patch(`plugins/marketplace/${plugin.slug}/installation/`, { enabled });
      setInstalled((current) => new Map(current).set(plugin.slug, response.data));
      flash(`${plugin.name} 已${enabled ? "启用" : "停用"}`);
      await load();
      window.dispatchEvent(new Event("anime-journal:plugins-changed"));
    } catch (requestError) {
      setError(readableApiError(requestError, "插件状态更新失败。"));
    } finally { setBusy(""); }
  };

  const uninstall = async (plugin, deleteData = false) => {
    if (deleteData && !window.confirm(`确定卸载 ${plugin.name} 并永久删除你的插件数据？`)) return;
    setBusy(plugin.slug);
    try {
      await api.delete(`plugins/marketplace/${plugin.slug}/installation/`, { data: { delete_data: deleteData } });
      setInstalled((current) => { const next = new Map(current); next.delete(plugin.slug); return next; });
      flash(deleteData ? `${plugin.name} 已卸载并删除个人数据` : `${plugin.name} 已卸载，个人数据已保留`);
      await load();
      window.dispatchEvent(new Event("anime-journal:plugins-changed"));
    } catch (requestError) {
      setError(readableApiError(requestError, "插件卸载失败。"));
    } finally { setBusy(""); }
  };

  const updateConfig = async (plugin, key, value) => {
    const current = installed.get(plugin.slug) || plugin.installation || {};
    setBusy(`config:${plugin.slug}`);
    try {
      await api.patch(`plugins/marketplace/${plugin.slug}/installation/`, {
        config: { ...(current.config || {}), [key]: value },
      });
      flash(`${plugin.name} 配置已保存`);
      await load();
    } catch (requestError) {
      setError(readableApiError(requestError, "插件配置保存失败。"));
    } finally { setBusy(""); }
  };

  const createProject = async (event) => {
    event.preventDefault();
    setBusy("create");
    try {
      await api.post("plugins/my/", draft);
      setDraft({ plugin_id: "", slug: "", name: "", description: "" });
      flash("插件项目已创建");
      await load();
    } catch (requestError) {
      setError(readableApiError(requestError, "创建插件项目失败。"));
    } finally { setBusy(""); }
  };

  const editProject = async (project) => {
    const name = window.prompt("插件名称", project.name);
    if (name === null) return;
    const description = window.prompt("插件说明", project.description);
    if (description === null) return;
    setBusy(`project:${project.id}`);
    try {
      await api.patch(`plugins/my/${project.id}/`, { name, description });
      flash("插件项目信息已更新");
      await load();
    } catch (requestError) {
      setError(readableApiError(requestError, "插件项目更新失败。"));
    } finally { setBusy(""); }
  };

  const archiveProject = async (project) => {
    if (!window.confirm(`确定删除或归档插件项目 ${project.name}？`)) return;
    setBusy(`project:${project.id}`);
    try {
      const response = await api.delete(`plugins/my/${project.id}/`);
      flash(response.data?.result === "archived" ? "已发布项目已归档" : "未发布项目已删除");
      await load();
    } catch (requestError) {
      setError(readableApiError(requestError, "插件项目处理失败。"));
    } finally { setBusy(""); }
  };

  const withdrawSubmission = async (version) => {
    if (!version.submission?.id) return;
    setBusy(`withdraw:${version.id}`);
    try {
      await api.post(`plugins/my/submissions/${version.submission.id}/withdraw/`);
      flash("审核提交已撤回");
      await load();
    } catch (requestError) {
      setError(readableApiError(requestError, "撤回审核失败。"));
    } finally { setBusy(""); }
  };

  const uploadVersion = async (project, file) => {
    if (!file) return;
    setBusy(`upload:${project.id}`);
    const payload = new FormData();
    payload.append("archive", file);
    try {
      await api.post(`plugins/my/${project.id}/versions/`, payload);
      flash(`${project.name} 新版本已保存为草稿`);
      await load();
    } catch (requestError) {
      setError(readableApiError(requestError, "版本上传失败。"));
    } finally { setBusy(""); }
  };

  const versionAction = async (version, action) => {
    setBusy(`${action}:${version.id}`);
    try {
      const response = await api.post(`plugins/my/versions/${version.id}/${action}/`);
      if (action === "preview" && response.data?.preview) window.open(response.data.preview, "_blank", "noopener,noreferrer");
      flash(action === "submit" ? "版本已提交审核" : "私人预览已生成");
      await load();
    } catch (requestError) {
      setError(readableApiError(requestError, action === "submit" ? "提交审核失败。" : "生成预览失败。"));
    } finally { setBusy(""); }
  };

  const visibleMarket = useMemo(() => tab === "installed" ? installedRows : marketplace, [installedRows, marketplace, tab]);

  return (
    <main className="plugin-platform-page">
      <header className="plugin-platform-header">
        <button type="button" className="plugin-platform-back" onClick={() => navigate("/dashboard")} title="返回手账"><Icon name="arrow-left" /></button>
        <div><span>PLUGIN PLATFORM V3</span><h1>插件中心</h1><p>共享运行时代码，隔离每位用户的启用状态、配置与数据。</p></div>
      </header>

      <nav className="plugin-platform-tabs" aria-label="插件中心视图">
        {TABS.map(([key, label, icon]) => <button key={key} type="button" className={tab === key ? "is-active" : ""} onClick={() => setTab(key)}><Icon name={icon} />{label}</button>)}
      </nav>

      {notice && <div className="plugin-platform-notice"><Icon name="check" />{notice}</div>}
      {error && <div className="plugin-platform-error"><Icon name="warning" />{error}<button type="button" onClick={() => setError("")}><Icon name="close" /></button></div>}

      {tab !== "mine" && (
        <section className="plugin-platform-grid" aria-label={tab === "installed" ? "已安装插件" : "插件市场"}>
          {visibleMarket.map((plugin) => {
            const installation = installed.get(plugin.slug);
            const current = plugin.current_version
              ? { version: plugin.current_version, runtime_types: plugin.runtime_types }
              : plugin.versions?.find((version) => version.published_at) || plugin.versions?.[0];
            return <article className="plugin-market-card" key={plugin.slug}>
              <div className="plugin-market-card__title"><span><Icon name="puzzle" /></span><div><h2>{plugin.name}</h2><code>{plugin.slug}</code></div></div>
              <p>{plugin.description}</p>
              <dl><div><dt>版本</dt><dd>{current?.version || "-"}</dd></div><div><dt>运行时</dt><dd>{current?.runtime_types?.join(" + ") || "frontend"}</dd></div><div><dt>发布者</dt><dd>{plugin.owner || "Anime Journal"}</dd></div></dl>
              {tab === "installed" && !plugin.published && <small className="plugin-market-card__notice">此版本已从市场下架，你的现有安装仍保留。</small>}
              {tab === "installed" && !plugin.available && <small className="plugin-market-card__notice is-danger">当前 Runtime 不可用。</small>}
              {tab === "installed" && plugin.settings?.length > 0 && <div className="plugin-market-card__settings">
                {plugin.settings.map((definition) => <label key={definition.key}><span>{definition.label}</span>
                  {definition.type === "select" ? <select defaultValue={installation?.config?.[definition.key] ?? definition.default ?? ""} disabled={busy === `config:${plugin.slug}`} onChange={(event) => updateConfig(plugin, definition.key, event.target.value)}>{(definition.choices || []).map((choice) => <option key={choice.value} value={choice.value}>{choice.label}</option>)}</select>
                    : <input type={definition.type === "number" ? "number" : "text"} min={definition.min} max={definition.max} defaultValue={installation?.config?.[definition.key] ?? definition.default ?? ""} disabled={busy === `config:${plugin.slug}`} onBlur={(event) => updateConfig(plugin, definition.key, definition.type === "number" ? Number(event.target.value) : event.target.value)} />}
                </label>)}
              </div>}
              <footer>
                {!installation && <button type="button" disabled={busy === plugin.slug} onClick={() => install(plugin)}><Icon name="download" />安装</button>}
                {installation && <button type="button" className={installation.enabled ? "is-disable" : "is-enable"} disabled={busy === plugin.slug} onClick={() => updateInstall(plugin, !installation.enabled)}><Icon name={installation.enabled ? "pause" : "play"} />{installation.enabled ? "停用" : "启用"}</button>}
                {installation && <button type="button" className="is-quiet" disabled={busy === plugin.slug} onClick={() => uninstall(plugin)}><Icon name="trash" />卸载</button>}
                {installation && tab === "installed" && <button type="button" className="is-danger" disabled={busy === plugin.slug} onClick={() => uninstall(plugin, true)}><Icon name="trash" />卸载并删除数据</button>}
              </footer>
            </article>;
          })}
          {visibleMarket.length === 0 && <div className="plugin-platform-empty"><Icon name="puzzle" /><strong>{tab === "installed" ? "尚未安装市场插件" : "市场暂时没有已发布插件"}</strong></div>}
        </section>
      )}

      {tab === "mine" && <section className="plugin-developer-workspace">
        {policy && <div className="plugin-platform-policy"><strong>上传限制</strong><span>单包最大 {Math.round((policy.package?.max_package_bytes || 0) / 1024 / 1024)} MB</span><span>最多 {policy.package?.max_files || 0} 个文件</span><span>草稿上限 {policy.draft_limit}</span><span>每小时 {policy.uploads_per_hour} 次</span></div>}
        <form className="plugin-project-form" onSubmit={createProject}>
          <div><span>NEW PROJECT</span><h2>创建插件项目</h2></div>
          <label>Plugin ID<input required value={draft.plugin_id} onChange={(event) => setDraft({ ...draft, plugin_id: event.target.value })} placeholder="com.example.my-plugin" /></label>
          <label>Slug<input required value={draft.slug} onChange={(event) => setDraft({ ...draft, slug: event.target.value })} placeholder="my-plugin" /></label>
          <label>名称<input required value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
          <label className="is-wide">说明<textarea required value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} rows="2" /></label>
          <button type="submit" disabled={busy === "create"}><Icon name="plus" />创建</button>
        </form>

        <div className="plugin-project-list">
          {projects.map((project) => <article className="plugin-project-row" key={project.id}>
            <header><div><h2>{project.name}</h2><code>{project.plugin_id}</code></div><div className="plugin-project-actions"><button type="button" onClick={() => editProject(project)} disabled={busy === `project:${project.id}`} title="编辑项目"><Icon name="edit" /></button><button type="button" onClick={() => archiveProject(project)} disabled={busy === `project:${project.id}`} title="删除或归档项目"><Icon name="trash" /></button><label className="plugin-upload-button"><Icon name="upload" />上传版本<input type="file" accept=".ajplugin,application/zip" disabled={busy === `upload:${project.id}`} onChange={(event) => uploadVersion(project, event.target.files?.[0])} /></label></div></header>
            <p>{project.description}</p>
            <small>{project.install_count || 0} 位用户安装{project.deployment ? ` · 当前部署 ${project.deployment.current_version}` : " · 尚未部署"}</small>
            <div className="plugin-version-list">
              {project.versions.map((version) => <div key={version.id} className="plugin-version-row">
                <strong>v{version.version}</strong><VersionState version={version} /><code>{version.package_sha256.slice(0, 12)}</code><span>{version.runtime_types.join(" + ")}</span>
                <div>
                  {version.review_status === "draft" && version.runtime_types.length === 1 && version.runtime_types[0] === "frontend" && <button type="button" onClick={() => versionAction(version, "preview")} disabled={busy === `preview:${version.id}`} title="私人预览"><Icon name="eye" /></button>}
                  {["draft", "rejected"].includes(version.review_status) && <button type="button" onClick={() => versionAction(version, "submit")} disabled={busy === `submit:${version.id}`} title="提交审核"><Icon name="send" /></button>}
                  {version.submission?.status === "submitted" && <button type="button" onClick={() => withdrawSubmission(version)} disabled={busy === `withdraw:${version.id}`} title="撤回审核"><Icon name="close" /></button>}
                </div>
                {version.submission?.security_report?.dangerous_findings?.length > 0 && <small className="plugin-version-warning">{version.submission.security_report.dangerous_findings.length} 项安全扫描提示</small>}
              </div>)}
              {project.versions.length === 0 && <small>尚未上传版本。</small>}
            </div>
          </article>)}
        </div>
      </section>}
    </main>
  );
}
