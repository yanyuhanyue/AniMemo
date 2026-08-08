import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { Icon } from "./Icon.jsx";
import { readablePluginError } from "./errors.js";
import "./styles.css";

const EMPTY = { users: [], batches: [], config: {}, plugin: {} };

function StatusChip({ value }) {
  const labels = {
    pending: "等待匹配",
    matched: "已匹配",
    ambiguous: "需要确认",
    low_confidence: "低置信度",
    no_result: "无结果",
    season_mismatch: "季数不匹配",
    network_error: "网络错误",
    episode_mismatch: "话数不一致",
  };
  return <span className={`ajp-watch-import__status is-${value}`}>{labels[value] || value}</span>;
}

function BangumiLink({ resolution, children }) {
  if (!resolution?.source_url) return <strong>{children}</strong>;
  return <a
    className="ajp-watch-import__bangumi-link"
    href={resolution.source_url}
    target="_blank"
    rel="noreferrer"
    aria-label={`在 Bangumi 查看 ${resolution.title || children}`}
    title={`在 Bangumi 查看 ${resolution.title || children}`}
  >
    <strong>{children}</strong>
    <Icon name="arrow-up-right" />
  </a>;
}

function Summary({ summary = {} }) {
  return <div className="ajp-watch-import__summary">
    <article><span>解析记录</span><strong>{summary.parsed ?? 0}</strong></article>
    <article><span>番剧分组</span><strong>{summary.anime_groups ?? 0}</strong></article>
    <article><span>已匹配</span><strong>{summary.matched ?? 0}</strong></article>
    <article><span>需要确认</span><strong>{summary.manual_review ?? 0}</strong></article>
    <article><span>已剔除</span><strong>{summary.excluded ?? 0}</strong></article>
  </div>;
}

