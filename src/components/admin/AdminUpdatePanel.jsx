import { useCallback, useEffect, useMemo, useState } from "react";

import { api, csrfApi, readableApiError } from "../../lib/api.js";
import { createLiveRefreshController } from "../../lib/liveRefresh.js";
import { Icon } from "../Icon.jsx";
import {
  ACTIVE_UPDATE_STATES,
  channelLabel,
  compatibilityPresentation,
  shortDigest,
  updateStateLabel,
} from "./updatePresentation.js";


function ReleaseIdentity({ value, label }) {
  if (!value) return <div className="admin-update-identity is-empty"><strong>{label}</strong><span>尚未记录</span></div>;
  return <div className="admin-update-identity">
    <strong>{label}</strong>
    <b>{value.version}</b>
    <span>{channelLabel(value.channel)} · {value.commit?.slice(0, 8)}</span>
    <code>API {shortDigest(value.apiDigest)}</code>
    <code>WEB {shortDigest(value.webDigest)}</code>
  </div>;
}

function CompatibilityBadge({ value }) {
  const presentation = compatibilityPresentation(value);
  return <div className={`admin-update-compatibility is-${presentation.tone}`}>
    <b>{presentation.label}</b><small>{presentation.detail}</small>
  </div>;
}

function OperationProgress({ operation }) {
  if (!operation) return <div className="admin-update-empty">当前没有更新操作。</div>;
  return <div className="admin-update-operation" aria-live="polite">
    <header><div><strong>{updateStateLabel(operation.status)}</strong><small>操作 {operation.id?.slice(0, 8)}</small></div><span className={`is-${operation.status}`}>{operation.status}</span></header>
    <ol>{(operation.events || []).map((event, index) => <li key={`${event.at}-${index}`} className={index === operation.events.length - 1 ? "is-current" : ""}>
      <i><Icon name={index === operation.events.length - 1 && ACTIVE_UPDATE_STATES.has(operation.status) ? "spinner" : "check"} spin={index === operation.events.length - 1 && ACTIVE_UPDATE_STATES.has(operation.status)} /></i>
      <div><b>{updateStateLabel(event.status)}</b><small>{event.detail || "状态已记录"}</small></div>
      <time>{event.at ? new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(event.at)) : "—"}</time>
    </li>)}</ol>
  </div>;
}

