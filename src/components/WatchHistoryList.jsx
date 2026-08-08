import { Icon } from "./Icon.jsx";

function episodeLabel(record) {
  const start = Number(record?.episode_start);
  const end = Number(record?.episode_end);
  if (start && end) return start === end ? `第 ${start} 话` : `第 ${start}-${end} 话`;
  if (start) return `从第 ${start} 话开始`;
  if (end) return `看到第 ${end} 话`;
  return "未记录话数范围";
}

export function WatchHistoryList({ records = [], emptyText = "暂无观看记录。", editable = false, onRemove }) {
  if (!records.length) {
    return <div className="watch-history-list__empty"><Icon name="history" /><p>{emptyText}</p></div>;
  }

  return (
    <div className="watch-history-list">
      {records.map((record, index) => (
        <article className={editable ? "is-editable" : ""} key={`${record.watched_on || record.watched_label}-${record.brush_label}-${index}`}>
          <span className="watch-history-list__index">NO.{String(index + 1).padStart(2, "0")}</span>
          <div className="watch-history-list__content">
            <strong>{record.watched_label || record.watched_on || "日期缺失"}</strong>
            <small>{episodeLabel(record)}</small>
            {record.notes?.length > 0 && <p>{record.notes.join(" · ")}</p>}
          </div>
          <div className="watch-history-list__actions">
            <b>{record.brush_label || "观看记录"}</b>
            {editable && onRemove && (
              <button type="button" aria-label={`删除 ${record.watched_label || record.watched_on || "这条"}观看记录`} onClick={() => onRemove(index)}>
                <Icon name="trash" /> 删除
              </button>
            )}
          </div>
        </article>
      ))}
    </div>
  );
}
