import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { client, errorMessage, result } from '../api/client';
import type { Entry } from '../api/client';
import { Dialog } from '../ui/Dialog';
import { Icon } from '../ui/Icon';
import { HistoryList } from './History';
import { formatLabels, statusLabels } from './labels';

export function EntryDetail({ entry, userID, onClose, onEdit, onWatch, onDeleted }: { entry: Entry; userID: string; onClose: () => void; onEdit: (entry: Entry) => void; onWatch: (entry: Entry) => void; onDeleted: (message: string) => void }) {
  const [confirmDelete, setConfirmDelete] = useState(false);
  const cache = useQueryClient();
  const current = useQuery({ queryKey: ['journal', userID, 'entry', entry.id], queryFn: ({ signal }) => result(client.GET('/api/v1/entries/{id}', { signal, params: { path: { id: entry.id } } })) });
  const history = useQuery({ queryKey: ['journal', userID, 'history', entry.id], queryFn: ({ signal }) => result(client.GET('/api/v1/entries/{id}/history', { signal, params: { path: { id: entry.id } } })) });
  const shown = current.data || entry;
  const remove = useMutation({
    mutationFn: () => result(client.DELETE('/api/v1/entries/{id}', { params: { path: { id: entry.id }, query: { version: shown.version } } })),
    onSuccess: async () => {
      for (const queryKey of [['journal', userID, 'entry', entry.id], ['journal', userID, 'history', entry.id]]) {
        await cache.cancelQueries({ queryKey, exact: true });
        cache.removeQueries({ queryKey, exact: true });
      }
      onDeleted('番剧及其观看记录已删除');
    },
  });
  return <Dialog title={shown.title} eyebrow="YOUR ANIME MEMORY" onClose={onClose} wide>
    <div className="detail-content">
      {shown.original_title && <p className="detail-original">{shown.original_title}</p>}
      <div className="detail-badges"><span>{formatLabels[shown.format]}</span><span>{statusLabels[shown.status]}</span>{shown.score !== null && <span className="score-badge"><Icon name="star" />{shown.score} / 10</span>}</div>
      <div className="detail-progress" data-accent={shown.accent}><div><span>已经看到</span><strong>{shown.watched_episodes}<small> / {shown.total_episodes || '—'} 话</small></strong></div><button className="button ink" onClick={() => onWatch(shown)}><Icon name="plus" />记一次观看</button></div>
      {shown.tags.length > 0 && <div className="entry-tags">{shown.tags.map(tag => <span key={tag}>{tag}</span>)}</div>}
      {shown.notes && <section className="detail-notes"><h3>我的短评</h3><p>{shown.notes}</p></section>}
      <section className="detail-history"><div className="section-heading"><h3>观看足迹</h3><span>最近 100 条</span></div>{history.isPending ? <p className="muted" role="status">正在读取观看记录…</p> : history.isError ? <p className="error-message" role="alert">{errorMessage(history.error)}</p> : <HistoryList records={history.data.items} />}</section>
      {current.isError && <p className="error-message" role="alert">{errorMessage(current.error)}</p>}
      {remove.isError && <p className="error-message" role="alert">{errorMessage(remove.error)}</p>}
      {confirmDelete && <div className="delete-confirm" role="alert"><strong>删除「{shown.title}」？</strong><p>这部番剧和所有观看记录都会被删除，无法撤销。</p><div><button className="button quiet" onClick={() => setConfirmDelete(false)}>保留记录</button><button className="button danger" onClick={() => remove.mutate()} disabled={remove.isPending}>{remove.isPending ? '正在删除…' : '确认删除'}</button></div></div>}
    </div>
    <footer className="dialog-footer detail-footer"><button type="button" className="text-button danger-text" onClick={() => setConfirmDelete(true)} disabled={remove.isPending}><Icon name="trash" />删除番剧</button><button type="button" className="button secondary" onClick={() => onEdit(shown)}><Icon name="edit" />编辑记录</button></footer>
  </Dialog>;
}