export function AdminUpdatePanel({ viewer, onNotice, onError }) {
  const [status, setStatus] = useState(null);
  const [channel, setChannel] = useState("stable");
  const [releases, setReleases] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [plan, setPlan] = useState(null);
  const [confirmation, setConfirmation] = useState("");
  const [rollbackConfirmation, setRollbackConfirmation] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const operation = status?.operation;
  const operationActive = ACTIVE_UPDATE_STATES.has(operation?.status);
  const previousCompatibility = status?.previousCompatibility;

  const loadStatus = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setLoading(true);
    try { setStatus((await api.get("staff/system/updates/status/")).data); }
    catch (error) { onError(readableApiError(error, "更新状态读取失败。")); }
    finally { if (!silent) setLoading(false); }
  }, [onError]);

  const loadReleases = useCallback(async ({ refresh = false } = {}) => {
    setRefreshing(true);
    try {
      const { data } = await api.get("staff/system/updates/releases/", { params: { channel, refresh } });
      setReleases(data.releases || []);
    } catch (error) { onError(readableApiError(error, "发布版本读取失败。")); }
    finally { setRefreshing(false); }
  }, [channel, onError]);

  useEffect(() => { void Promise.all([loadStatus(), loadReleases()]); }, [loadReleases, loadStatus]);
  useEffect(() => {
    if (!operationActive || !operation?.id) return undefined;
    const liveRefresh = createLiveRefreshController({
      intervalMs: 2500,
      refresh: async () => {
        try {
          const { data } = await api.get(`staff/system/updates/operations/${operation.id}/`);
          setStatus((current) => ({ ...current, operation: data }));
          if (!ACTIVE_UPDATE_STATES.has(data.status)) await loadStatus({ silent: true });
        } catch (error) { onError(readableApiError(error, "更新进度读取失败。")); }
      },
    });
    return () => liveRefresh.dispose();
  }, [loadStatus, onError, operation?.id, operationActive]);

  const selectedPlanVersion = plan?.to?.version || "";
  const currentVersion = status?.current?.version;
  const available = useMemo(() => releases.filter((item) => item.version !== currentVersion), [currentVersion, releases]);

  const createPlan = async (version) => {
    setSubmitting(true); setPlan(null); setConfirmation("");
    try { setPlan((await csrfApi.post("staff/system/updates/plan/", { version })).data); }
    catch (error) { onError(readableApiError(error, "更新计划生成失败。")); }
    finally { setSubmitting(false); }
  };

  const applyPlan = async () => {
    setSubmitting(true);
    try {
      const { data } = await csrfApi.post("staff/system/updates/apply/", { plan_id: plan.planId, confirmation });
      setStatus((current) => ({ ...current, operation: data.operation }));
      setPlan(null); setConfirmation(""); onNotice("更新操作已交给 AniMemo Update Agent");
    } catch (error) { onError(readableApiError(error, "更新启动失败。")); }
    finally { setSubmitting(false); }
  };

  const rollback = async () => {
    setSubmitting(true);
    try {
      const { data } = await csrfApi.post("staff/system/updates/rollback/", { confirmation: rollbackConfirmation });
      setStatus((current) => ({ ...current, operation: data.operation }));
      setRollbackConfirmation(""); onNotice("应用层回退已启动，数据库不会自动回退");
    } catch (error) { onError(readableApiError(error, "回退启动失败。")); }
    finally { setSubmitting(false); }
  };

  if (loading) return <div className="admin-dashboard-loading"><span /><span /><span /> 正在读取 Update Agent</div>;
  return <div className="admin-update-page">
    <section className="admin-panel admin-update-summary">
      <header><div><h3>当前发布身份</h3><p>运行版本只认 Release Manifest 与 OCI digest。</p></div><button type="button" disabled={refreshing} onClick={() => { void loadStatus(); void loadReleases({ refresh: true }); }}><Icon name="reset" /> {refreshing ? "正在验证..." : "检查更新"}</button></header>
      <div className="admin-update-identities"><ReleaseIdentity label="CURRENT" value={status?.current} /><ReleaseIdentity label="PREVIOUS" value={status?.previous} /></div>
      <div className="admin-update-runtime"><span>Updater <b>{status?.updaterVersion}</b></span><span>DB Contract <b>{status?.runtime?.databaseContract}</b></span><span>Plugin SDK <b>{status?.runtime?.enabledPluginApis?.join(", ") || "—"}</b></span></div>
    </section>

    <section className="admin-panel admin-update-releases">
      <header><div><h3>可用版本</h3><p>计划阶段会验证 Release、checksums、GitHub attestation 与兼容性。</p></div><div className="admin-update-channels" aria-label="发布通道">{["stable", ...(viewer.is_superuser ? ["rc", "beta"] : [])].map((value) => <button type="button" key={value} className={channel === value ? "is-active" : ""} aria-pressed={channel === value} onClick={() => { setChannel(value); setPlan(null); }}>{channelLabel(value)}</button>)}</div></header>
      {channel === "beta" && <div className="admin-update-experimental" role="note"><Icon name="warning" /><div><b>Beta 是开发验证通道</b><span>功能仍可能变化，不是默认生产验收候选。</span></div></div>}
      <div className="admin-update-release-list">{available.length ? available.map((release) => <article key={release.version}>
        <div><b>{release.version}</b><span>{channelLabel(release.channel)} · {release.publishedAt ? new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium" }).format(new Date(release.publishedAt)) : "时间未知"}</span></div>
        <CompatibilityBadge value={release.compatibility} />
        <button type="button" disabled={submitting || operationActive || release.compatibility?.allowed === false} onClick={() => createPlan(release.version)}>查看并确认</button>
      </article>) : <div className="admin-update-empty">当前通道没有比运行版本更新或不同的 Release。</div>}</div>
    </section>

    {plan && <section className="admin-panel admin-update-plan">
      <header><div><h3>更新确认</h3><p>确认对象已绑定到精确 manifest；计划过期后必须重新生成。</p></div><button type="button" onClick={() => { setPlan(null); setConfirmation(""); }}>取消</button></header>
      <div className="admin-update-plan-route"><ReleaseIdentity label="FROM" value={plan.from} /><Icon name="arrow-right" /><ReleaseIdentity label="TO" value={plan.to} /></div>
      <CompatibilityBadge value={plan.compatibility} />
      <dl><div><dt>数据库迁移</dt><dd>{plan.compatibility?.migrationRequired ? `需要 · ${plan.compatibility.migrationPolicy}` : "不需要"}</dd></div><div><dt>数据库自动回退</dt><dd>永不执行</dd></div><div><dt>影响服务</dt><dd>{plan.affectedServices?.join(" / ")}</dd></div></dl>
      <label><span>输入 <b>APPLY {selectedPlanVersion}</b> 确认</span><input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" spellCheck="false" /></label>
      <button className="admin-update-apply" type="button" disabled={submitting || confirmation !== `APPLY ${selectedPlanVersion}`} onClick={applyPlan}><Icon name="bolt" /> {submitting ? "正在提交..." : `更新到 ${selectedPlanVersion}`}</button>
    </section>}

    <section className="admin-panel admin-update-progress"><header><div><h3>真实操作进度</h3><p>每一步都来自 Agent 的持久 operation journal。</p></div></header><OperationProgress operation={operation} /></section>

    <section className="admin-panel admin-update-history">
      <header><div><h3>版本历史与应用回退</h3><p>回退只切换 API / Web；数据库保留当前 schema。</p></div></header>
      <div className="admin-update-history-list">{(status?.history || []).map((item) => <article key={`${item.version}-${item.commit}`}><div><b>{item.version}</b><span>{channelLabel(item.channel)} · {item.commit?.slice(0, 8)}</span></div>{item.version === currentVersion ? <strong className="is-current">当前版本</strong> : <CompatibilityBadge value={item.compatibility} />}</article>)}</div>
      {status?.previous && <div className="admin-update-rollback"><div><b>回退到 PREVIOUS：{status.previous.version}</b><span>仅在兼容性允许时执行，不恢复数据库备份。</span><CompatibilityBadge value={previousCompatibility} /></div><label><span>输入 ROLLBACK PREVIOUS</span><input value={rollbackConfirmation} onChange={(event) => setRollbackConfirmation(event.target.value)} autoComplete="off" spellCheck="false" disabled={previousCompatibility?.allowed === false} /></label><button type="button" disabled={submitting || operationActive || previousCompatibility?.allowed === false || rollbackConfirmation !== "ROLLBACK PREVIOUS"} onClick={rollback}><Icon name="history" /> 回退应用</button></div>}
    </section>
  </div>;
}
