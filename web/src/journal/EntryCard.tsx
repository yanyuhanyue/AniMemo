import { Button } from "../components/ui/Button";
import { Input } from "../components/ui/Input";
import type { Entry } from "../api/client";
import { Icon } from "../components/ui/Icon";
import { formatLabels, statusLabels } from "./labels";
import { CoverImage } from "./CoverImage";
import { TagChip } from "./ManageTools";

export function EntryCard({
  entry,
  onOpen,
  onMemory,
  selected,
  onSelect,
}: {
  entry: Entry;
  selected?: boolean;
  onSelect?: () => void;
  onOpen: (entry: Entry) => void;
  onMemory: (entry: Entry) => void;
}) {
  return (
    <article className="entry-card" data-accent={entry.accent}>
      {selected !== undefined && (
        <label className="entry-select">
          <Input type="checkbox" checked={selected} onChange={onSelect} />
          选择 {entry.title}
        </label>
      )}
      <Button
        className="entry-cover entry-cover-summary"
        onClick={() => onOpen(entry)}
        aria-label={`查看 ${entry.title}`}
      >
        <span className="entry-poster">
          <span className="entry-poster-empty" aria-hidden="true"><Icon name="book" /><small>暂无封面</small></span>
          <CoverImage entry={entry} card />
        </span>
        <div className="entry-cover-copy">
        <div className="cover-heading">
          <span>{formatLabels[entry.format]}</span>
          {entry.score !== null && (
            <span className="cover-score">
              <Icon name="star" />
              {entry.score}
              <small>/10</small>
            </span>
          )}
        </div>
        <div className="cover-title">
          <h3>{entry.title}</h3>
          {entry.original_title && <span>{entry.original_title}</span>}
        </div>
        <span className={`entry-status status-${entry.status}`}>
          <span />
          {statusLabels[entry.status]}
        </span>
        </div>
      </Button>
      <div className="entry-body">
        {entry.tags.length > 0 && <div className="entry-tags">{entry.tags.slice(0, 3).map(tag => <TagChip key={tag} name={tag} />)}</div>}
        {entry.notes && <p className="entry-memory-preview">{entry.notes}</p>}
        {entry.watched_episodes > 0 && <p className="entry-viewing-fact">已记录到第 {entry.watched_episodes} 话</p>}
        <div className="entry-actions">
          <Button className="record-button" onClick={() => onMemory(entry)}>
            <Icon name="plus" />
            留点回忆
          </Button>
          <Button
            className="icon-button entry-open"
            onClick={() => onOpen(entry)}
            aria-label={`打开 ${entry.title} 详情`}
          >
            <Icon name="arrow" />
          </Button>
        </div>
      </div>
    </article>
  );
}
