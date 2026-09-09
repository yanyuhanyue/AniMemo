import { useState } from "react";
import { PublicFieldReader } from "./PublicCatalogControls.jsx";
import { publicFacetOptionKey as optionKey } from "../../lib/publicCatalog.js";

export function PublicFacetControl({ name, label, selected, selectedLabel, facet, catalog, onSelect }) {
  const [search, setSearch] = useState("");
  const [reading, setReading] = useState(null);
  const busy = facet.status === "loading";
  const selectedInPage = facet.values.some((item) => optionKey(item) === selected);
  const field = catalog.field.target?.type === "facet" && catalog.field.target?.facet === name ? catalog.field : null;
  return <div className="filter-field">
    <label>
      <span>{label}</span>
      <span className="filter-control-hitbox">
        <select aria-label={label} value={selected || "all"} disabled={busy} onChange={(event) => {
          const item = facet.values.find((value) => optionKey(value) === event.target.value);
          onSelect(item || null);
        }}>
          <option value="all">全部{name === "tags" ? "标签" : "年份"}</option>
          {selected && !selectedInPage && <option value={selected}>{selectedLabel || "已选筛选值"}</option>}
          {facet.values.map((item) => <option key={item.selection_token} value={optionKey(item)}>{item.preview || "（空值）"}{item.complete ? "" : "…（可读完整值）"}</option>)}
        </select>
      </span>
    </label>
    <details>
      <summary>搜索与更多{name === "tags" ? "标签" : "年份"}</summary>
      <label><span>搜索{label}</span><input value={search} maxLength={200} onChange={(event) => setSearch(event.target.value)} onKeyDown={(event) => {
        if (event.key === "Enter") { event.preventDefault(); catalog.loadFacet(name, { search }); }
      }} /></label>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBlock: 8 }}>
        <button type="button" disabled={busy} onClick={() => catalog.loadFacet(name, { search })}>搜索选项</button>
        <button type="button" disabled={busy || !facet.previousAvailable} onClick={() => catalog.loadFacet(name, { direction: "previous" })}>上一组选项</button>
        <button type="button" disabled={busy || !facet.next_cursor} onClick={() => catalog.loadFacet(name, { direction: "next" })}>下一组选项</button>
        <button type="button" disabled={busy} onClick={() => { setSearch(""); catalog.loadFacet(name, { search: "" }); }}>返回首组</button>
      </div>
      <small role="status">{busy ? "正在读取选项…" : `第 ${facet.pageIndex + 1} 组 · ${facet.values.length} 个选项${facet.complete ? " · 已到末组" : ""}`}</small>
      {facet.error && <p role="alert">{facet.error.detail}</p>}
      {facet.values.filter((item) => !item.complete).map((item) => <div key={item.selection_token} style={{ overflowWrap: "anywhere" }}>
        <button type="button" onClick={() => { setReading(item); catalog.readFacetValue(name, item); }}>阅读完整值：{item.preview}…</button>
      </div>)}
      {reading && field && <PublicFieldReader field={field}
        onRead={(direction) => catalog.readFacetValue(name, reading, direction)}
        onDownload={() => catalog.exportFacetValue(name, reading)} exporting={catalog.exporting} onCancel={catalog.cancelExport} />}
    </details>
  </div>;
}