export default function WatchHistoryImporterPage({ host, api: pluginApi }) {
  const client = pluginApi;
  const navigate = useNavigate();
  const inputRef = useRef(null);
  const resolvingRef = useRef(false);
  const [data, setData] = useState(EMPTY);
  const [batch, setBatch] = useState(null);
  const [targetUserId, setTargetUserId] = useState("");
  const [files, setFiles] = useState([]);
  const [dragging, setDragging] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [subjectDrafts, setSubjectDrafts] = useState({});
  const [previewQuery, setPreviewQuery] = useState("");
  const [excludedGroupIndices, setExcludedGroupIndices] = useState(() => new Set());

  const flash = useCallback((message) => {
    setNotice(message);
    window.setTimeout(() => setNotice(""), 2600);
  }, []);

  useEffect(() => {
    if (!host?.auth?.isAuthenticated() || !host.auth.isStaff()) {
      navigate("/admin-login", { replace: true });
      return;
    }
    if (!client) {
      setError("Host SDK API 不可用。");
      setLoading(false);
      return;
    }
    client.get("status/")
      .then(({ data: result }) => {
        setData({ ...EMPTY, ...(result || {}) });
        setTargetUserId(String(result?.users?.find((user) => !user.is_staff)?.id || result?.users?.[0]?.id || ""));
      })
      .catch((requestError) => setError(readablePluginError(requestError, "插件未启用或暂时不可用。")))
      .finally(() => setLoading(false));
  }, [client, navigate]);

  const chooseFiles = (list) => {
    const next = Array.from(list || []).filter((file) => file.name.toLowerCase().endsWith(".txt")).slice(0, 8);
    setFiles(next);
    setError(next.length ? "" : "请选择 TXT 观看记录文件。");
  };

  const openBatch = async (batchId) => {
    if (!batchId || busy) return;
    setBusy(`batch-${batchId}`);
    setError("");
    try {
      const response = await client.get(`batches/${batchId}/`);
      setBatch(response.data);
      setTargetUserId(String(response.data?.target_user?.id || ""));
      setSubjectDrafts({});
      setPreviewQuery("");
      setExcludedGroupIndices(new Set(response.data?.summary?.excluded_group_indices || []));
    } catch (requestError) {
      setError(readablePluginError(requestError, "导入批次读取失败。"));
    } finally {
      setBusy("");
    }
  };

  const resetPreview = () => {
    if (busy) return;
    setBatch(null);
    setFiles([]);
    setSubjectDrafts({});
    setPreviewQuery("");
    setExcludedGroupIndices(new Set());
    if (inputRef.current) inputRef.current.value = "";
  };

  const createPreview = async () => {
    if (!files.length || !targetUserId || busy) return;
    setBusy("preview");
    setError("");
    const payload = new FormData();
    payload.append("target_user_id", targetUserId);
    files.forEach((file) => payload.append("files", file));
    try {
      const response = await client.post("preview/", payload);
      setBatch(response.data);
      setExcludedGroupIndices(new Set());
      flash("解析完成，尚未写入番剧库");
    } catch (requestError) {
      setError(readablePluginError(requestError, "观看记录解析失败。"));
    } finally {
      setBusy("");
    }
  };

  const resolveAll = async () => {
    if (!batch || resolvingRef.current) return;
    resolvingRef.current = true;
    setBusy("resolve");
    setError("");
    let current = batch;
    try {
      const hasSelectedPending = (candidate) => (candidate.groups || []).some((group, index) => (
        !excludedGroupIndices.has(index) && group.resolution?.status === "pending"
      ));
      while (hasSelectedPending(current)) {
        const response = await client.post(`batches/${current.id}/resolve-next/`);
        current = response.data;
        setBatch(current);
      }
      const hasSelectedReview = (current.groups || []).some((group, index) => (
        !excludedGroupIndices.has(index) && !["pending", "matched"].includes(group.resolution?.status)
      ));
      flash(hasSelectedReview ? "自动匹配完成，请处理待确认条目" : "Bangumi 匹配完成");
    } catch (requestError) {
      setError(readablePluginError(requestError, "Bangumi 分批匹配失败，可再次继续。"));
    } finally {
      resolvingRef.current = false;
      setBusy("");
    }
  };

  const selectSubject = async (groupIndex, fallbackBangumiId) => {
    const bangumiId = Number(subjectDrafts[groupIndex] ?? fallbackBangumiId);
    if (!batch || !bangumiId || busy) return;
    setBusy(`select-${groupIndex}`);
    setError("");
    try {
      const response = await client.post(`batches/${batch.id}/select-subject/`, { group_index: groupIndex, bangumi_id: bangumiId });
      setBatch(response.data);
      flash("Bangumi 条目已人工确认");
    } catch (requestError) {
      setError(readablePluginError(requestError, "Bangumi 条目确认失败。"));
    } finally {
      setBusy("");
    }
  };

  const commit = async () => {
    if (!batch || busy) return;
    setBusy("commit");
    setError("");
    try {
      const response = await client.post(`batches/${batch.id}/commit/`, {
        excluded_group_indices: [...excludedGroupIndices].sort((left, right) => left - right),
      });
      setBatch(response.data);
      flash("观看记录已通过事务导入");
    } catch (requestError) {
      setError(readablePluginError(requestError, "正式导入被阻止，请先完成待确认条目。"));
    } finally {
      setBusy("");
    }
  };

  const indexedGroups = useMemo(() => (batch?.groups || []).map((group, index) => ({ ...group, index })), [batch]);
  const visibleGroups = useMemo(() => {
    const query = previewQuery.trim().toLocaleLowerCase("zh-CN");
    if (!query) return indexedGroups;
    return indexedGroups.filter((group) => [
      group.source_title,
      group.resolution?.title,
      group.resolution?.japanese_title,
      group.resolution?.studio,
    ].some((value) => String(value || "").toLocaleLowerCase("zh-CN").includes(query)));
  }, [indexedGroups, previewQuery]);
  const selectedGroups = useMemo(() => indexedGroups.filter((group) => !excludedGroupIndices.has(group.index)), [excludedGroupIndices, indexedGroups]);
  const selectedPendingCount = selectedGroups.filter((group) => group.resolution?.status === "pending").length;
  const reviewGroups = selectedGroups.filter((group) => !["pending", "matched"].includes(group.resolution?.status));
  const selectedMatchedCount = selectedGroups.filter((group) => group.resolution?.status === "matched").length;
  const selectedGroupCount = selectedGroups.length;
  const excludedGroupCount = indexedGroups.length - selectedGroupCount;
  const selectionSummary = batch ? {
    ...batch.summary,
    anime_groups: indexedGroups.length,
    matched: selectedMatchedCount,
    manual_review: reviewGroups.length,
  } : {};
  const imported = batch?.status === "imported";

  const setGroupIncluded = (groupIndex, included) => {
    setExcludedGroupIndices((current) => {
      const next = new Set(current);
      if (included) next.delete(groupIndex);
      else next.add(groupIndex);
      return next;
    });
  };

  const setVisibleGroupsIncluded = (included) => {
    setExcludedGroupIndices((current) => {
      const next = new Set(current);
      visibleGroups.forEach((group) => {
        if (included) next.delete(group.index);
        else next.add(group.index);
      });
      return next;
    });
  };

  return <main className="ajp-watch-import">
    <header className="ajp-watch-import__header">
      <div><span>WATCH HISTORY IMPORTER</span><h1>忆往昔观看记录导入器</h1><p>解析文档、匹配 Bangumi、人工确认，最后一次性写入。</p></div>
      <Link to="/admin-control"><Icon name="arrow-left" /> 返回管理控制室</Link>
    </header>

    {loading ? <div className="ajp-watch-import__loading"><Icon name="spinner" spin /> 正在连接插件</div> : <>
      <section className="ajp-watch-import__workspace">
        <div className="ajp-watch-import__source">
          <label><span>导入目标账号</span><select value={targetUserId} onChange={(event) => setTargetUserId(event.target.value)} disabled={Boolean(batch)}>{data.users.map((user) => <option key={user.id} value={user.id}>{user.username} · {user.email}</option>)}</select></label>
          <label
            className={`ajp-watch-import__drop${dragging ? " is-dragging" : ""}`}
            onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={(event) => { event.preventDefault(); setDragging(false); chooseFiles(event.dataTransfer.files); }}
          >
            <input ref={inputRef} type="file" accept=".txt,text/plain" multiple onChange={(event) => chooseFiles(event.target.files)} />
            <Icon name="file-upload" />
            <strong>{files.length ? `已选择 ${files.length} 个年度文档` : "拖入 2021-2024 TXT 文档"}</strong>
            <small>{files.length ? files.map((file) => file.name).join(" / ") : "也可以点击选择，单个文件最大 2 MB"}</small>
          </label>
          <button type="button" className="is-primary" disabled={!files.length || !targetUserId || Boolean(batch) || busy === "preview"} onClick={createPreview}><Icon name="layers" /> {busy === "preview" ? "正在解析..." : "生成只读预览"}</button>
          {!batch && data.batches.length > 0 && <div className="ajp-watch-import__recent">
            <strong>最近导入批次</strong>
            {data.batches.map((item) => <button type="button" key={item.id} disabled={Boolean(busy)} onClick={() => openBatch(item.id)}>
              <span>{item.source_names?.join(" / ") || `批次 #${item.id}`}</span>
              <small>{item.status} · {item.summary?.anime_groups ?? 0} 部</small>
            </button>)}
          </div>}
        </div>

        <div className="ajp-watch-import__flow">
          <ol><li className={batch ? "is-done" : "is-active"}>解析文档</li><li className={selectedPendingCount === 0 && batch ? "is-done" : batch ? "is-active" : ""}>Bangumi 匹配</li><li className={reviewGroups.length === 0 && selectedPendingCount === 0 && batch ? "is-done" : ""}>人工核对</li><li className={imported ? "is-done" : ""}>事务导入</li></ol>
          {batch ? <>
            <Summary summary={selectionSummary} />
            <div className="ajp-watch-import__actions">
              <button type="button" disabled={Boolean(busy)} onClick={resetPreview}><Icon name="reset" /> 重新选择文档</button>
              <button type="button" disabled={!selectedPendingCount || busy === "resolve" || imported} onClick={resolveAll}><Icon name="search" /> {busy === "resolve" ? `正在匹配，剩余 ${selectedPendingCount}` : "开始 / 继续匹配"}</button>
              <button type="button" className="is-commit" disabled={!selectedGroupCount || selectedPendingCount > 0 || reviewGroups.length > 0 || busy === "commit" || imported} onClick={commit}><Icon name="save" /> {imported ? "已经导入" : busy === "commit" ? "事务处理中..." : `确认导入 ${selectedGroupCount} 部`}</button>
            </div>
          </> : <div className="ajp-watch-import__empty"><Icon name="history" /><p>上传文档后，这里会显示匹配进度和剔除报告。</p></div>}
        </div>
      </section>

      {batch && <section className="ajp-watch-import__preview">
        <header>
          <div><span>IMPORT SELECTION</span><h2>导入内容预览</h2><p>点击“确认正式导入”前不会写入番剧库。</p></div>
          <label><Icon name="search" /><input value={previewQuery} onChange={(event) => setPreviewQuery(event.target.value)} placeholder="搜索标题或制作公司" /></label>
        </header>
        <div className="ajp-watch-import__selection" aria-live="polite">
          <div><strong>将导入 {selectedGroupCount} 部</strong><span>已排除 {excludedGroupCount} 部 · 当前结果 {visibleGroups.length} 部</span></div>
          <div className="ajp-watch-import__selection-actions">
            <button type="button" className="is-include" disabled={imported || !visibleGroups.length || visibleGroups.every((group) => !excludedGroupIndices.has(group.index))} onClick={() => setVisibleGroupsIncluded(true)}>当前结果全部导入</button>
            <button type="button" className="is-exclude" disabled={imported || !visibleGroups.length || visibleGroups.every((group) => excludedGroupIndices.has(group.index))} onClick={() => setVisibleGroupsIncluded(false)}>当前结果全部排除</button>
          </div>
        </div>
        <div className="ajp-watch-import__preview-table" role="region" aria-label="导入内容预览" tabIndex="0">
          <div className="ajp-watch-import__preview-row is-header"><span>导入</span><span>来源标题</span><span>观看记录</span><span>Bangumi 结果</span><span>状态</span></div>
          {visibleGroups.map((group) => {
            const excluded = excludedGroupIndices.has(group.index);
            return <article className={`ajp-watch-import__preview-row${excluded ? " is-excluded" : ""}`} key={`${group.source_key}-${group.index}`}>
            <label className="ajp-watch-import__selection-control">
              <input type="checkbox" checked={!excluded} disabled={imported} onChange={(event) => setGroupIncluded(group.index, event.target.checked)} aria-label={`导入 ${group.source_title}`} />
              <span>{excluded ? "排除" : "导入"}</span>
            </label>
            <div><strong>{group.source_title}</strong><small>{group.records?.[0]?.source_file || ""}</small></div>
            <div><strong>{group.latest_watch_date_label || "日期缺失"}</strong><small>{group.records?.map((record) => record.brush_label).join(" / ")}</small></div>
            <div><BangumiLink resolution={group.resolution}>{group.resolution?.title || "等待匹配"}</BangumiLink><small>{group.resolution?.studio || group.resolution?.japanese_title || "-"}</small></div>
            <StatusChip value={group.resolution?.status || "pending"} />
          </article>;
          })}
          {visibleGroups.length === 0 && <p className="ajp-watch-import__no-result">没有符合条件的预览条目。</p>}
        </div>
        {(batch.excluded || []).length > 0 && <details className="ajp-watch-import__excluded">
          <summary>查看已剔除记录（{batch.excluded.length}）</summary>
          <div>{batch.excluded.map((item) => <article key={`${item.source_file}-${item.source_line}-${item.source_title}`}><strong>{item.source_title}</strong><span>{item.exclusion_reason}</span><small>{item.source_file} · 第 {item.source_line} 行</small></article>)}</div>
        </details>}
      </section>}

      {reviewGroups.length > 0 && <section className="ajp-watch-import__review">
        <header><div><span>MANUAL REVIEW</span><h2>需要人工确认的番剧</h2></div><b>{reviewGroups.length}</b></header>
        <div>{reviewGroups.map((group) => {
          const currentBangumiId = subjectDrafts[group.index] ?? String(group.resolution?.bangumi_id || "");
          const confirming = busy === `select-${group.index}`;
          return <article key={`${group.source_key}-${group.index}`}>
            <div>
              <BangumiLink resolution={group.resolution}>{group.source_title}</BangumiLink>
              <small>{group.records?.[0]?.source_file} · {group.records?.length || 0} 条观看记录</small>
              {group.resolution?.title && <small className="ajp-watch-import__candidate">当前候选：{group.resolution.title} · ID {group.resolution.bangumi_id}</small>}
            </div>
            <StatusChip value={group.resolution?.status} />
            <label><span>Bangumi ID</span><input type="number" value={currentBangumiId} onChange={(event) => setSubjectDrafts((current) => ({ ...current, [group.index]: event.target.value }))} /></label>
            <button type="button" disabled={!currentBangumiId || confirming} onClick={() => selectSubject(group.index, group.resolution?.bangumi_id)}>{confirming ? "确认中..." : group.resolution?.bangumi_id ? "直接确认" : "确认"}</button>
          </article>;
        })}</div>
      </section>}
    </>}

    {error && <div className="ajp-watch-import__message is-error" role="alert"><Icon name="warning" /> {error}</div>}
    {notice && <div className="ajp-watch-import__message is-success" role="status"><Icon name="check" /> {notice}</div>}
  </main>;
}
