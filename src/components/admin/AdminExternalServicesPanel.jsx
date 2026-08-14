import { useEffect, useState } from "react";

import { api, readableApiError } from "../../lib/api.js";
import { Icon } from "../Icon.jsx";


const EMPTY_PROVIDER = {
  provider: "bangumi",
  display_name: "Bangumi",
  enabled: false,
  enabled_source: "not_configured",
  client_id: "",
  client_id_source: "not_configured",
  client_secret_configured: false,
  client_secret_source: "not_configured",
  oauth_callback: "",
  oauth_available: false,
};

function sourceLabel(source) {
  return {
    database: "管理员后台",
    environment: "服务器环境变量",
    not_configured: "未配置",
  }[source] || "未配置";
}

function normalizedProvider(data) {
  return { ...EMPTY_PROVIDER, ...(data || {}) };
}

export function AdminExternalServicesPanel({ onNotice, onError }) {
  const [provider, setProvider] = useState(EMPTY_PROVIDER);
  const [enabled, setEnabled] = useState(false);
  const [enabledDirty, setEnabledDirty] = useState(false);
  const [clientId, setClientId] = useState("");
  const [clientIdDirty, setClientIdDirty] = useState(false);
  const [clientSecret, setClientSecret] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [clearingSecret, setClearingSecret] = useState(false);

  const applyProvider = (data) => {
    const next = normalizedProvider(data);
    setProvider(next);
    setEnabled(Boolean(next.enabled));
    setEnabledDirty(false);
    setClientId(next.client_id || "");
    setClientIdDirty(false);
    setClientSecret("");
  };

  const load = async () => {
    setLoading(true);
    try {
      applyProvider((await api.get("staff/external-providers/bangumi/")).data);
    } catch (error) {
      onError(readableApiError(error, "Bangumi 外部服务配置加载失败。"));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);

  const save = async (event) => {
    event.preventDefault();
    if (saving || clearingSecret) return;
    const body = {};
    if (enabledDirty) body.enabled = enabled;
    if (clientIdDirty) body.client_id = clientId.trim();
    if (clientSecret.trim()) body.client_secret = clientSecret.trim();
    setSaving(true);
    try {
      applyProvider((await api.patch("staff/external-providers/bangumi/", body)).data);
      onNotice("Bangumi 外部服务配置已保存");
    } catch (error) {
      setClientSecret("");
      onError(readableApiError(error, "Bangumi 外部服务配置保存失败。"));
    } finally {
      setSaving(false);
    }
  };

  const clearDatabaseSecret = async () => {
    if (saving || clearingSecret || provider.client_secret_source !== "database") return;
    if (!window.confirm("确认清除后台保存的 Bangumi OAuth App Secret？若服务器环境变量已配置，将自动回退使用环境变量。")) return;
    setClearingSecret(true);
    try {
      applyProvider((await api.delete("staff/external-providers/bangumi/client-secret/")).data);
      onNotice("Bangumi 数据库 Secret 已清除");
    } catch (error) {
      onError(readableApiError(error, "Bangumi 数据库 Secret 清除失败。"));
    } finally {
      setClearingSecret(false);
    }
  };

  if (loading) return <div className="admin-dashboard-loading"><span /><span /><span /> 正在读取外部服务配置</div>;

  return (
    <section className="admin-panel admin-panel--full admin-external-services">
      <header>
        <div><span>EXTERNAL SERVICES</span><h3>外部服务</h3><p>管理 AniMemo 使用的第三方应用凭据。密钥只写入服务器，不会回传浏览器。</p></div>
        <button type="button" onClick={load} disabled={saving || clearingSecret}><Icon name="reset" /> 刷新状态</button>
      </header>
      <form onSubmit={save}>
        <div className="admin-external-services__provider-head">
          <div><span>BANGUMI OAUTH</span><h4>Bangumi</h4><p>用于用户授权读取收藏。Personal Access Token 与公共条目检索不依赖此配置。</p></div>
          <div className={`admin-external-services__availability${provider.oauth_available ? " is-ready" : ""}`}><Icon name={provider.oauth_available ? "circle-check" : "warning"} /> OAuth {provider.oauth_available ? "可用" : "不可用"}</div>
        </div>

        <div className="admin-external-services__body">
          <label className="admin-registration-switch admin-external-services__switch">
            <input type="checkbox" checked={enabled} onChange={(event) => { setEnabled(event.target.checked); setEnabledDirty(true); }} />
            <span aria-hidden="true"><i /></span>
            <strong><b>启用 Bangumi 集成</b><small>{enabled ? "OAuth 凭据完整时允许用户发起授权" : "暂停 OAuth 授权，不影响公共检索与 Token 连接"}</small></strong>
          </label>

          <div className="admin-external-services__fields">
            <label><span>OAuth App ID</span><input value={clientId} onChange={(event) => { setClientId(event.target.value); setClientIdDirty(true); }} autoComplete="off" placeholder="填写 Bangumi OAuth App ID" /><small>当前来源：{sourceLabel(provider.client_id_source)}。清空并保存可移除数据库覆盖。</small></label>
            <label><span>OAuth App Secret</span><input type="password" value={clientSecret} onChange={(event) => setClientSecret(event.target.value)} autoComplete="new-password" placeholder={provider.client_secret_configured ? "已配置，填写新值可替换" : "填写 Bangumi OAuth App Secret"} /><small>状态：{provider.client_secret_configured ? "已配置" : "未配置"} · 来源：{sourceLabel(provider.client_secret_source)}。保存后不会显示原值。</small></label>
          </div>

          <div className="admin-external-services__secret-actions">
            <div><Icon name="key" /><span><b>Secret 数据库配置</b><small>{provider.client_secret_source === "database" ? "当前由管理员后台提供，可单独清除。" : "当前没有数据库 Secret 覆盖。"}</small></span></div>
            <button type="button" className="is-danger" disabled={provider.client_secret_source !== "database" || saving || clearingSecret} onClick={clearDatabaseSecret}><Icon name="trash" /> {clearingSecret ? "正在清除..." : "清除数据库 Secret"}</button>
          </div>

          <div className="admin-external-services__status-grid">
            <div><span>App ID 来源</span><strong>{sourceLabel(provider.client_id_source)}</strong></div>
            <div><span>App Secret 来源</span><strong>{sourceLabel(provider.client_secret_source)}</strong></div>
            <div><span>集成状态</span><strong>{provider.enabled ? "已启用" : "已停用"} · {sourceLabel(provider.enabled_source)}</strong></div>
            <div><span>OAuth 状态</span><strong>{provider.oauth_available ? "可用" : "不可用"}</strong></div>
          </div>

          <label className="admin-external-services__callback"><span>OAuth 回调地址 · 只读</span><input readOnly value={provider.oauth_callback || "尚未配置公共站点地址"} /><small>回调地址由 AniMemo 公共站点地址生成，不能在浏览器中编辑。</small></label>
        </div>

        <footer>
          <span><Icon name="shield" /> GET 接口仅返回配置状态，不返回 Secret、掩码片段或密文。</span>
          <button type="submit" disabled={saving || clearingSecret}><Icon name="check" /> {saving ? "正在保存..." : clientSecret ? "保存并替换 Secret" : "保存 Bangumi 配置"}</button>
        </footer>
      </form>
    </section>
  );
}
