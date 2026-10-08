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
        className="entry-cover"
        onClick={() => onOpen(entry)}
        aria-label={`查看 ${entry.title}`}
      >
        <CoverImage entry={entry} card />
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
        <div className="cover-art" aria-hidden="true">
          <span className="cover-orbit" />
          <span className="cover-disc">
            <Icon name="play" />
          </span>
          <span className="cover-caption">MY ANIME MEMORY</span>
        </div>
        <div className="cover-title">
          <h3>{entry.title}</h3>
          <span>{entry.original_title || "把故事，留在这里。"}</span>
        </div>
        <span className={`entry-status status-${entry.status}`}>
          <span />
          {statusLabels[entry.status]}
        </span>
      </Button>
      <div className="entry-body">
        <div className="entry-tags">
          {entry.tags.length > 0 ? (
            entry.tags
              .slice(0, 3)
              .map((tag) => <TagChip key={tag} name={tag} />)
          ) : (
            <span className="tag-placeholder">属于你的观看清单</span>
          )}
        </div>
        <p className="entry-memory-preview">
          {entry.notes || "还记得哪一幕，或当时的自己？"}
        </p>
        <p className="entry-viewing-fact">
          {entry.watched_episodes > 0
            ? `已记录到第 ${entry.watched_episodes} 话`
            : entry.status === "planned"
              ? "尚未记录观看经历"
              : "具体话数未记"}
          {entry.score !== null ? ` · 我的评分 ${entry.score}` : ""}
        </p>
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
