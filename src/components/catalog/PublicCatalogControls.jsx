const controlsStyle = { display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8, marginBlock: 8 };
const buttonStyle = { border: "2px solid var(--ink)", background: "var(--cream)", padding: "6px 10px", font: "inherit", cursor: "pointer" };

export function PublicCatalogPager({ list, onPrevious, onNext, onRefresh, label = "番剧列表翻页" }) {
  const busy = list.status === "loading";
  return <nav aria-label={label} style={controlsStyle}>
    <button type="button" style={buttonStyle} disabled={busy || !list.previousAvailable} onClick={onPrevious}>上一页</button>
    <span aria-live="polite">第 {list.pageIndex + 1} 页 · 本页 {list.results.length} 条</span>
    <button type="button" style={buttonStyle} disabled={busy || !list.next_cursor} onClick={onNext}>下一页</button>
    <button type="button" style={buttonStyle} disabled={busy} onClick={onRefresh}>刷新并回首页</button>
    {list.historyEvicted && <small>较早页面已移出缓存，请从首页继续浏览。</small>}
    {list.duplicates > 0 && <small>内容已更新，本页已合并重复记录；可刷新查看最新顺序。</small>}
  </nav>;
}

export function PublicExportStatus({ state, onCancel }) {
  if (!state || state.status === "idle") return null;
  return <div role="status" aria-live="polite" style={{ overflowWrap: "anywhere" }}>
    {state.status === "choosing" && "请选择完整数据的保存位置。"}
    {state.status === "writing" && <span>正在下载：{state.kind === "all" ? `${state.records} 条记录 · ` : ""}{Math.ceil(state.bytes / 1024)} KiB <button type="button" style={buttonStyle} onClick={onCancel}>取消下载</button></span>}
    {state.status === "writing" && state.retryAfter && <p>服务器请求较多，约 {state.retryAfter} 秒后继续下载。</p>}
    {state.status === "finalizing" && "正在完成文件保存…"}
    {state.status === "complete" && `完整${state.kind === "field" ? "字段" : "数据"}已保存${state.kind === "all" ? `，共 ${state.records} 条记录` : ""}。`}
    {state.status === "cancelled" && "下载已取消，文件未完成保存。"}
    {state.status === "error" && state.error?.detail}
  </div>;
}

export function PublicFieldReader({ field, onRead, onDownload, exporting, onCancel }) {
  const busy = field?.status === "loading";
  const frame = field?.frame;
  return <section aria-label="完整字段分段阅读" style={{ maxWidth: "100%", overflowWrap: "anywhere" }}>
    <div style={controlsStyle}>
      <button type="button" style={buttonStyle} disabled={busy} onClick={() => onRead("first")}>从头读取</button>
      <button type="button" style={buttonStyle} disabled={busy || !field?.previousAvailable} onClick={() => onRead("previous")}>上一段</button>
      <button type="button" style={buttonStyle} disabled={busy || !frame?.next_cursor} onClick={() => onRead("next")}>下一段</button>
      <button type="button" style={buttonStyle} disabled={["choosing", "writing", "finalizing"].includes(exporting?.status)} onClick={onDownload}>下载完整字段</button>
    </div>
    {busy && <p role="status">正在读取字段片段…</p>}
    {field?.error && <p role="alert">{field.error.detail}</p>}
    {frame && <>
      <small>第 {frame.pageIndex + 1} 段 · {frame.complete ? "已到末尾" : "后续内容可继续读取"}</small>
      <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", font: "inherit", maxHeight: 300, overflow: "auto" }} tabIndex={0}>{frame.text || "（此片段不含可显示文字）"}</pre>
      {frame.pageIndex > 0 && !field.previousAvailable && <p>较早片段已移出缓存，可从头读取或下载完整字段。</p>}
    </>}
    <PublicExportStatus state={exporting?.kind === "all" ? null : exporting} onCancel={onCancel} />
  </section>;
}
