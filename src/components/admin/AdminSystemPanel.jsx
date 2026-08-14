import { useEffect, useState } from "react";
import { QRCodeSVG } from "qrcode.react";

import { api, readableApiError, storeTokens } from "../../lib/api.js";
import { Icon } from "../Icon.jsx";
import { AdminConfirmDialog } from "./AdminControlDialogs.jsx";
import { downloadBlob, hasCapability } from "./adminControlUtils.js";


export function AdminSystemPanel({ viewer, onNotice, onError }) {
  const [health, setHealth] = useState(null);
  const [healthLoading, setHealthLoading] = useState(false);
  const [security, setSecurity] = useState(null);
  const [setup, setSetup] = useState(null);
  const [setupExpiresAt, setSetupExpiresAt] = useState(0);
  const [secondsRemaining, setSecondsRemaining] = useState(0);
  const [recoveryCodes, setRecoveryCodes] = useState([]);
  const [twoFactorForm, setTwoFactorForm] = useState({
    beginPassword: "", currentCode: "", confirmCode: "",
    disablePassword: "", disableCode: "", recoveryCode: "",
    regeneratePassword: "", regenerateCode: "",
  });
  const [exportConfirmation, setExportConfirmation] = useState(null);
  const canSystem = hasCapability(viewer, "manage_system");
  const canBackup = hasCapability(viewer, "backup_data");

  const loadHealth = async () => {
    setHealthLoading(true);
    try { setHealth((await api.get("staff/system/health/")).data); }
    catch (error) { onError(readableApiError(error)); }
    finally { setHealthLoading(false); }
  };
  const loadSecurity = async () => {
    try { setSecurity((await api.get("staff/security/two-factor/")).data); }
    catch (error) { onError(readableApiError(error)); }
  };
  useEffect(() => { if (canSystem) void loadHealth(); void loadSecurity(); }, [canSystem]);
  useEffect(() => {
    if (!setupExpiresAt) { setSecondsRemaining(0); return undefined; }
    const update = () => setSecondsRemaining(Math.max(0, Math.ceil((setupExpiresAt - Date.now()) / 1000)));
    update();
    const timer = window.setInterval(update, 1000);
    return () => window.clearInterval(timer);
  }, [setupExpiresAt]);

  const update2fa = (key, value) => setTwoFactorForm((current) => ({ ...current, [key]: value }));
  const numericCode = (value) => value.replace(/\D/g, "").slice(0, 6);
  const acceptRotatedSession = (data) => {
    if (data?.access) storeTokens({ access: data.access });
  };

  const exportData = async (format, kind = "all") => {
    try {
      const response = await api.get("staff/system/backup/", { params: { export_format: format, kind }, responseType: "blob", timeout: 30000 });
      downloadBlob(response.data, response.headers["content-disposition"], `animemo-${kind}.${format === "csv" ? "csv" : "zip"}`);
      onNotice(format === "zip" ? "完整安全备份已导出" : "CSV 数据已导出");
    } catch (error) { onError(readableApiError(error, "备份生成失败。")); }
  };
  const askExport = (format, kind = "all") => {
    const label = format === "zip" ? "完整安全备份" : ({ users: "用户数据", entries: "番剧记录", columns: "专栏数据", audit: "审计日志" })[kind];
    setExportConfirmation({
      reason: "",
      title: `确认导出${label}？`,
      message: format === "zip"
        ? "将生成 ZIP 备份文件，包含当前可备份的站点数据，但不包含用户密码与系统密钥。请妥善保管导出的文件。"
        : `将把${label}导出为 CSV 文件。文件可能包含用户或运营数据，请确认当前环境安全并妥善保管。`,
      confirmLabel: "确认导出",
      onConfirm: () => exportData(format, kind),
    });
  };
  const begin2fa = async () => {
    try {
      const { data } = await api.post("staff/security/two-factor/", {
        action: "begin",
        password: twoFactorForm.beginPassword,
        current_code: twoFactorForm.currentCode,
      });
      setSetup(data);
      setSetupExpiresAt(Date.now() + Number(data.expires_in || 600) * 1000);
      setRecoveryCodes([]);
      update2fa("confirmCode", "");
    }
    catch (error) { onError(readableApiError(error)); }
  };
  const confirm2fa = async () => {
    try {
      const { data } = await api.post("staff/security/two-factor/", { action: "confirm", code: twoFactorForm.confirmCode });
      acceptRotatedSession(data);
      setRecoveryCodes(data.recovery_codes || []);
      setSetup(null);
      setSetupExpiresAt(0);
      setTwoFactorForm((current) => ({ ...current, beginPassword: "", currentCode: "", confirmCode: "" }));
      onNotice(data.detail);
      await loadSecurity();
    }
    catch (error) { onError(readableApiError(error)); }
  };
  const disable2fa = async () => {
    try {
      const { data } = await api.post("staff/security/two-factor/", {
        action: "disable",
        password: twoFactorForm.disablePassword,
        code: twoFactorForm.disableCode,
        recovery_code: twoFactorForm.recoveryCode,
      });
      acceptRotatedSession(data);
      setRecoveryCodes([]);
      setTwoFactorForm((current) => ({ ...current, disablePassword: "", disableCode: "", recoveryCode: "" }));
      onNotice(data.detail);
      await loadSecurity();
    }
    catch (error) { onError(readableApiError(error)); }
  };
  const regenerateRecoveryCodes = async () => {
    try {
      const { data } = await api.post("staff/security/two-factor/", {
        action: "regenerate",
        password: twoFactorForm.regeneratePassword,
        code: twoFactorForm.regenerateCode,
      });
      acceptRotatedSession(data);
      setRecoveryCodes(data.recovery_codes || []);
      setTwoFactorForm((current) => ({ ...current, regeneratePassword: "", regenerateCode: "" }));
      onNotice(data.detail);
      await loadSecurity();
    } catch (error) { onError(readableApiError(error)); }
  };

  return <div className="admin-operations-grid">
    <section className="admin-panel admin-operations-card">
      <header><div><span>SYSTEM HEALTH</span><h3>服务健康检查</h3></div>{canSystem && <button type="button" onClick={loadHealth}><Icon name="reset" /> 重新检查</button>}</header>
      {!canSystem ? <div className="admin-empty-state"><Icon name="lock" /><span>当前角色没有系统运维权限</span></div> : healthLoading ? <div className="admin-dashboard-loading"><span /><span /><span /> 正在探测服务</div> : <div className="admin-health-list">{health?.services?.map((service) => <article key={service.key} className={`is-${service.status}`}><i /><div><strong>{service.label}</strong><small>{service.detail}</small></div><b>{service.status === "healthy" ? "正常" : service.status === "warning" ? "需配置" : "异常"}</b></article>)}</div>}
    </section>

    <section className="admin-panel admin-operations-card">
      <header><div><span>BACKUP / EXPORT</span><h3>安全备份与数据导出</h3></div></header>
      {!canBackup ? <div className="admin-empty-state"><Icon name="lock" /><span>当前角色没有备份权限</span></div> : <div className="admin-backup-actions"><button type="button" className="is-primary" onClick={() => askExport("zip")}><Icon name="export" /><span><b>导出完整安全备份</b><small>ZIP / 不包含密码与密钥</small></span></button>{["users", "entries", "columns", "audit"].map((kind) => <button type="button" key={kind} onClick={() => askExport("csv", kind)}><Icon name="table" /><span><b>导出 {({ users: "用户", entries: "番剧记录", columns: "专栏", audit: "审计日志" })[kind]} CSV</b><small>确认后生成表格文件</small></span></button>)}</div>}
    </section>

    <section className="admin-panel admin-operations-card admin-2fa-card">
      <header><div><span>ACCOUNT SECURITY</span><h3>当前管理员两步验证</h3></div><span className={`admin-status ${security?.enabled ? "is-approved" : "is-pending"}`}>{security?.enabled ? "已启用" : "未启用"}</span></header>
      {!setup && <div className="admin-2fa-begin">
        <p>{security?.enabled ? "重新绑定前需验证当前密码和旧身份验证器动态码，确认新设备前旧设备仍然有效。" : "先验证当前密码，再使用 Google Authenticator、Microsoft Authenticator、1Password 或其他 TOTP 应用扫码。系统将生成 6 枚一次性恢复码，每枚恢复码只能使用一次。"}</p>
        <input type="password" autoComplete="current-password" value={twoFactorForm.beginPassword} onChange={(event) => update2fa("beginPassword", event.target.value)} placeholder="当前密码" />
        {security?.enabled && <input inputMode="numeric" autoComplete="one-time-code" maxLength="6" value={twoFactorForm.currentCode} onChange={(event) => update2fa("currentCode", numericCode(event.target.value))} placeholder="旧身份验证器 6 位验证码" />}
        <button type="button" className="admin-2fa-start" disabled={!twoFactorForm.beginPassword || (security?.enabled && twoFactorForm.currentCode.length !== 6)} onClick={begin2fa}><Icon name="shield" /> {security?.enabled ? "重新绑定身份验证器" : "生成扫码二维码"}</button>
      </div>}
      {setup && <div className="admin-2fa-setup">
        <div className="admin-2fa-qr"><QRCodeSVG value={setup.otpauth_uri} size={176} level="M" marginSize={2} /><span>使用身份验证器扫描二维码</span></div>
        <div className="admin-2fa-verify">
          <p>扫码后输入应用生成的 6 位验证码。二维码将在 <b>{secondsRemaining}</b> 秒后过期。</p>
          <details><summary>无法扫码？手动输入密钥</summary><code>{setup.secret}</code></details>
          <label><span>6 位验证码</span><input inputMode="numeric" autoComplete="one-time-code" maxLength="6" value={twoFactorForm.confirmCode} onChange={(event) => update2fa("confirmCode", numericCode(event.target.value))} /></label>
          <div className="admin-2fa-actions"><button type="button" disabled={secondsRemaining === 0 || twoFactorForm.confirmCode.length !== 6} onClick={confirm2fa}>验证并启用</button><button type="button" className="is-secondary" onClick={begin2fa}>{secondsRemaining === 0 ? "二维码已过期，重新生成" : "重新生成二维码"}</button></div>
        </div>
      </div>}
      {recoveryCodes.length > 0 && <div className="admin-2fa-recovery" role="status"><strong>恢复码只显示一次，请保存在安全的位置。系统固定生成 6 枚。</strong><div>{recoveryCodes.map((item) => <code key={item}>{item}</code>)}</div></div>}
      {security?.enabled && !setup && <div className="admin-2fa-security-actions">
        <section><h4>重新生成恢复码</h4><p>验证成功后，旧恢复码立即全部失效。</p><input type="password" autoComplete="current-password" value={twoFactorForm.regeneratePassword} onChange={(event) => update2fa("regeneratePassword", event.target.value)} placeholder="当前密码" /><input inputMode="numeric" autoComplete="one-time-code" maxLength="6" value={twoFactorForm.regenerateCode} onChange={(event) => update2fa("regenerateCode", numericCode(event.target.value))} placeholder="6 位动态验证码" /><button type="button" disabled={!twoFactorForm.regeneratePassword || twoFactorForm.regenerateCode.length !== 6} onClick={regenerateRecoveryCodes}>生成新的恢复码</button></section>
        <section className="admin-2fa-disable"><h4>关闭两步验证</h4><p>输入当前密码，并使用动态验证码或一枚恢复码。</p><input type="password" autoComplete="current-password" value={twoFactorForm.disablePassword} onChange={(event) => update2fa("disablePassword", event.target.value)} placeholder="当前密码" /><input inputMode="numeric" autoComplete="one-time-code" maxLength="6" value={twoFactorForm.disableCode} onChange={(event) => update2fa("disableCode", numericCode(event.target.value))} placeholder="6 位动态验证码（任选）" /><input value={twoFactorForm.recoveryCode} onChange={(event) => update2fa("recoveryCode", event.target.value.toUpperCase())} placeholder="恢复码（任选）" /><button type="button" disabled={!twoFactorForm.disablePassword || (twoFactorForm.disableCode.length !== 6 && !twoFactorForm.recoveryCode.trim())} onClick={disable2fa}>关闭两步验证</button></section>
      </div>}
    </section>
    {exportConfirmation && <AdminConfirmDialog value={exportConfirmation} onChange={setExportConfirmation} onClose={() => setExportConfirmation(null)} />}
  </div>;
}
