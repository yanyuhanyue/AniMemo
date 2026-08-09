import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { Icon } from "../Icon.jsx";
import { api, readableApiError } from "../../lib/api.js";
import { buildImportItems, importResultCount, initialImportAction } from "../../lib/externalAccounts.js";

const FILTERS = [
  ["all", "全部"], ["planned", "想看"], ["watching", "在看"], ["completed", "看过"],
  ["on_hold", "搁置"], ["conflict", "冲突"], ["existing", "已存在"],
];

function actionLabel(mode) {
  return { CREATE_NEW: "新建记录", BIND_EXISTING: "绑定到本地", IMPORT_SAFE_USER_FIELDS: "导入选定字段", SKIP: "跳过" }[mode] || mode;
}

export function BangumiImportDialog({ onClose, onImported }) {
  const closeRef = useRef(null);
  const [preview, setPreview] = useState(null);
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [actions, setActions] = useState({});
  const [busy, setBusy] = useState("preview");
  const [error, setError] = useState("");
  const [result, setResult] = useState(null);

  const mergeActions = (rows) => setActions((current) => {
    const next = { ...current };
    rows.forEach((row) => { if (!next[row.external_id]) next[row.external_id] = initialImportAction(row); });
    return next;
  });

  const loadPage = async ({ previewId, nextPage = 1, nextFilter = filter, nextQuery = query } = {}) => {
    const id = previewId || preview?.preview_id;
    if (!id) return;
    setBusy("page");
    setError("");
    try {
      const { data } = await api.get(`external-accounts/bangumi/import-preview/${id}/`, { params: { page: nextPage, page_size: 24, filter: nextFilter, query: nextQuery } });
      setPreview(data);
      setPage(data.page);
      mergeActions(data.results || []);
    } catch (requestError) {
      setError(readableApiError(requestError, "导入预览读取失败。"));
    } finally {
      setBusy("");
    }
  };

  useEffect(() => {
    let active = true;
    api.post("external-accounts/bangumi/import-preview/", { page: 1, page_size: 24 })
      .then(({ data }) => {
        if (!active) return;
        setPreview(data);
        setPage(data.page);
        mergeActions(data.results || []);
      })
      .catch((requestError) => { if (active) setError(readableApiError(requestError, "Bangumi 收藏读取失败。")); })
      .finally(() => { if (active) setBusy(""); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    closeRef.current?.focus();
    const onKeyDown = (event) => { if (event.key === "Escape") onClose(); };
    document.body.classList.add("modal-open");
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.classList.remove("modal-open");
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [onClose]);

  const updateAction = (externalId, patch) => setActions((current) => ({
    ...current,
    [externalId]: { ...(current[externalId] || {}), ...patch },
  }));
  const toggleField = (externalId, field) => {
    const current = actions[externalId] || {};
    const fields = current.apply_fields || [];
    updateAction(externalId, { apply_fields: fields.includes(field) ? fields.filter((item) => item !== field) : [...fields, field] });
  };
  const selectedCount = useMemo(() => importResultCount(actions), [actions]);

  const apply = async () => {
    const items = buildImportItems(actions);
    if (!items.length) {
      setError("请选择至少一个要导入的收藏项目。");
      return;
    }
    setBusy("apply");
    setError("");
    try {
      const { data } = await api.post("external-accounts/bangumi/import-apply/", { preview_id: preview.preview_id, items });
      setResult(data);
      onImported?.(data);
    } catch (requestError) {
      setError(readableApiError(requestError, "收藏导入失败，请检查冲突后重试。"));
    } finally {
      setBusy("");
    }
  };

  return createPortal(
    <div className="bangumi-import-backdrop" role="dialog" aria-modal="true" aria-label="导入 Bangumi 收藏">
      <button type="button" className="bangumi-import-backdrop__dismiss" onClick={onClose} aria-label="关闭导入收藏" />
      <section className="bangumi-import-dialog">
        <header><div><span>READ-ONLY COLLECTION IMPORT</span><h2>导入 Bangumi 收藏</h2><p>预览后逐项决定，本地资料默认优先。</p></div><button ref={closeRef} type="button" onClick={onClose} aria-label="关闭"><Icon name="close" /></button></header>
        {busy === "preview" ? <div className="bangumi-import-loading" role="status"><Icon name="spinner" spin /><strong>正在分页读取 Bangumi 收藏</strong><small>不会自动创建或覆盖任何记录</small></div> : result ? (
          <div className="bangumi-import-result" role="status"><Icon name="circle-check" /><h3>导入处理完成</h3><dl>{Object.entries(result.counts || {}).map(([key, value]) => <div key={key}><dt>{({ created: "新建", bound: "绑定", updated: "更新", skipped: "跳过", conflict: "冲突", failed: "失败" })[key]}</dt><dd>{value}</dd></div>)}</dl><button type="button" onClick={onClose}>返回手账</button></div>
        ) : (
          <>
            <div className="bangumi-import-tools">
              <nav aria-label="收藏筛选">{FILTERS.map(([value, label]) => <button type="button" key={value} className={filter === value ? "is-active" : ""} onClick={() => { setFilter(value); setPage(1); loadPage({ nextPage: 1, nextFilter: value }); }}>{label}</button>)}</nav>
              <form onSubmit={(event) => { event.preventDefault(); setPage(1); loadPage({ nextPage: 1, nextQuery: query }); }}><label><span className="sr-only">搜索收藏</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索收藏标题" maxLength={100} /></label><button type="submit" aria-label="搜索"><Icon name="search" /></button></form>
            </div>
            {preview?.summary && <div className="bangumi-import-summary"><span>远端 {preview.summary.remote_count}</span><span>已存在 {preview.summary.already_bound}</span><span>可能重复 {preview.summary.possible_duplicates}</span><span>字段冲突 {preview.summary.conflicts}</span></div>}
            {error && <p className="bangumi-import-error" role="alert"><Icon name="warning" /> {error}</p>}
            <div className={`bangumi-import-rows ${busy === "page" ? "is-loading" : ""}`} aria-busy={busy === "page"}>
              {(preview?.results || []).map((row) => {
                const action = actions[row.external_id] || initialImportAction(row);
                const modes = row.match_state === "already_bound" ? ["IMPORT_SAFE_USER_FIELDS", "SKIP"] : ["CREATE_NEW", "BIND_EXISTING", "SKIP"];
                return <article key={row.external_id} className={`bangumi-import-row is-${row.match_state}`}>
                  <label className="bangumi-import-select"><input type="checkbox" checked={Boolean(action.selected)} onChange={(event) => updateAction(row.external_id, { selected: event.target.checked })} /><span className="sr-only">选择 {row.title}</span></label>
                  <div className="bangumi-import-poster">{row.poster_url ? <img src={row.poster_url} alt="" /> : <Icon name="film" />}</div>
                  <div className="bangumi-import-copy"><strong>{row.title}</strong><small>{row.japanese_title || `Bangumi #${row.external_id}`}</small><div><span>{row.remote_status_label}</span><span>{row.remote_rating ? `${row.remote_rating}/10` : "未评分"}</span><span>{row.match_state === "already_bound" ? "已绑定" : row.match_state === "possible_local_match" ? "可能重复" : "未存在"}</span></div>{row.remote_comment_summary && <p>{row.remote_comment_summary}</p>}</div>
                  <div className="bangumi-import-decision">
                    <label><span>处理方式</span><select value={action.mode} onChange={(event) => updateAction(row.external_id, { mode: event.target.value, selected: event.target.value !== "SKIP" })}>{modes.map((mode) => <option value={mode} key={mode}>{actionLabel(mode)}</option>)}</select></label>
                    {action.mode === "BIND_EXISTING" && <label><span>绑定到</span><select value={action.local_entry_id || ""} onChange={(event) => updateAction(row.external_id, { local_entry_id: event.target.value })} required><option value="">选择本地记录</option>{(preview.bind_targets || []).map((entry) => <option value={entry.id} key={entry.id}>{entry.title}</option>)}</select></label>}
                    {action.mode === "IMPORT_SAFE_USER_FIELDS" && <fieldset><legend>显式使用 Bangumi</legend>{[["personal_score", "评分"], ["watch_status", "状态"], ["review", "短评"]].map(([field, label]) => <label key={field}><input type="checkbox" checked={(action.apply_fields || []).includes(field)} onChange={() => toggleField(row.external_id, field)} /><span>{label}</span>{row.conflicts?.[field] && <small>AniMemo：{String(row.conflicts[field].local)} / Bangumi：{String(row.conflicts[field].remote)}</small>}</label>)}</fieldset>}
                  </div>
                </article>;
              })}
              {!preview?.results?.length && <div className="bangumi-import-empty"><Icon name="search" /><strong>当前筛选没有收藏项目</strong></div>}
            </div>
            <footer><div><button type="button" onClick={() => loadPage({ nextPage: page - 1 })} disabled={page <= 1 || Boolean(busy)} aria-label="上一页"><Icon name="arrow-left" /></button><span>{page} / {preview?.pages || 1}</span><button type="button" onClick={() => loadPage({ nextPage: page + 1 })} disabled={page >= (preview?.pages || 1) || Boolean(busy)} aria-label="下一页"><Icon name="arrow-right" /></button></div><strong>已选择 {selectedCount} 项</strong><button type="button" className="is-apply" onClick={apply} disabled={!selectedCount || Boolean(busy)}><Icon name={busy === "apply" ? "spinner" : "check"} spin={busy === "apply"} /> {busy === "apply" ? "正在导入" : "确认导入"}</button></footer>
          </>
        )}
      </section>
    </div>,
    document.body,
  );
}
