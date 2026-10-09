import type { ComponentProps, ReactNode } from 'react';
import type { Entry } from '../api/client';
import { Button } from '../components/ui/Button';
import { Icon } from '../components/ui/Icon';
import { Rating } from '../components/Rating';
import { notesThemeStyle, useTheme } from '../themes/Themes';
import { ThemeBoundary, ThemeFrame, ThemeTemplate } from '../themes/ThemeTemplate';
import { EntryCard } from './EntryCard';
import { CoverImage } from './CoverImage';
import { TagChip } from './ManageTools';
import { formatLabels, statusLabels } from './labels';

const scope = 'private.journal' as const;
export function EntryPoster({ entry, detail = false }: { entry: Entry; detail?: boolean }) {
  return <div className="theme-entry-poster"><div className="theme-cover-empty"><Icon name="book" /><span>暂无封面</span></div><CoverImage entry={entry} card={!detail} /></div>;
}
export function EntryThemeMetadata({ entry, onEdit, disabled }: { entry: Entry; onEdit?: () => void; disabled?: boolean }) {
  return <div className="theme-entry-metadata"><span>{formatLabels[entry.format]}</span><span>{statusLabels[entry.status]}</span><span className="theme-visibility">{{ private: '仅自己', unlisted: '持链接可见', public: '公开' }[entry.visibility]}</span><>{onEdit ? <Button className="theme-rating-edit" aria-label="修改我的评分" onClick={onEdit} disabled={disabled}><Rating score={entry.score} /><Icon name="edit" /></Button> : <Rating score={entry.score} />}</></div>;
}
function EntryThemeTags({ entry }: { entry: Entry }) {
  return entry.tags.length ? <div className="theme-entry-tags">{entry.tags.map(tag => <TagChip key={tag} name={tag} />)}</div> : null;
}
function EntryThemeFacts({ entry, detail = false }: { entry: Entry; detail?: boolean }) {
  return <div className="theme-entry-facts"><span>{entry.watched_episodes > 0 ? `已记录到第 ${entry.watched_episodes} 话${entry.total_episodes ? ` · 共 ${entry.total_episodes} 话` : ''}` : '观看细节还没有补记'}</span>{detail && <a href={`/memory?anime_id=${entry.anime_id}#episodes`}>长篇与集数 →</a>}</div>;
}

type Props = Omit<ComponentProps<typeof EntryCard>, 'entry' | 'selected' | 'onSelect'> & {
  entries: Entry[]; view: 'cards' | 'list'; bulk: boolean; selected: Entry[]; onSelect: (entry: Entry) => void;
};
export function JournalEntries({ entries, view, bulk, selected, onSelect, ...actions }: Props) {
  const release = useTheme(scope);
  const theme = release?.manifest.journal_theme;
  const fallback = <div className={`entry-grid ${view === 'list' ? 'list-view' : ''}`}>{entries.map(entry => <EntryCard key={entry.id} entry={entry} selected={bulk ? selected.some(item => item.id === entry.id) : undefined} onSelect={() => onSelect(entry)} {...actions} />)}</div>;
  if (!release || !theme || bulk) return <>{bulk && theme && <p className="muted">批量管理期间使用默认布局，方便勾选记录。</p>}{fallback}</>;
  const items = entries.map(entry => <ThemeTemplate key={entry.id} release={release} scope={scope} source={view === 'cards' ? theme.presentation.card : theme.presentation.row} slots={{
    title: <h3><Button className="theme-title" onClick={() => actions.onOpen(entry)}>{entry.title}</Button></h3>,
    poster: <EntryPoster entry={entry} />, metadata: <EntryThemeMetadata entry={entry} />, tags: <EntryThemeTags entry={entry} />,
    notes: entry.notes ? <p>{entry.notes}</p> : null, facts: <EntryThemeFacts entry={entry} />,
    read: <Button className="theme-read" aria-label={`打开 ${entry.title} 详情`} onClick={() => actions.onOpen(entry)}>打开详情 →</Button>,
    actions: <div className="theme-entry-actions"><Button onClick={() => actions.onMemory(entry)} aria-label={`为 ${entry.title} 留点回忆`}>留点回忆</Button><Button onClick={() => actions.onEdit(entry)} aria-label={`修改 ${entry.title} 的记录`}>修改</Button></div>,
  }} />);
  return <ThemeBoundary key={release.digest} fallback={fallback}><div className="journal-theme-surface" style={notesThemeStyle(theme)}><ThemeFrame release={release} scope={scope}><ThemeTemplate release={release} scope={scope} source={theme.presentation.list} slots={{ items }} /></ThemeFrame></div></ThemeBoundary>;
}

export function JournalDetailOverview({ entry, fallback, onEdit, disabled }: { entry: Entry; fallback: ReactNode; onEdit: () => void; disabled: boolean }) {
  const release = useTheme(scope), theme = release?.manifest.journal_theme;
  if (!release || !theme) return fallback;
  return <ThemeBoundary key={release.digest} fallback={fallback}><div className="journal-theme-overview" style={notesThemeStyle(theme)}><ThemeFrame release={release} scope={scope}><ThemeTemplate release={release} scope={scope} source={theme.presentation.detail} slots={{
    poster: <EntryPoster entry={entry} detail />, original: entry.original_title ? <p>{entry.original_title}</p> : null,
    metadata: <EntryThemeMetadata entry={entry} onEdit={onEdit} disabled={disabled} />, tags: <EntryThemeTags entry={entry} />, facts: <EntryThemeFacts entry={entry} detail />,
    recollection: <p className="theme-recollection">{entry.notes || '故事的细节可以慢慢补上，先把看过的记忆留在这里。'}</p>,
    memory: <a className="theme-memory-link" href={`/memory?anime_id=${entry.anime_id}#notes`}>翻开作品记忆 →</a>,
  }} /></ThemeFrame></div></ThemeBoundary>;
}
