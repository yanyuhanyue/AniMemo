import { useEffect, useMemo, useState } from "react";

import { api, readableApiError } from "../../lib/api.js";
import { Icon } from "../Icon.jsx";


const DECIMAL_GB = 1_000_000_000;
const GIB = 1_073_741_824;
const EMPTY_FORM = {
  id: null,
  slug: "",
  name: "",
  backend_type: "cloudflare_r2",
  enabled: true,
  accept_new_writes: true,
  priority: 100,
  warning_gb: 8,
  write_limit_gb: 9,
  bucket_name: "",
  endpoint_url: "",
  public_base_url: "",
  region: "auto",
  cloudflare_account_id: "",
  account_name: "",
  account_warning_gb: "",
  account_write_limit_gb: "",
  access_key_id: "",
  secret_access_key: "",
  analytics_token: "",
  local_root: "",
  local_public_base_url: "",
  min_free_warning_gb: 15,
  min_free_block_gb: 10,
};

function unitFor(type) {
  return type === "local" ? GIB : DECIMAL_GB;
}

function bytesLabel(value, unit = DECIMAL_GB, suffix = "GB") {
  if (value == null) return "无法获取用量";
  return `${(Number(value) / unit).toFixed(2)} ${suffix}`;
}

const STORAGE_STATE_LABELS = {
  ONLINE: "在线",
  OFFLINE: "离线",
  WRITE_BLOCKED: "已停止新写入",
  DEGRADED: "状态异常 / 降级",
};

function storageStateLabel(status) {
  const normalized = String(status || "OFFLINE").toUpperCase();
  return STORAGE_STATE_LABELS[normalized] || "状态未知";
}

function toForm(item) {
  return {
    ...EMPTY_FORM,
    ...item,
    warning_gb: Number(item.warning_bytes || 0) / unitFor(item.backend_type),
    write_limit_gb: Number(item.write_limit_bytes || 0) / unitFor(item.backend_type),
    min_free_warning_gb: Number(item.min_free_warning_bytes || 0) / GIB,
    min_free_block_gb: Number(item.min_free_block_bytes || 0) / GIB,
    access_key_id: "",
    secret_access_key: "",
    analytics_token: "",
    account_name: item.account?.name || "",
    account_warning_gb: item.account?.warning_bytes == null ? "" : Number(item.account.warning_bytes) / DECIMAL_GB,
    account_write_limit_gb: item.account?.write_limit_bytes == null ? "" : Number(item.account.write_limit_bytes) / DECIMAL_GB,
  };
}

