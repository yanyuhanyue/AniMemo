import { useEffect, useState } from "react";

import { Icon } from "../Icon.jsx";
import { api, readableApiError } from "../../lib/api.js";

const EMPTY_PROVIDER = {
  provider: "bangumi",
  display_name: "Bangumi",
  account_connection_available: true,
  import_available: true,
  oauth_available: false,
  personal_access_token_available: true,
  connection: null,
};

function connectionDate(value) {
  if (!value) return "尚未验证";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "尚未验证" : date.toLocaleString("zh-CN", { hour12: false });
}

export function ExternalAccountPanel({ isDemo, onOpenImport }) {
  const [provider, setProvider] = useState(EMPTY_PROVIDER);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState("");
  const [status, setStatus] = useState({ type: "", message: "" });

  const load = async () => {
    if (isDemo) return;
    setBusy("load");
    try {
      const { data } = await api.get("external-accounts/");
      setProvider(data?.providers?.find((item) => item.provider === "bangumi") || EMPTY_PROVIDER);
    } catch (error) {
      setStatus({ type: "error", message: readableApiError(error, "外部账号状态读取失败。") });
    } finally {
      setBusy("");
    }
  };

  useEffect(() => { load(); }, [isDemo]);

  const connectToken = async (event) => {
    event.preventDefault();
    if (!token.trim()) return;
    setBusy("connect");
    setStatus({ type: "", message: "" });
    try {
      const { data } = await api.post("external-accounts/bangumi/connect/", { access_token: token.trim() });
      setProvider((current) => ({ ...current, connection: data }));
      setToken("");
      setStatus({ type: "success", message: "Bangumi 身份已验证并安全连接。" });
    } catch (error) {
      setStatus({ type: "error", message: readableApiError(error, "Bangumi 连接失败。") });
    } finally {
      setBusy("");
    }
  };

  const authorize = async () => {
    setBusy("authorize");
    setStatus({ type: "", message: "" });
    try {
      const { data } = await api.post("external-accounts/bangumi/authorize/", {});
      if (!data?.authorization_url) throw new Error("missing authorization URL");
      window.location.assign(data.authorization_url);
    } catch (error) {
      setBusy("");
      setStatus({ type: "error", message: readableApiError(error, "Bangumi 授权暂不可用。") });
    }
  };

  const verify = async () => {
    setBusy("verify");
    setStatus({ type: "", message: "" });
    try {
      const { data } = await api.post("external-accounts/bangumi/verify/", {});
      setProvider((current) => ({ ...current, connection: data }));
      setStatus({ type: "success", message: "连接有效，Bangumi 身份已重新验证。" });
    } catch (error) {
      await load();
      setStatus({ type: "error", message: readableApiError(error, "连接验证失败，请重新授权。") });
    } finally {
      setBusy("");
    }
  };

  const disconnect = async () => {
    if (!window.confirm("断开后不会删除已导入的番剧、评分、评论或外部作品绑定。确认断开 Bangumi 吗？")) return;
    setBusy("disconnect");
    setStatus({ type: "", message: "" });
    try {
      await api.delete("external-accounts/bangumi/");
      setProvider((current) => ({ ...current, connection: null }));
      setToken("");
      setStatus({ type: "success", message: "Bangumi 已断开，本地数据保持不变。" });
    } catch (error) {
      setStatus({ type: "error", message: readableApiError(error, "断开连接失败。") });
    } finally {
      setBusy("");
    }
  };

  const connection = provider.connection;
  return (
    <div id="dashboard-external-panel" role="tabpanel" aria-labelledby="dashboard-external-tab" className="dashboard-external-panel">
      <div className="dashboard-external-heading dashboard-profile-modal__piece">
        <span className="dashboard-external-heading__icon"><Icon name="link" /></span>
        <div><strong>Bangumi 账号</strong><small>只读发现收藏，由你确认后才导入 AniMemo。</small></div>
        <span className={`dashboard-external-state ${connection ? "is-connected" : ""}`}>{connection ? "已连接" : "未连接"}</span>
      </div>

      {isDemo ? (
        <div className="dashboard-external-empty dashboard-profile-modal__piece"><Icon name="shield" /><strong>演示模式不连接真实账号</strong><small>登录后可使用 OAuth 或 Bangumi Access Token。</small></div>
      ) : busy === "load" ? (
        <div className="dashboard-external-empty dashboard-profile-modal__piece" role="status"><Icon name="spinner" spin /><strong>正在读取连接状态</strong></div>
      ) : connection ? (
        <div className="dashboard-external-connected dashboard-profile-modal__piece">
          <div className="dashboard-external-identity">
            {connection.avatar_url ? <img src={connection.avatar_url} alt="" /> : <span><Icon name="user" /></span>}
            <div><strong>{connection.display_name || connection.username}</strong><small>@{connection.username}</small></div>
          </div>
          <dl>
            <div><dt>连接方式</dt><dd>{connection.auth_method === "oauth" ? "OAuth" : "Access Token"}</dd></div>
            <div><dt>最近验证</dt><dd>{connectionDate(connection.verified_at)}</dd></div>
            <div><dt>状态</dt><dd>{connection.status === "needs_reauthorization" ? "需要重新授权" : "连接正常"}</dd></div>
          </dl>
          <div className="dashboard-external-actions">
            <button type="button" onClick={verify} disabled={Boolean(busy) || !provider.account_connection_available} title="验证连接"><Icon name={busy === "verify" ? "spinner" : "shield"} spin={busy === "verify"} /> 验证连接</button>
            <button type="button" className="is-import" onClick={onOpenImport} disabled={Boolean(busy) || !provider.import_available || connection.status === "needs_reauthorization"}><Icon name="export" /> 导入收藏</button>
            <button type="button" className="is-danger" onClick={disconnect} disabled={Boolean(busy)}><Icon name={busy === "disconnect" ? "spinner" : "unlink"} spin={busy === "disconnect"} /> 断开</button>
          </div>
        </div>
      ) : !provider.account_connection_available ? (
        <div className="dashboard-external-empty dashboard-profile-modal__piece"><Icon name="shield" /><strong>Bangumi 账号连接暂不可用</strong><small>作品搜索、资料绑定与手动刷新不受影响。</small></div>
      ) : (
        <div className="dashboard-external-connect dashboard-profile-modal__piece">
          {provider.oauth_available && <button type="button" className="dashboard-external-oauth" onClick={authorize} disabled={Boolean(busy)}><Icon name={busy === "authorize" ? "spinner" : "link"} spin={busy === "authorize"} /><span><strong>使用 Bangumi OAuth 连接</strong><small>跳转至 Bangumi 完成授权</small></span><Icon name="arrow-up-right" /></button>}
          {provider.personal_access_token_available && <form onSubmit={connectToken}>
            <label><span><Icon name="key" /> Bangumi Access Token</span><input type="password" autoComplete="off" value={token} onChange={(event) => setToken(event.target.value)} placeholder="粘贴后验证，提交成功后不再显示" maxLength={4096} required /></label>
            <button type="submit" disabled={Boolean(busy) || !token.trim()}><Icon name={busy === "connect" ? "spinner" : "link"} spin={busy === "connect"} /> {busy === "connect" ? "正在验证" : "验证并连接"}</button>
          </form>}
          <a href="https://next.bgm.tv/demo/access-token" target="_blank" rel="noreferrer"><Icon name="arrow-up-right" /> 打开 Bangumi 官方 Token 页面</a>
        </div>
      )}
      {status.message && <p className={`dashboard-external-notice is-${status.type}`} role="status">{status.message}</p>}
    </div>
  );
}
