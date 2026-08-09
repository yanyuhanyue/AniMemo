import { useMemo, useRef, useState } from "react";
import { Icon } from "../Icon.jsx";
import { api, readableApiError } from "../../lib/api.js";
import { ExternalCollectionSyncPanel } from "./ExternalCollectionSyncPanel.jsx";
import {
  bangumiIdentityFromResult,
  externalMediaResultFromApi,
  REFRESH_FIELD_LABELS,
  refreshRecordPatch,
  replaceProviderIdentity,
} from "../../lib/externalMedia.js";

const PROVIDER = "bangumi";

function formatFetchedAt(value) {
  if (!value) return "尚未同步";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间未知";
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function providerScore(identity) {
  const score = Number(identity?.provider_score ?? identity?.metadata?.score);
  return Number.isFinite(score) && score > 0 ? score.toFixed(1) : "暂无";
}

export function ExternalMediaIdentityPanel({ draft, setDraft, onIdentityChange, isDemo = false }) {
  const requestRef = useRef(0);
  const identities = Array.isArray(draft.externalIdentities) ? draft.externalIdentities : [];
  const identity = identities.find((item) => item?.provider === PROVIDER) || null;
  const [query, setQuery] = useState("");
  const [results, setResults] = useState([]);
  const [searching, setSearching] = useState(false);
  const [action, setAction] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const score = useMemo(() => providerScore(identity), [identity]);

  const commit = (nextIdentities, entryPatch = {}) => {
    setDraft((current) => ({ ...current, ...entryPatch, externalIdentities: nextIdentities }));
    onIdentityChange?.({ externalIdentities: nextIdentities, entryPatch });
  };

  const search = async (event) => {
    event.preventDefault();
    const normalized = query.trim();
    if (normalized.length < 2) {
      setResults([]);
      setError("请至少输入 2 个字符。");
      return;
    }
    const requestId = ++requestRef.current;
    setSearching(true);
    setError("");
    setNotice("");
    try {
      const response = await api.get("external-media/providers/bangumi/search/", { params: { q: normalized } });
      if (requestId === requestRef.current) {
        setResults((response.data?.results || []).map(externalMediaResultFromApi));
      }
    } catch (requestError) {
      if (requestId === requestRef.current) {
        setResults([]);
        setError(readableApiError(requestError, "Bangumi 搜索暂时不可用。"));
      }
    } finally {
      if (requestId === requestRef.current) setSearching(false);
    }
  };

  const bind = async (result) => {
    const requestedIdentity = bangumiIdentityFromResult(result);
    if (!requestedIdentity) return;
    const subjectTitle = result.title || result.japaneseTitle || `条目 ${requestedIdentity.external_id}`;
    if (!window.confirm(`将「${draft.title}」绑定到 Bangumi「${subjectTitle}」（ID ${requestedIdentity.external_id}）吗？`)) return;
    setAction("bind");
    setError("");
    setNotice("");
    try {
      const response = await api.post(`entries/${draft.id}/external-identities/`, requestedIdentity);
      const nextIdentities = replaceProviderIdentity(identities, response.data);
      commit(nextIdentities);
      setResults([]);
      setQuery("");
      setNotice("Bangumi 资料已绑定。");
    } catch (requestError) {
      setError(readableApiError(requestError, "绑定失败，请核对条目后重试。"));
    } finally {
      setAction("");
    }
  };

  const refresh = async () => {
    setAction("refresh");
    setError("");
    setNotice("");
    try {
      const response = await api.post(`entries/${draft.id}/external-identities/${PROVIDER}/refresh/`);
      const nextIdentities = replaceProviderIdentity(identities, response.data?.identity);
      const entryPatch = refreshRecordPatch(response.data?.changed_fields, draft);
      commit(nextIdentities, entryPatch);
      const labels = (response.data?.applied_fields || []).map((field) => REFRESH_FIELD_LABELS[field]).filter(Boolean);
      setNotice(labels.length ? `已更新：${labels.join("、")}。` : "资料已是最新状态。");
    } catch (requestError) {
      setError(readableApiError(requestError, "Bangumi 资料刷新失败。"));
    } finally {
      setAction("");
    }
  };

  const unbind = async () => {
    if (!window.confirm("确定解除 Bangumi 绑定吗？\n\n解除后不会删除你的番剧记录、评分、评论或观看记录。")) return;
    setAction("unbind");
    setError("");
    setNotice("");
    try {
      await api.delete(`entries/${draft.id}/external-identities/${PROVIDER}/`);
      commit(identities.filter((item) => item?.provider !== PROVIDER));
      setNotice("Bangumi 绑定已解除。");
    } catch (requestError) {
      setError(readableApiError(requestError, "解除绑定失败，请稍后重试。"));
    } finally {
      setAction("");
    }
  };

  const selectMetadataSource = async (applyMetadata) => {
    setAction(applyMetadata ? "source-apply" : "source-only");
    setError("");
    setNotice("");
    try {
      const response = await api.post(
        `entries/${draft.id}/external-identities/${PROVIDER}/metadata-source/`,
        { apply_metadata: applyMetadata },
      );
      const nextIdentities = identities.map((item) => ({
        ...item,
        is_metadata_source: item?.provider === PROVIDER,
      }));
      const entryPatch = applyMetadata
        ? refreshRecordPatch(response.data?.changed_fields, draft)
        : {};
      commit(nextIdentities, entryPatch);
      setNotice(applyMetadata ? "已设为资料来源并应用可更新字段。" : "已设为资料来源，未改动手账字段。");
    } catch (requestError) {
      setError(readableApiError(requestError, "资料来源切换失败。"));
    } finally {
      setAction("");
    }
  };

  const syncEntryRefresh = ({ entryPatch = {}, externalIdentities } = {}) => {
    commit(Array.isArray(externalIdentities) ? externalIdentities : identities, entryPatch);
  };

  if (identity) {
    return (
      <div className="external-media-panel">
        <div className="external-media-panel__heading">
          <span><Icon name="link" /> Bangumi</span>
          <strong>{identity.is_metadata_source ? "当前资料来源" : "已绑定 · 仅快照"}</strong>
        </div>
        <dl className="external-media-panel__facts">
          <div><dt>条目 ID</dt><dd>{identity.external_id}</dd></div>
          <div><dt>站点评分</dt><dd>{score}</dd></div>
          <div><dt>最近同步</dt><dd>{formatFetchedAt(identity.metadata_fetched_at)}</dd></div>
        </dl>
        <div className="external-media-panel__actions">
          <a href={identity.canonical_url} target="_blank" rel="noreferrer"><Icon name="arrow-up-right" /> 查看 Bangumi</a>
          <button type="button" onClick={refresh} disabled={Boolean(action) || isDemo}><Icon name="reset" /> {action === "refresh" ? "同步中..." : "刷新资料"}</button>
          {!identity.is_metadata_source && <>
            <button type="button" onClick={() => selectMetadataSource(false)} disabled={Boolean(action) || isDemo}><Icon name="check" /> {action === "source-only" ? "切换中..." : "仅设为来源"}</button>
            <button type="button" onClick={() => selectMetadataSource(true)} disabled={Boolean(action) || isDemo}><Icon name="download" /> {action === "source-apply" ? "应用中..." : "设为来源并应用"}</button>
          </>}
          <button className="is-danger" type="button" onClick={unbind} disabled={Boolean(action) || isDemo}><Icon name="unlink" /> {action === "unbind" ? "解除中..." : "解除绑定"}</button>
        </div>
        <ExternalCollectionSyncPanel entryId={draft.id} identityId={identity.id} onEntryRefresh={syncEntryRefresh} isDemo={isDemo} />
        {notice && <p className="external-media-panel__notice" role="status"><Icon name="check" /> {notice}</p>}
        {error && <p className="external-media-panel__error" role="alert"><Icon name="warning" /> {error}</p>}
      </div>
    );
  }

  return (
    <div className="external-media-panel">
      <div className="external-media-panel__heading">
        <span><Icon name="link" /> Bangumi</span>
        <strong>未绑定</strong>
      </div>
      <form className="external-media-panel__search" onSubmit={search}>
        <label htmlFor="external-media-search">搜索 Bangumi 条目</label>
        <div><input id="external-media-search" value={query} onChange={(event) => setQuery(event.target.value)} disabled={Boolean(action) || isDemo} placeholder="番剧中文名或日文名" /><button type="submit" disabled={searching || Boolean(action) || isDemo}><Icon name="search" /> {searching ? "搜索中..." : "搜索"}</button></div>
      </form>
      {results.length > 0 && <div className="external-media-panel__results" aria-label="Bangumi 搜索结果">{results.map((result) => {
        const resultIdentity = bangumiIdentityFromResult(result);
        return (
          <div className="external-media-result" key={resultIdentity?.external_id}>
            {result.thumbnailUrl || result.posterUrl ? <img src={result.thumbnailUrl || result.posterUrl} alt="" /> : <span className="external-media-result__placeholder"><Icon name="film" /></span>}
            <div><strong>{result.title || result.japaneseTitle}</strong><span>{result.japaneseTitle || "日文名未收录"}</span><small>ID {resultIdentity?.external_id} · {result.airDate || "日期未定"}</small></div>
            <button type="button" onClick={() => bind(result)} disabled={Boolean(action)}><Icon name="link" /> {action === "bind" ? "绑定中..." : "绑定"}</button>
          </div>
        );
      })}</div>}
      {!searching && query.trim().length >= 2 && !results.length && !error && <p className="external-media-panel__empty">没有找到匹配条目。</p>}
      {notice && <p className="external-media-panel__notice" role="status"><Icon name="check" /> {notice}</p>}
      {error && <p className="external-media-panel__error" role="alert"><Icon name="warning" /> {error}</p>}
    </div>
  );
}
