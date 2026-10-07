import { useState } from 'react';
import type { FormEvent } from 'react';
import { useMutation } from '@tanstack/react-query';
import { client, errorMessage, result } from '../api/client';
import type { Entry, WatchRecord } from '../api/client';
import { Dialog } from '../ui/Dialog';
import { Icon } from '../ui/Icon';
import { localDate } from './labels';

export function WatchEditor({ entry, record, onClose, onSaved }: { entry: Entry; record?: WatchRecord; onClose: () => void; onSaved: (message: string) => void }) {
  const [requestID] = useState(() => crypto.randomUUID());
  const [confirmDelete, setConfirmDelete] = useState(false);
  const nextEpisode = entry.total_episodes > 0 && entry.watched_episodes >= entry.total_episodes ? 1 : entry.watched_episodes + 1;
  const save = useMutation({
    mutationFn: async (data: FormData) => {
      const body = {
        watched_on: String(data.get('watched_on')),
        episode_from: Number(data.get('episode_from')),
        episode_to: Number(data.get('episode_to')),
        note: String(data.get('note')).trim(),
        request_id: requestID,
        rewatch: Number(data.get('rewatch')),
      };
      if (record) { const { request_id: _requestID, ...patch } = body; return result(client.PATCH('/api/v1/entries/{id}/history/{record}', { params: { path: { id: entry.id, record: record.id } }, body: { ...patch, version: record.version } })); }
      return result(client.POST('/api/v1/entries/{id}/history', { params: { path: { id: entry.id } }, body }));
    },
    onSuccess: () => onSaved(record ? '观看记录已更正，进度已重新计算' : '这次观看，已经记下了'),
  });
  const remove = useMutation({ mutationFn: () => result(client.DELETE('/api/v1/entries/{id}/history/{record}', { params: { path: { id: entry.id, record: record!.id }, query: { version: record!.version } } })), onSuccess: () => onSaved('观看记录已删除，进度已重新计算') });
  function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); save.mutate(new FormData(event.currentTarget)); }
  return <Dialog title={record ? '编辑观看记录' : '记一次观看'} eyebrow="SAVE THIS MOMENT" onClose={onClose}>
    <div className="watching-title" data-accent={entry.accent}><span className="watching-icon"><Icon name="play" /></span><div><strong>{entry.title}</strong><span>当前看到第 {entry.watched_episodes} 话{entry.total_episodes > 0 ? ` / 共 ${entry.total_episodes} 话` : ''}</span></div></div>
    <form className="editor-form" onSubmit={submit}>
      <fieldset disabled={save.isPending || remove.isPending}>
        <label>观看日期<input name="watched_on" type="date" defaultValue={record?.watched_on || localDate()} min="1900-01-01" max="2100-12-31" required data-initial-focus /></label>
        <div className="form-row"><label>从第几话<input name="episode_from" type="number" min="1" max={entry.total_episodes || 10000} step="1" defaultValue={record?.episode_from || nextEpisode} required /></label><label>看到第几话<input name="episode_to" type="number" min="1" max={entry.total_episodes || 10000} step="1" defaultValue={record?.episode_to || nextEpisode} required /></label></div>
        <label>第几次观看<input name="rewatch" type="number" min="1" max="1000" step="1" defaultValue={record?.rewatch || 1} required /><span className="field-hint">1 为首刷，2 为二刷，以此类推。</span></label>
        <label>此刻的感想 <span className="optional">选填</span><textarea name="note" defaultValue={record?.note} placeholder="记下这一话的心情，也可以只留下观看进度。" maxLength={2000} rows={4} /></label>
        <p className="form-note">{record ? '更正或删除后，会根据剩余观看记录重新计算进度。' : '进度会更新到看过的最远一话，重看不会减少进度。'}</p>
      </fieldset>
      {(save.error || remove.error) && <p className="error-message" role="alert">{errorMessage(save.error || remove.error)}</p>}
      {record && <div className="history-delete"><button type="button" className="text-button danger-text" disabled={save.isPending || remove.isPending} onClick={() => setConfirmDelete(true)}>删除这条观看记录</button>{confirmDelete && <div className="delete-confirm"><p>删除后无法撤销，番剧会保留。</p><button type="button" className="button quiet" onClick={() => setConfirmDelete(false)}>保留记录</button><button type="button" className="button danger" disabled={save.isPending || remove.isPending} onClick={() => remove.mutate()}>确认删除观看记录</button></div>}</div>}
      <footer className="dialog-footer"><button type="button" className="button quiet" onClick={onClose}>取消</button><button className="button primary" disabled={save.isPending || remove.isPending}>{save.isPending ? '正在记录…' : record ? '保存观看修改' : '保存这次观看'}<Icon name="check" /></button></footer>
    </form>
  </Dialog>;
}
