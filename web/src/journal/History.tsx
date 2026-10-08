import { Button } from '../components/ui/Button';
import type { WatchRecord } from '../api/client';
import { watchDate } from './labels';
import { Icon } from '../components/ui/Icon';

export function HistoryList({ records, onEdit }: { records: WatchRecord[]; onEdit?: (record: WatchRecord) => void }) {
  if (records.length === 0) return <div className="history-empty"><Icon name="clock" /><p>还没有观看记录。</p><span>看完一话，给这段时光留一个位置。</span></div>;
  return <ol className="history-list">{records.map(record => <li key={record.id} data-accent={record.accent}>
    <time dateTime={record.watched_on}><strong>{watchDate(record.watched_on, record.time_precision)}</strong><span>{record.time_precision === 'day' ? '那一天' : '记忆时间'}</span></time>
    <div className="history-marker"><Icon name="play" /></div>
    <div className="history-content"><div className="history-title"><h3>{record.entry_title}</h3><span>{record.episode_from === record.episode_to ? `第 ${record.episode_to} 话` : `第 ${record.episode_from}–${record.episode_to} 话`}{record.rewatch > 1 && ` · 第 ${record.rewatch} 次观看`}</span></div>{record.note ? <p>{record.note}</p> : <p className="muted">又和这个故事相处了一会儿。</p>}{onEdit && <Button className="text-button" onClick={() => onEdit(record)} aria-label={`编辑 ${record.entry_title} ${record.watched_on} 的观看记录`}>更正记录</Button>}</div>
  </li>)}</ol>;
}
