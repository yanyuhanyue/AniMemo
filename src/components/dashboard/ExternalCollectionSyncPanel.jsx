import { useEffect, useMemo, useState } from "react";

import { Icon } from "../Icon.jsx";
import { api, readableApiError } from "../../lib/api.js";
import {
  SYNC_FIELD_LABELS,
  SYNC_STATE_LABELS,
  syncEntryPatch,
  syncUiActions,
  syncValueLabel,
} from "../../lib/externalSync.js";

const PROVIDER = "bangumi";

function dateTimeLabel(value) {
  if (!value) return "尚未建立同步基线";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "时间未知" : date.toLocaleString("zh-CN", { hour12: false });
}

function errorCode(error) {
  return String(error?.response?.data?.code || "");
}

function syncRequestError(error, fallback) {
  const code = errorCode(error);
  if (code === "provider_unavailable") return "Bangumi 暂时不可用，请稍后重新比较。";
  if (code === "provider_invalid_response") return "Bangumi 返回了无法识别的数据，请稍后重试。";
  return readableApiError(error, fallback);
}

function unavailableReason(field) {
  if (field.state === "remote_missing") return "Bangumi 尚未收藏此条目，当前阶段无法拉取。";
  if (field.pull_block_reason === "remote_value_not_representable") {
    return "AniMemo 当前无法无损表示 Bangumi 的此字段值。";
  }
  return "此字段当前无法安全拉取。";
}

function SyncField({ field, selected, disabled, onSelect }) {
  const actions = syncUiActions(field);
  const canPull = actions.includes("pull_remote");
  const canAccept = actions.includes("accept_equal");
  const baseline = field.baseline
    ? syncValueLabel(field.field, field.baseline)
    : "尚无同步基线";
  return (
    <article className={`external-sync-field is-${field.state}`}>
      <header>
        <strong>{SYNC_FIELD_LABELS[field.field] || field.field}</strong>
        <span>{SYNC_STATE_LABELS[field.state] || "状态未知"}</span>
      </header>
      <div className="external-sync-field__values">
        <div><small>AniMemo 当前值</small><p>{syncValueLabel(field.field, field.local)}</p></div>
        <div><small>Bangumi 当前值</small><p>{syncValueLabel(field.field, field.remote)}</p></div>
        <div><small>上次确认一致</small><p>{baseline}</p></div>
      </div>
      {field.state === "unsupported" || field.state === "remote_missing" ? (
        <p className="external-sync-field__reason"><Icon name="warning" /> {unavailableReason(field)}</p>
      ) : actions.length ? (
        <div className="external-sync-field__actions" role="group" aria-label={`${SYNC_FIELD_LABELS[field.field]}同步选择`}>
          {canPull && <button type="button" className={selected === "pull_remote" ? "is-selected" : ""} aria-pressed={selected === "pull_remote"} onClick={() => onSelect("pull_remote")} disabled={disabled}><Icon name="arrow-down" /> 使用 Bangumi</button>}
          {canAccept && <button type="button" className={selected === "accept_equal" ? "is-selected" : ""} aria-pressed={selected === "accept_equal"} onClick={() => onSelect("accept_equal")} disabled={disabled}><Icon name="circle-check" /> 确认当前一致</button>}
          <button type="button" className={!selected ? "is-kept" : ""} aria-pressed={!selected} onClick={() => onSelect("")} disabled={disabled}><Icon name="shield" /> {canPull ? "保留 AniMemo" : "不处理"}</button>
        </div>
      ) : null}
    </article>
  );
}

