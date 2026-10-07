import type { Entry } from '../api/client';
import { Icon } from '../ui/Icon';
import { formatLabels, statusLabels } from './labels';
import { CoverImage } from './CoverImage';
import { TagChip } from './ManageTools';

export function EntryCard({ entry, onOpen, onWatch, selected, onSelect }: { entry: Entry; selected?: boolean; onSelect?: () => void; onOpen: (entry: Entry) => void; onWatch: (entry: Entry) => void }) {
  const progress = entry.total_episodes > 0 ? entry.watched_episodes / entry.total_episodes : 0;
  return <article className="entry-card" data-accent={entry.accent}>
    {selected !== undefined && <label className="entry-select"><input type="checkbox" checked={selected} onChange={onSelect} />选择 {entry.title}</label>}
    <button className="entry-cover" onClick={() => onOpen(entry)} aria-label={`查看 ${entry.title}`}>
      <CoverImage entry={entry} card />
      <div className="cover-heading"><span>{formatLabels[entry.format]}</span>{entry.score !== null && <span className="cover-score"><Icon name="star" />{entry.score}<small>/10</small></span>}</div>
      <div className="cover-art" aria-hidden="true"><span className="cover-orbit" /><span className="cover-disc"><Icon name="play" /></span><span className="cover-caption">MY ANIME MEMORY</span></div>
      <div className="cover-title"><h3>{entry.title}</h3><span>{entry.original_title || '把故事，留在这里。'}</span></div>
      <span className={`entry-status status-${entry.status}`}><span />{statusLabels[entry.status]}</span>
    </button>
    <div className="entry-body">
      <div className="entry-tags">{entry.tags.length > 0 ? entry.tags.slice(0, 3).map(tag => <TagChip key={tag} name={tag} />) : <span className="tag-placeholder">属于你的观看清单</span>}</div>
      <div className="progress-label"><span>观看进度</span><strong>{entry.watched_episodes}<span> / {entry.total_episodes || '—'} 话</span></strong></div>
      <progress className="watch-progress" max="1" value={progress} aria-label={`${entry.title}观看进度`} />
      <div className="entry-actions"><button className="record-button" onClick={() => onWatch(entry)}><Icon name="plus" />记一次观看</button><button className="icon-button entry-open" onClick={() => onOpen(entry)} aria-label={`打开 ${entry.title} 详情`}><Icon name="arrow" /></button></div>
    </div>
  </article>;
}
