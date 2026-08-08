import { useCallback, useEffect, useMemo, useState } from "react";

import { api, readableApiError } from "../../lib/api.js";
import { Icon } from "../Icon.jsx";
import { AdminConfirmDialog, AdminDetailDialog, AuditLogList } from "./AdminControlDialogs.jsx";
import {
  EMPTY_PAGE,
  dateTimeLabel,
  hasCapability,
  resourceMeta,
  resourceStatus,
  statusOptions,
} from "./adminControlUtils.js";


export function AdminResourcePanel({ kind, viewer, onNotice, onError }) {
  const meta = resourceMeta[kind];
  const [pageData, setPageData] = useState(EMPTY_PAGE);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState([]);
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [confirmation, setConfirmation] = useState(null);
  const canAccess = Boolean(meta && hasCapability(viewer, meta.capability));

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedQuery(query.trim());
      setPage(1);
    }, 260);
    return () => window.clearTimeout(timer);
  }, [query]);

  const load = useCallback(async () => {
    if (!canAccess) return;
    setLoading(true);
    try {
      const { data } = await api.get(`staff/resources/${kind}/`, { params: { page, page_size: pageSize, q: debouncedQuery, status } });
      setPageData({ ...EMPTY_PAGE, ...data });
      setSelected([]);
    } catch (error) {
      onError(readableApiError(error, "后台列表加载失败。"));
    } finally {
      setLoading(false);
    }
  }, [canAccess, debouncedQuery, kind, onError, page, pageSize, status]);

  useEffect(() => { void load(); }, [load]);

  const ids = useMemo(() => pageData.results.map((item) => `${item.resource_type || kind}:${item.id}`), [kind, pageData.results]);
  const allSelected = ids.length > 0 && ids.every((id) => selected.includes(id));

  const toggle = (id) => setSelected((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]);
  const toggleAll = () => setSelected(allSelected ? [] : ids);

  const openDetail = async (item) => {
    setDetailLoading(true);
    setDetail({ ...item, loading: true });
    try {
      const endpoint = kind === "users" ? `staff/users/${item.id}/detail/` : `staff/resources/${kind}/${item.id}/`;
      const { data } = await api.get(endpoint);
      setDetail(data);
    } catch (error) {
      setDetail(null);
      onError(readableApiError(error, "详情加载失败。"));
    } finally {
      setDetailLoading(false);
    }
  };

  const run = async (task, successMessage) => {
    try {
      const result = await task();
      onNotice(result?.data?.detail || successMessage);
      setDetail(null);
      await load();
    } catch (error) {
      onError(readableApiError(error));
    }
  };

  const ask = (config) => setConfirmation({ reason: "", ...config });

  const reviewColumn = (item, action) => {
    if (action === "approve") return run(() => api.patch(`staff/columns/${item.id}/review/`, { status: "approved" }), "专栏已通过审核");
    ask({
      title: "驳回专栏",
      message: `请说明《${item.title}》未通过审核的原因，投稿者会看到这段反馈。`,
      confirmLabel: "确认驳回",
      reasonRequired: true,
      onConfirm: (reason) => run(() => api.patch(`staff/columns/${item.id}/review/`, { status: "rejected", reason }), "专栏已驳回"),
    });
  };

  const reviewJournal = (item, action) => {
    if (action === "approve") return run(() => api.patch(`staff/public-journals/${item.id}/review/`, { status: "approved" }), "公开手账已通过");
    ask({
      title: "驳回公开申请",
      message: `将 ${item.nickname} 的手账恢复为私密，并把原因反馈给用户。`,
      confirmLabel: "确认驳回",
      reasonRequired: true,
      onConfirm: (reason) => run(() => api.patch(`staff/public-journals/${item.id}/review/`, { status: "private", reason }), "公开申请已驳回"),
    });
  };

  const recycle = (item, itemKind = kind) => ask({
    title: "移入回收站",
    message: `这会立即从前台隐藏“${item.title}”，但仍可在回收站恢复。`,
    confirmLabel: "移入回收站",
    reasonRequired: true,
    onConfirm: (reason) => run(() => api.post(`staff/bulk/${itemKind}/`, { ids: [item.id], action: "recycle", reason }), "内容已移入回收站"),
  });

  const restore = (item) => run(() => api.post(`staff/bulk/${item.resource_type === "column" ? "columns" : "entries"}/`, { ids: [item.id], action: "restore" }), "内容已恢复");

  const bulk = (action) => {
    const grouped = selected.reduce((result, value) => {
      const [prefix, rawId] = value.split(":");
      const resource = prefix === "column" ? "columns" : prefix === "entry" ? "entries" : kind;
      result[resource] ||= [];
      result[resource].push(Number(rawId));
      return result;
    }, {});
    const reasonRequired = action === "reject" || action === "recycle";
    ask({
      title: "确认批量操作",
      message: `将对 ${selected.length} 条记录执行“${action === "approve" ? "通过" : action === "reject" ? "驳回" : action === "restore" ? "恢复" : "移入回收站"}”。`,
      confirmLabel: "确认执行",
      reasonRequired,
      onConfirm: async (reason) => {
        try {
          await Promise.all(Object.entries(grouped).map(([resource, groupedIds]) => api.post(`staff/bulk/${resource}/`, { ids: groupedIds, action, reason })));
          onNotice(`已处理 ${selected.length} 条记录`);
          await load();
        } catch (error) {
          onError(readableApiError(error));
        }
      },
    });
  };

  if (!canAccess) {
    return <section className="admin-panel admin-panel--full"><div className="admin-empty-state"><Icon name="lock" /><span>当前后台角色没有此模块权限</span></div></section>;
  }

  return (
    <>
      <section className={`admin-panel admin-panel--full admin-resource-panel admin-resource-panel--${kind}`}>
        <header>
          <div><span>{meta.kicker}</span><h3>{meta.title}</h3></div>
          <div className="admin-table-tools">
            <label><Icon name="search" /><input aria-label={meta.placeholder} value={query} onChange={(event) => setQuery(event.target.value)} placeholder={meta.placeholder} /></label>
            {statusOptions[kind] && <select aria-label="状态筛选" value={status} onChange={(event) => { setStatus(event.target.value); setPage(1); }}>{statusOptions[kind].map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>}
            <select aria-label="每页数量" value={pageSize} onChange={(event) => { setPageSize(Number(event.target.value)); setPage(1); }}><option value="10">10 / 页</option><option value="20">20 / 页</option><option value="50">50 / 页</option></select>
            <button type="button" onClick={load}><Icon name="reset" /> 刷新</button>
          </div>
        </header>

        {kind !== "audit" && pageData.results.length > 0 && <div className="admin-bulk-bar">
          <label><input type="checkbox" checked={allSelected} onChange={toggleAll} /> 本页全选</label>
          <span>已选 {selected.length} / 共 {pageData.count}</span>
          {selected.length > 0 && <div>
            {(kind === "columns" || kind === "journals") && <button type="button" className="is-approve" onClick={() => bulk("approve")}><Icon name="check" /> 批量通过</button>}
            {(kind === "columns" || kind === "journals") && <button type="button" className="is-reject" onClick={() => bulk("reject")}><Icon name="close" /> 批量驳回</button>}
            {(kind === "columns" || kind === "entries") && <button type="button" className="is-reject" onClick={() => bulk("recycle")}><Icon name="trash" /> 移入回收站</button>}
            {kind === "recycle" && <button type="button" className="is-approve" onClick={() => bulk("restore")}><Icon name="reset" /> 批量恢复</button>}
          </div>}
        </div>}

        {loading ? <div className="admin-dashboard-loading"><span /><span /><span /> 正在读取完整数据</div> : pageData.results.length === 0 ? <div className="admin-empty-state"><Icon name="layers" /><span>没有匹配的数据</span></div> : kind === "audit" ? <AuditLogList entries={pageData.results} onOpen={setDetail} /> : <div className="admin-resource-list">
          {pageData.results.map((item) => {
            const selectId = `${item.resource_type || kind}:${item.id}`;
            const statusInfo = resourceStatus(item, kind);
            const rowTone = kind === "users" ? (item.is_superuser ? "superuser" : item.is_staff ? "staff" : "user") : "";
            return <article className={`admin-resource-row${rowTone ? ` is-${rowTone}` : ""}`} key={selectId}>
              <input className="admin-resource-row__check" type="checkbox" checked={selected.includes(selectId)} onChange={() => toggle(selectId)} aria-label={`选择 ${item.title || item.nickname || item.username}`} />
              <div className="admin-resource-row__main">
                <strong>{item.title || item.nickname || item.target_label || item.username}</strong>
                <small>{item.author ? `${item.author} · ${item.author_email || "未填写邮箱"}` : item.user ? `${item.user} · ${item.email || "未填写邮箱"}` : item.email || item.target_type}</small>
                {(item.moderation_reason || item.review_reason || item.deletion_reason) && <p>{item.moderation_reason || item.review_reason || item.deletion_reason}</p>}
              </div>
              <div className="admin-resource-row__state">
                <div className="admin-resource-row__badges">
                  <span className={`admin-status is-${statusInfo.tone}`}>{statusInfo.label}</span>
                  {statusInfo.secondaryLabel && <span className={`admin-status is-${statusInfo.secondaryTone}`}>{statusInfo.secondaryLabel}</span>}
                </div>
                <small>{dateTimeLabel(item.updated_at || item.created_at || item.deleted_at)}</small>
              </div>
              <div className="admin-row-actions">
                {kind !== "recycle" && <button type="button" onClick={() => openDetail(item)}><Icon name="eye" /> 详情</button>}
                {kind === "columns" && item.status !== "approved" && <button type="button" className="is-approve" onClick={() => reviewColumn(item, "approve")}><Icon name="check" /> 通过</button>}
                {kind === "columns" && item.status === "pending" && <button type="button" className="is-reject" onClick={() => reviewColumn(item, "reject")}><Icon name="close" /> 驳回</button>}
                {kind === "journals" && item.public_status === "pending" && <button type="button" className="is-approve" onClick={() => reviewJournal(item, "approve")}><Icon name="check" /> 通过</button>}
                {kind === "journals" && item.public_status !== "private" && <button type="button" className="is-reject" onClick={() => reviewJournal(item, "reject")}><Icon name="eye-slash" /> 撤销</button>}
                {(kind === "columns" || kind === "entries") && <button type="button" className="is-reject" onClick={() => recycle(item)}><Icon name="trash" /> 回收</button>}
                {kind === "recycle" && <button type="button" className="is-approve" onClick={() => restore(item)}><Icon name="reset" /> 恢复</button>}
              </div>
            </article>;
          })}
        </div>}

        <footer className="admin-pagination">
          <span>第 {pageData.page} / {Math.max(pageData.pages, 1)} 页，共 {pageData.count} 条</span>
          <div><button type="button" disabled={pageData.page <= 1} onClick={() => setPage((current) => Math.max(current - 1, 1))}><Icon name="arrow-left" /> 上一页</button><button type="button" disabled={pageData.page >= pageData.pages} onClick={() => setPage((current) => current + 1)}>下一页 <Icon name="arrow-right" /></button></div>
        </footer>
      </section>

      {detail && <AdminDetailDialog kind={kind} detail={detail} loading={detailLoading} viewer={viewer} onClose={() => setDetail(null)} onAsk={ask} onRun={run} />}
      {confirmation && <AdminConfirmDialog value={confirmation} onChange={setConfirmation} onClose={() => setConfirmation(null)} />}
    </>
  );
}