export function ExternalCollectionSyncPanel({ entryId, identityId, onEntryRefresh, isDemo = false }) {
  const [provider, setProvider] = useState(null);
  const [availabilityError, setAvailabilityError] = useState("");
  const [opened, setOpened] = useState(false);
  const [preview, setPreview] = useState(null);
  const [selected, setSelected] = useState({});
  const [loading, setLoading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    let active = true;
    setProvider(null);
    setAvailabilityError("");
    setOpened(false);
    setPreview(null);
    setSelected({});
    if (isDemo || !entryId || !identityId) return () => { active = false; };
    api.get("external-accounts/")
      .then(({ data }) => {
        if (active) setProvider(data?.providers?.find((item) => item.provider === PROVIDER) || null);
      })
      .catch((requestError) => {
        if (active) setAvailabilityError(syncRequestError(requestError, "Bangumi 同步能力读取失败。"));
      });
    return () => { active = false; };
  }, [entryId, identityId, isDemo]);

  const available = Boolean(
    provider?.connection?.status === "connected"
    && provider?.collection_sync_preview_available
    && provider?.collection_sync_pull_available
    && provider?.collection_sync_apply_available
    && !provider?.collection_sync_push_available
    && !provider?.collection_write_implemented
  );

  const refreshPreview = async ({ keepNotice = false } = {}) => {
    setLoading(true);
    setError("");
    setPreview(null);
    setSelected({});
    if (!keepNotice) setNotice("");
    try {
      const { data } = await api.get(`external-sync/providers/${PROVIDER}/entries/${entryId}/preview/`);
      setPreview(data);
      return data;
    } catch (requestError) {
      const code = errorCode(requestError);
      if (code === "external_account_needs_reauthorization") {
        setError("Bangumi 连接需要重新授权，请先到账号设置完成授权。");
      } else {
        setError(syncRequestError(requestError, "Bangumi 收藏暂时无法读取。"));
      }
      return null;
    } finally {
      setLoading(false);
    }
  };

  const openComparison = async () => {
    setOpened(true);
    await refreshPreview();
  };

  const updateSelection = (field, action) => {
    setSelected((current) => {
      const next = { ...current };
      if (action) next[field] = action;
      else delete next[field];
      return next;
    });
  };

  const actions = useMemo(() => Object.entries(selected).map(([field, action]) => ({ field, action })), [selected]);

  const applySelection = async () => {
    if (!preview?.preview_token || !actions.length || applying) return;
    setApplying(true);
    setError("");
    setNotice("");
    try {
      const { data } = await api.post(
        `external-sync/providers/${PROVIDER}/entries/${entryId}/apply/`,
        { preview_token: preview.preview_token, actions },
      );
      const entryResponse = await api.get(`entries/${entryId}/`);
      const entry = entryResponse.data || {};
      onEntryRefresh?.({
        entryPatch: syncEntryPatch(entry),
        externalIdentities: Array.isArray(entry.external_identities) ? entry.external_identities : undefined,
      });
      const count = Array.isArray(data?.baseline_advanced_fields) ? data.baseline_advanced_fields.length : actions.length;
      setNotice(`已确认 ${count} 个字段，Bangumi 未被修改。`);
      await refreshPreview({ keepNotice: true });
    } catch (requestError) {
      const code = errorCode(requestError);
      if (code === "sync_preview_stale") {
        const refreshed = await refreshPreview();
        if (refreshed) setError("数据已发生变化，请重新确认。");
      } else if (code === "sync_context_changed") {
        setError("作品绑定或账号连接已变化，请重新打开同步比较。");
      } else if (code === "external_account_needs_reauthorization") {
        setError("Bangumi 连接需要重新授权，请先到账号设置完成授权。");
      } else if (code === "sync_preview_expired" || code === "sync_preview_invalid") {
        const refreshed = await refreshPreview();
        if (refreshed) setError("同步确认已失效，请检查最新数据后重新选择。");
      } else {
        setError(syncRequestError(requestError, "所选字段未能应用，AniMemo 数据保持不变。"));
      }
    } finally {
      setApplying(false);
    }
  };

  if (availabilityError) {
    return <p className="external-sync-availability-error" role="alert"><Icon name="warning" /> {availabilityError}</p>;
  }
  if (!available) return null;

  return (
    <section className="external-sync-panel" aria-labelledby="external-sync-title">
      <div className="external-sync-panel__entry">
        <div><strong id="external-sync-title">Bangumi 收藏同步</strong><small>当前只会拉取到 AniMemo，不会修改 Bangumi。</small></div>
        {!opened ? (
          <button type="button" onClick={openComparison} disabled={loading || applying}><Icon name="sliders" /> 比较 Bangumi 收藏</button>
        ) : (
          <button type="button" onClick={() => setOpened(false)} disabled={applying}><Icon name="chevron-up" /> 收起比较</button>
        )}
      </div>

      {opened && <div className="external-sync-panel__body">
        <p className="external-sync-panel__warning"><Icon name="shield" /> Pull-only：不会写入 Bangumi；拉取时只更新 AniMemo 的观看状态、评分或评价。</p>
        {loading && !preview ? <div className="external-sync-panel__loading" role="status"><Icon name="spinner" spin /> 正在读取 Bangumi 收藏...</div> : null}
        {preview?.remote_collection_missing && <p className="external-sync-panel__missing" role="status">Bangumi 尚未收藏此条目，当前阶段无法拉取。</p>}
        {preview && <>
          <div className="external-sync-panel__meta"><span>最近确认：{dateTimeLabel(preview.last_synced_at)}</span><button type="button" onClick={() => refreshPreview()} disabled={loading || applying} title="刷新比较"><Icon name={loading ? "spinner" : "reset"} spin={loading} /> 刷新比较</button></div>
          <div className="external-sync-fields">
            {(preview.fields || []).map((field) => <SyncField key={field.field} field={field} selected={selected[field.field] || ""} disabled={loading || applying} onSelect={(action) => updateSelection(field.field, action)} />)}
          </div>
          <div className="external-sync-panel__confirm">
            <span>{actions.length ? `已选择 ${actions.length} 个字段` : "尚未选择同步字段"}</span>
            <button type="button" onClick={applySelection} disabled={!actions.length || loading || applying}><Icon name={applying ? "spinner" : "check"} spin={applying} /> {applying ? "正在应用" : "应用所选"}</button>
          </div>
        </>}
        {notice && <p className="external-sync-panel__notice" role="status"><Icon name="circle-check" /> {notice}</p>}
        {error && <p className="external-sync-panel__error" role="alert"><Icon name="warning" /> {error}</p>}
      </div>}
    </section>
  );
}
