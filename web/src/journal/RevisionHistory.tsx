import { useQuery } from '@tanstack/react-query';
import { client, result, errorMessage } from '../api/client';
import { statusLabels, watchDate } from './labels';
import type { Status } from '../api/client';

const labels: Record<string,string> = { created: '初次相遇', changed: '心情与进度', removed: '移出手账', 'watch.created': '记一次观看', 'watch.corrected': '更正观看记录', 'watch.retracted': '撤回观看记录' };
export function RevisionHistory({ entryID, userID, version }: { entryID:string; userID:string; version:number }) {
  const history = useQuery({ queryKey:['revisions',userID,entryID,version], queryFn:({signal})=>result(client.GET('/api/v1/entries/{id}/revisions',{params:{path:{id:entryID}},signal})) });
  return <details className="revision-history settings-section"><summary>记忆的变化 <span>评分、心情与观看更正</span></summary>
    {history.isPending ? <p role="status">正在翻阅…</p> : history.isError ? <p role="alert">{errorMessage(history.error)}</p> : <ol>{history.data.items.map(item=>{
      const s=item.snapshot;
      return <li key={item.id}><div><strong>{labels[item.kind] || '记忆更新'}</strong><time>{new Date(item.recorded_at).toLocaleString('zh-CN')}</time></div><p>{typeof s.score === 'number' && `评分 ${s.score} · `}{typeof s.status === 'string' && statusLabels[s.status as Status]}{typeof s.watched_on === 'string' && watchDate(s.watched_on, String(s.time_precision || 'day'))}{typeof s.episode_from === 'number' && ` · 第 ${s.episode_from}–${s.episode_to} 话`}</p>{(s.notes || s.note) ? <blockquote>{String(s.notes || s.note)}</blockquote> : null}</li>;
    })}</ol>}
  </details>;
}
