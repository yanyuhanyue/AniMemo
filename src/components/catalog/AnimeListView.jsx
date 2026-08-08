import { Icon } from "../Icon.jsx";
import { AnimeListRow } from "./AnimeListRow.jsx";
import { AddAnimePlaceholder } from "./AddAnimePlaceholder.jsx";
import { useCatalogReveal } from "./useCatalogReveal.js";

export function AnimeListView({ records, onOpenDetail, sort, onSortChange, ready, variant, onAddRecord }) {
  const revealDependency = records.map((record) => record.id).join(":");
  const listRef = useCatalogReveal({ enabled: ready, dependency: revealDependency });

  const toggleSort = (type) => {
    if (type === "date") {
      onSortChange(sort === "date-asc" ? "date-desc" : "date-asc");
      return;
    }
    onSortChange(sort === "score-desc" ? "score-asc" : "score-desc");
  };

  return (
    <div className="anime-list" ref={listRef}>
      <div className="anime-list__head">
        <span>序号</span>
        <span>海报画风</span>
        <span>番剧名称（中/日）</span>
        <button
          type="button"
          className={`table-sort-button table-sort-button--season${sort.startsWith("date") ? " is-active" : ""}`}
          onClick={() => toggleSort("date")}
          title="点击切换开播时间排序"
        >
          <span className="table-sort-button__visual">
            放送季度
            <span key={sort.startsWith("date") ? sort : "date"} className="table-sort-button__icon">
              <Icon name={sort === "date-asc" ? "arrow-up" : "arrow-down"} />
            </span>
          </span>
        </button>
        <span>类型标签</span>
        <button
          type="button"
          className={`table-sort-button${sort.startsWith("score") ? " is-active" : ""}`}
          onClick={() => toggleSort("score")}
          title="点击切换评分排序"
        >
          <span className="table-sort-button__visual">
            综合评分
            <span key={sort.startsWith("score") ? sort : "score"} className="table-sort-button__icon">
              <Icon name={sort === "score-asc" ? "arrow-up" : "arrow-down"} />
            </span>
          </span>
        </button>
        <span>操作</span>
      </div>
      {records.map((record, index) => (
        <AnimeListRow
          key={record.id}
          record={record}
          rank={index + 1}
          onOpen={onOpenDetail}
          catalogReady={ready}
          variant={variant}
        />
      ))}
      {onAddRecord && <AddAnimePlaceholder layout="list" onAdd={onAddRecord} />}
    </div>
  );
}
