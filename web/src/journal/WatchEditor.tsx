import { useState } from 'react';
import type { FormEvent } from 'react';
import { useMutation } from '@tanstack/react-query';
import { client, errorMessage, result } from '../api/client';
import type { Entry } from '../api/client';
import { Dialog } from '../ui/Dialog';
import { Icon } from '../ui/Icon';
import { localDate } from './labels';

export function WatchEditor({ entry, onClose, onSaved }: { entry: Entry; onClose: () => void; onSaved: (message: string) => void }) {
  const [requestID] = useState(() => crypto.randomUUID());
  const nextEpisode = entry.total_episodes > 0 && entry.watched_episodes >= entry.total_episodes ? 1 : entry.watched_episodes + 1;
  const save = useMutation({
    mutationFn: async (data: FormData) => {
      return result(client.POST('/api/v1/entries/{id}/history', { params: { path: { id: entry.id } }, body: {
        watched_on: String(data.get('watched_on')),
        episode_from: Number(data.get('episode_from')),
        episode_to: Number(data.get('episode_to')),
        note: String(data.get('note')).trim(),
        request_id: requestID,
      } }));
    },
    onSuccess: () => onSaved('这次观看，已经记下了'),
  });
  function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); save.mutate(new FormData(event.currentTarget)); }
  return <Dialog title="记一次观看" eyebrow="SAVE THIS MOMENT" onClose={onClose}>
    <div className="watching-title" data-accent={entry.accent}><span className="watching-icon"><Icon name="play" /></span><div><strong>{entry.title}</strong><span>当前看到第 {entry.watched_episodes} 话{entry.total_episodes > 0 ? ` / 共 ${entry.total_episodes} 话` : ''}</span></div></div>
    <form className="editor-form" onSubmit={submit}>
      <fieldset disabled={save.isPending}>
        <label>观看日期<input name="watched_on" type="date" defaultValue={localDate()} min="1900-01-01" max="2100-12-31" required data-initial-focus /></label>
        <div className="form-row"><label>从第几话<input name="episode_from" type="number" min="1" max={entry.total_episodes || 10000} step="1" defaultValue={nextEpisode} required /></label><label>看到第几话<input name="episode_to" type="number" min="1" max={entry.total_episodes || 10000} step="1" defaultValue={nextEpisode} required /></label></div>
        <label>此刻的感想 <span className="optional">选填</span><textarea name="note" placeholder="记下这一话的心情，也可以只留下观看进度。" maxLength={2000} rows={4} /></label>
        <p className="form-note">进度会更新到看过的最远一话，重看不会减少进度。</p>
      </fieldset>
      {save.isError && <p className="error-message" role="alert">{errorMessage(save.error)}</p>}
      <footer className="dialog-footer"><button type="button" className="button quiet" onClick={onClose}>取消</button><button className="button primary" disabled={save.isPending}>{save.isPending ? '正在记录…' : '保存这次观看'}<Icon name="check" /></button></footer>
    </form>
  </Dialog>;
}