export function AdminMediaStoragePanel({ viewer, onNotice, onError }) {
  const [payload, setPayload] = useState({ preferred_write_backend_id: null, effective_write_backend_id: null, results: [] });
  const [form, setForm] = useState(EMPTY_FORM);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [busy, setBusy] = useState(null);
  const isSuperuser = Boolean(viewer?.is_superuser);

  const load = async () => {
    if (!isSuperuser) return;
    setLoading(true);
    try { setPayload((await api.get("staff/system/media-storage/")).data); }
    catch (error) { onError(readableApiError(error, "媒体存储加载失败。")); }
    finally { setLoading(false); }
  };

  useEffect(() => { void load(); }, [isSuperuser]);
  const preferred = useMemo(() => payload.results.find((item) => item.id === payload.preferred_write_backend_id), [payload]);
  const effective = useMemo(() => payload.results.find((item) => item.id === payload.effective_write_backend_id), [payload]);
  const update = (field, value) => setForm((current) => ({ ...current, [field]: value }));

  const save = async (event) => {
    event.preventDefault();
    setSaving(true);
    const body = {
      slug: form.slug,
      name: form.name,
      backend_type: form.backend_type,
      enabled: form.enabled,
      accept_new_writes: form.accept_new_writes,
      priority: Number(form.priority),
      warning_bytes: Math.round(Number(form.warning_gb) * unitFor(form.backend_type)),
      write_limit_bytes: Math.round(Number(form.write_limit_gb) * unitFor(form.backend_type)),
      bucket_name: form.bucket_name,
      endpoint_url: form.endpoint_url,
      public_base_url: form.public_base_url,
      region: form.region || "auto",
      cloudflare_account_id: form.cloudflare_account_id,
      account_name: form.account_name,
      account_warning_bytes: form.account_warning_gb === "" ? null : Math.round(Number(form.account_warning_gb) * DECIMAL_GB),
      account_write_limit_bytes: form.account_write_limit_gb === "" ? null : Math.round(Number(form.account_write_limit_gb) * DECIMAL_GB),
      local_root: form.local_root,
      local_public_base_url: form.local_public_base_url,
      min_free_warning_bytes: Math.round(Number(form.min_free_warning_gb) * GIB),
      min_free_block_bytes: Math.round(Number(form.min_free_block_gb) * GIB),
    };
    if (form.access_key_id.trim()) body.access_key_id = form.access_key_id.trim();
    if (form.secret_access_key.trim()) body.secret_access_key = form.secret_access_key.trim();
    if (form.analytics_token.trim()) body.analytics_token = form.analytics_token.trim();
    try {
      if (form.id) await api.patch(`staff/system/media-storage/${form.id}/`, body);
      else await api.post("staff/system/media-storage/", body);
      setForm(EMPTY_FORM);
      await load();
      onNotice(form.id ? "存储配置已更新，无需重启" : "媒体存储已创建");
    } catch (error) { onError(readableApiError(error, "媒体存储保存失败。")); }
    finally { setSaving(false); }
  };

  const action = async (item, actionName, extra = {}) => {
    setBusy(`${item.id}:${actionName}`);
    try {
      const { data } = await api.post(`staff/system/media-storage/${item.id}/actions/`, { action: actionName, ...extra });
      if (data?.results) setPayload(data);
      else await load();
      onNotice(data?.detail || "存储操作已完成");
    } catch (error) { onError(readableApiError(error, "存储操作失败。")); }
    finally { setBusy(null); }
  };

  const remove = async (item) => {
    if (!window.confirm(`确认删除存储“${item.name}”？仍有媒体引用时服务器会拒绝删除。`)) return;
    try {
      await api.delete(`staff/system/media-storage/${item.id}/`);
      await load();
      onNotice("存储配置已删除");
    } catch (error) { onError(readableApiError(error, "存储删除失败。")); }
  };

  const clearCredentials = async (item) => {
    if (!window.confirm(`确认清除“${item.name}”的全部 R2 凭证？该存储会立即停止新写入。`)) return;
    await action(item, "clear-credentials", { fields: ["access_key_id", "secret_access_key", "analytics_token"] });
  };

  if (!isSuperuser) return <div className="admin-empty-state"><Icon name="lock" /><span>媒体存储配置仅超级管理员可见</span></div>;
  if (loading) return <div className="admin-dashboard-loading"><span /><span /><span /> 正在读取媒体存储池</div>;

  return <div className="admin-storage-page">
    <section className="admin-panel admin-storage-summary">
       <header><div><span>媒体存储池</span><h3>媒体存储</h3></div><button type="button" onClick={load}><Icon name="reset" /> 刷新状态</button></header>
      <div className="admin-storage-summary__body"><span><small>首选存储</small><strong>{preferred?.name || "尚未选择"}</strong></span><span><small>下次可写</small><strong>{effective?.name || "当前无可写存储"}</strong></span><span><small>可写</small><strong>{payload.results.filter((item) => item.state?.writable).length}</strong></span></div>
    </section>

    <div className="admin-storage-grid">
       {payload.results.map((item) => <article className="admin-panel admin-storage-card" key={item.id}>
         <header><div><span>{item.backend_type === "cloudflare_r2" ? "Cloudflare R2" : "VPS 本地存储"}</span><h3>{item.name}</h3></div><div className="admin-storage-card__roles">{item.is_effective && <b className="admin-status is-effective">当前写入</b>}{item.is_preferred && <b className="admin-status is-preferred">首选</b>}<b className={`admin-status is-${String(item.state?.status || "offline").toLowerCase()}`}>{storageStateLabel(item.state?.status)}</b></div></header>
         <dl><div><dt>优先级</dt><dd>{item.priority}</dd></div><div><dt>已管理容量</dt><dd>{bytesLabel(item.usage?.managed_bytes, unitFor(item.backend_type), item.backend_type === "local" ? "GiB" : "GB")}</dd></div>{item.backend_type === "cloudflare_r2" && <><div><dt>R2 实际容量</dt><dd>{bytesLabel(item.usage?.actual_bytes, DECIMAL_GB, "GB")}</dd></div><div><dt>未纳管容量</dt><dd>{bytesLabel(item.usage?.untracked_bytes, DECIMAL_GB, "GB")}</dd></div>{item.account && <><div><dt>Cloudflare 账户</dt><dd>{item.account.name}</dd></div><div><dt>账户容量</dt><dd>{bytesLabel(item.account.effective_bytes, DECIMAL_GB, "GB")}{item.account.write_limit_bytes == null ? " / 无限制" : ` / ${bytesLabel(item.account.write_limit_bytes, DECIMAL_GB, "GB")}`}</dd></div></>}</>}{item.backend_type === "local" && <div><dt>磁盘剩余空间</dt><dd>{bytesLabel(item.state?.disk_free_bytes, GIB, "GiB")}</dd></div>}<div><dt>媒体对象数</dt><dd>{item.media_object_count}</dd></div></dl>
         {item.backend_type === "cloudflare_r2" && <div className="admin-storage-credentials"><span>访问密钥 ID <b>{item.access_key_configured ? "已配置" : "未配置"}</b></span><span>访问密钥 <b>{item.secret_key_configured ? "已配置" : "未配置"}</b></span><span>Analytics 令牌 <b>{item.analytics_token_configured ? "已配置" : "未配置"}</b></span></div>}
        <div className="admin-row-actions">
          <button type="button" onClick={() => setForm(toForm(item))}><Icon name="edit" /> 编辑</button>
          <button type="button" disabled={!item.state?.writable || busy} onClick={() => action(item, "set-active")}><Icon name="upload" /> 设为当前写入</button>
          <button type="button" disabled={busy} onClick={() => action(item, "test-connection")}><Icon name="bolt" /> 测试连接</button>
          {item.backend_type === "cloudflare_r2" && <button type="button" disabled={busy} onClick={() => action(item, "refresh-usage")}><Icon name="chart" /> 刷新容量</button>}
          <button type="button" disabled={busy} onClick={() => action(item, "toggle-writes", { accept_new_writes: !item.accept_new_writes })}><Icon name={item.accept_new_writes ? "eye-slash" : "eye"} /> {item.accept_new_writes ? "停止新写入" : "恢复新写入"}</button>
          {item.backend_type === "cloudflare_r2" && <button type="button" className="is-reject" onClick={() => clearCredentials(item)}><Icon name="key" /> 清除凭证</button>}
          <button type="button" className="is-reject" onClick={() => remove(item)}><Icon name="trash" /> 删除</button>
        </div>
      </article>)}
    </div>

    <section className="admin-panel admin-storage-editor">
       <header><div><span>存储配置</span><h3>{form.id ? `编辑 ${form.name}` : "新增媒体存储"}</h3></div>{form.id && <button type="button" onClick={() => setForm(EMPTY_FORM)}><Icon name="plus" /> 新建</button>}</header>
       <form onSubmit={save}>
         <section className="admin-storage-form-section"><div className="admin-storage-form-section__heading"><h4>基础设置</h4><p>决定存储类型、优先级与容量保护规则。</p></div><div className="admin-storage-fields">
           <label><span>存储类型</span><select disabled={Boolean(form.id)} value={form.backend_type} onChange={(event) => update("backend_type", event.target.value)}><option value="cloudflare_r2">Cloudflare R2</option><option value="local">VPS 本地存储</option></select><small className="admin-storage-help">已创建的存储类型不可直接修改。</small></label>
           <label><span>存储名称 <em>必填</em></span><input required maxLength="120" value={form.name} placeholder="例如 主 R2 存储" onChange={(event) => update("name", event.target.value)} /></label>
           <label><span>唯一标识（Slug） <em>必填</em></span><input required maxLength="64" value={form.slug} placeholder="例如 primary-r2" onChange={(event) => update("slug", event.target.value)} /></label>
           <label><span>优先级</span><input type="number" min="0" value={form.priority} onChange={(event) => update("priority", event.target.value)} /><small className="admin-storage-help">数字越小优先级越高。</small></label>
           <label><span>容量预警（{form.backend_type === "local" ? "GiB" : "GB"}）</span><input type="number" min="0.01" step="0.01" value={form.warning_gb} onChange={(event) => update("warning_gb", event.target.value)} /><small className="admin-storage-help">达到该容量后显示预警，不立即停止写入。</small></label>
           <label><span>写入上限（{form.backend_type === "local" ? "GiB" : "GB"}）</span><input type="number" min="0.02" step="0.01" value={form.write_limit_gb} onChange={(event) => update("write_limit_gb", event.target.value)} /><small className="admin-storage-help">达到上限后停止向该存储写入新文件，仍允许读取和删除。</small></label>
         </div></section>
          {form.backend_type === "cloudflare_r2" ? <>
            <section className="admin-storage-form-section"><div className="admin-storage-form-section__heading"><h4>R2 连接配置</h4><p>填写 Cloudflare R2 的 S3 连接和公开访问信息。</p></div><div className="admin-storage-fields">
              <label><span>R2 存储桶名称（Bucket） <em>必填</em></span><input required maxLength="63" value={form.bucket_name} placeholder="例如 anime-journal-media" onChange={(event) => update("bucket_name", event.target.value)} /></label>
              <label><span>R2 接口地址（Endpoint） <em>必填</em></span><input required type="url" maxLength="300" value={form.endpoint_url} placeholder="https://&lt;ACCOUNT_ID&gt;.r2.cloudflarestorage.com" onChange={(event) => update("endpoint_url", event.target.value)} /></label>
              <label><span>公共访问地址（Public URL） <em>必填</em></span><input required type="url" maxLength="300" value={form.public_base_url} placeholder="https://media.example.com" onChange={(event) => update("public_base_url", event.target.value)} /><small className="admin-storage-help">媒体文件对外访问使用的域名或基础 URL。</small></label>
              <label><span>区域（Region）</span><input maxLength="32" value={form.region} placeholder="Cloudflare R2 通常使用 auto" onChange={(event) => update("region", event.target.value)} /></label>
              <label><span>访问密钥 ID（Access Key ID）</span><input autoComplete="off" maxLength="300" value={form.access_key_id} placeholder={form.access_key_configured ? "已配置，留空表示保持不变" : "填写 Cloudflare R2 Access Key ID"} onChange={(event) => update("access_key_id", event.target.value)} /></label>
              <label><span>访问密钥（Secret Access Key）</span><input type="password" autoComplete="new-password" maxLength="500" value={form.secret_access_key} placeholder={form.secret_key_configured ? "已配置，留空表示保持不变" : "填写 Cloudflare R2 Secret Access Key"} onChange={(event) => update("secret_access_key", event.target.value)} /><small className="admin-storage-help">已配置的密钥不会回显，填写新值才会替换。</small></label>
            </div></section>
            <section className="admin-storage-form-section"><div className="admin-storage-form-section__heading"><h4>Cloudflare 账户与容量统计</h4><p>用于账户级容量限制和 R2 实际用量查询。</p></div><div className="admin-storage-fields">
              <label><span>Cloudflare 账户 ID</span><input maxLength="64" value={form.cloudflare_account_id} placeholder="填写 Cloudflare Account ID" onChange={(event) => update("cloudflare_account_id", event.target.value)} /></label>
              <label><span>账户名称</span><input maxLength="120" value={form.account_name} placeholder="例如主 Cloudflare 账户" onChange={(event) => update("account_name", event.target.value)} /></label>
              <label><span>账户容量预警（GB） <em>可选</em></span><input type="number" min="0.01" step="0.01" value={form.account_warning_gb} placeholder="留空表示不启用账户级容量预警" onChange={(event) => update("account_warning_gb", event.target.value)} /></label>
              <label><span>账户写入上限（GB） <em>可选</em></span><input type="number" min="0.02" step="0.01" value={form.account_write_limit_gb} placeholder="留空表示不启用账户级写入限制" onChange={(event) => update("account_write_limit_gb", event.target.value)} /></label>
              <label><span>Analytics API 令牌</span><input type="password" autoComplete="new-password" maxLength="500" value={form.analytics_token} placeholder={form.analytics_token_configured ? "已配置，留空表示保持不变" : "填写 Analytics API Token"} onChange={(event) => update("analytics_token", event.target.value)} /><small className="admin-storage-help">读取 Cloudflare R2 实际容量，需要 Account Analytics: Read 权限。</small></label>
            </div></section>
          </> : <section className="admin-storage-form-section"><div className="admin-storage-form-section__heading"><h4>VPS 本地存储</h4><p>配置本机媒体目录和磁盘空间保护阈值。</p></div><div className="admin-storage-fields">
            <label><span>存储子路径</span><input maxLength="240" value={form.local_root} placeholder="例如 secondary" onChange={(event) => update("local_root", event.target.value)} /><small className="admin-storage-help">相对于服务器固定媒体根目录的子路径。</small></label>
            <label><span>本地媒体公共访问地址（Base URL） <em>必填</em></span><input required type="url" maxLength="300" value={form.local_public_base_url} placeholder="https://media.example.com/local-media" onChange={(event) => update("local_public_base_url", event.target.value)} /></label>
            <label><span>磁盘剩余空间预警（GiB）</span><input type="number" min="0.01" step="0.01" value={form.min_free_warning_gb} onChange={(event) => update("min_free_warning_gb", event.target.value)} /></label>
            <label><span>磁盘剩余空间写入阻断（GiB）</span><input type="number" min="0" step="0.01" value={form.min_free_block_gb} onChange={(event) => update("min_free_block_gb", event.target.value)} /><small className="admin-storage-help">剩余空间低于该值时停止新媒体写入。</small></label>
            <p className="admin-storage-note">请为 PostgreSQL、Docker、Redis、日志和系统更新保留足够磁盘空间。</p>
          </div></section>}
        <button type="submit" disabled={saving}><Icon name="save" /> {saving ? "正在保存..." : "保存存储配置"}</button>
      </form>
    </section>
  </div>;
}
