import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { client, errorMessage, result } from '../api/client';
import type { WatchRecord } from '../api/client';
import { HistoryList } from './History';
import { WatchEditor } from './WatchEditor';

export function HistoryPanel({ userID }: { userID: string }) {
  const [range, setRange] = useState({ from: '', to: '' });
  const [page, setPage] = useState(1);
  const [editing, setEditing] = useState<WatchRecord | null>(null);
  const cache = useQueryClient();
  const history = useQuery({ queryKey: ['journal', userID, 'history-page', range, page], queryFn: ({ signal }) => result(client.GET('/api/v1/history/page', { signal, params: { query: { ...range, page } } })) });
  const analytics = useQuery({ queryKey: ['journal', userID, 'analytics', range], queryFn: ({ signal }) => result(client.GET('/api/v1/analytics', { signal, params: { query: range } })) });
  const entry = useQuery({ queryKey: ['journal', userID, 'entry', editing?.entry_id], enabled: !!editing, queryFn: ({ signal }) => result(client.GET('/api/v1/entries/{id}', { signal, params: { path: { id: editing!.entry_id } } })) });
  return <section className="history-panel"><div className="section-heading"><h2>观看足迹与统计</h2><span>观看日仅统计精确日期；月度分布不含年份或未知时间</span></div>
    <form className="date-filter" onSubmit={event => { event.preventDefault(); const data = new FormData(event.currentTarget); setRange({ from: String(data.get('from')), to: String(data.get('to')) }); setPage(1); }}><label>起始日期<Input name="from" type="date" min="1900-01-01" max="2100-12-31" /></label><label>结束日期<Input name="to" type="date" min="1900-01-01" max="2100-12-31" /></label><Button className="button secondary">应用日期范围</Button></form>
    {analytics.data && <><div className="activity-summary"><span><strong>{analytics.data.active_days}</strong> 个观看日</span><span><strong>{analytics.data.records}</strong> 次记录</span><span><strong>{analytics.data.episodes}</strong> 话（含重看）</span><span>全部番剧均分 <strong>{analytics.data.average_score?.toFixed(1) || '—'}</strong></span></div>{analytics.data.months.length > 0 && <details className="monthly-activity"><summary>月度观看分布</summary><ul>{analytics.data.months.map(month => <li key={month.month}><span>{month.month}</span><meter value={month.episodes} max={Math.max(...analytics.data.months.map(item => item.episodes), 1)} aria-label={`${month.month} 观看话数`} /><span>{month.episodes} 话 · {month.records} 次</span></li>)}</ul></details>}</>}
    {history.isPending ? <p role="status">正在读取观看记录…</p> : history.data && <><HistoryList records={history.data.items} onEdit={setEditing} /><div className="pagination"><Button className="button quiet" disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</Button><span>{history.data.total} 条记录 · 第 {page} 页</span><Button className="button quiet" disabled={page * 50 >= history.data.total} onClick={() => setPage(page + 1)}>下一页</Button></div></>}
    {(history.error || analytics.error || entry.error) && <p className="error-message" role="alert">{errorMessage(history.error || analytics.error || entry.error)}</p>}
    {editing && entry.data && <WatchEditor key={editing.id} entry={entry.data} record={editing} onClose={() => setEditing(null)} onSaved={async () => { setEditing(null); await cache.invalidateQueries({ queryKey: ['journal', userID] }); }} />}
  </section>;
}
