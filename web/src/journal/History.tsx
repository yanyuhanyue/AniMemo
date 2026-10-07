import type { WatchRecord } from '../api/client';
import { Icon } from '../ui/Icon';

export function HistoryList({ records }: { records: WatchRecord[] }) {
  if (records.length === 0) return <div className="history-empty"><Icon name="clock" /><p>还没有观看记录。</p><span>看完一话，给这段时光留一个位置。</span></div>;
  return <ol className="history-list">{records.map(record => <li key={record.id} data-accent={record.accent}>
    <time dateTime={record.watched_on}><strong>{record.watched_on.slice(5).replace('-', '.')}</strong><span>{record.watched_on.slice(0, 4)}</span></time>
    <div className="history-marker"><Icon name="play" /></div>
    <div className="history-content"><div className="history-title"><h3>{record.entry_title}</h3><span>{record.episode_from === record.episode_to ? `第 ${record.episode_to} 话` : `第 ${record.episode_from}–${record.episode_to} 话`}</span></div>{record.note ? <p>{record.note}</p> : <p className="muted">又和这个故事相处了一会儿。</p>}</div>
  </li>)}</ol>;
}
