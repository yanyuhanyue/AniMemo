import { RevisionHistory } from './RevisionHistory';
import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, client, errorMessage, result } from '../api/client';
import type { Entry, WatchRecord } from '../api/client';
import { Dialog } from '../components/ui/Dialog';
import { Icon } from '../components/ui/Icon';
import { HistoryList } from './History';
import { formatLabels, statusLabels } from './labels';
import { CoverImage } from './CoverImage';
import { TagChip } from './ManageTools';
import { ShareControls } from './ShareControls';
import { WatchEditor } from './WatchEditor';
import { SourcePicker } from './SourcePicker';

export function EntryDetail({ entry, userID, onClose, onEdit, onWatch, onDeleted }: { entry: Entry; userID: string; onClose: () => void; onEdit: (entry: Entry) => void; onWatch: (entry: Entry) => void; onDeleted: (message: string) => void }) {
  const [editingRecord, setEditingRecord] = useState<WatchRecord | null>(null);
  const [source, setSource] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmCoverDelete, setConfirmCoverDelete] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const cache = useQueryClient();
  const current = useQuery({ queryKey: ['journal', userID, 'entry', entry.id], queryFn: ({ signal }) => result(client.GET('/api/v1/entries/{id}', { signal, params: { path: { id: entry.id } } })) });
  const history = useQuery({ queryKey: ['journal', userID, 'history', entry.id], queryFn: ({ signal }) => result(client.GET('/api/v1/entries/{id}/history', { signal, params: { path: { id: entry.id } } })) });
  const shown = current.data || entry;
  const cover = useMutation({
    mutationFn: (file: File | null) => {
      const params = { path: { id: entry.id }, query: { version: shown.version } };
      if (!file) return result(client.DELETE('/api/v1/entries/{id}/cover', { params }));
      if (!['image/jpeg', 'image/png'].includes(file.type)) throw new ApiError(415, 'unsupported_media_type', '请选择 JPG 或 PNG 图片。');
      if (file.size > 2 * 1024 * 1024) throw new ApiError(413, 'cover_too_large', '封面不能超过 2 MB。');
      return result(client.PUT('/api/v1/entries/{id}/cover', { params, headers: { 'Content-Type': file.type }, body: '', bodySerializer: () => file }));
    },
    onSuccess: async updated => {
      const queryKey = ['journal', userID, 'entry', entry.id];
      await cache.cancelQueries({ queryKey, exact: true });
      cache.setQueryData(queryKey, updated);
      setConfirmCoverDelete(false);
      await cache.invalidateQueries({ queryKey: ['journal', userID] });
    },
  });
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
  if (editingRecord) return <WatchEditor entry={shown} record={editingRecord} onClose={() => setEditingRecord(null)} onSaved={async () => { setEditingRecord(null); await cache.invalidateQueries({ queryKey: ['journal', userID] }); }} />;
  if (source) return <SourcePicker entry={shown} onClose={() => setSource(false)} onSaved={async () => { setSource(false); await cache.invalidateQueries({ queryKey: ['journal', userID] }); }} />;
  return <Dialog title={shown.title} eyebrow="YOUR ANIME MEMORY" onClose={onClose} wide>
    <div className="detail-content">
      <section className="detail-cover" aria-label="番剧封面">
        <CoverImage entry={shown} />
        <div className="cover-controls">
          <div><h3>番剧封面</h3><p className="muted">遵循番剧可见性 · JPG / PNG，最多 2 MB</p><p className="muted">导出手账不包含封面图片。</p></div>
          <div className="cover-buttons">
            <Input ref={fileInput} type="file" accept="image/jpeg,image/png" aria-label="选择封面图片" hidden disabled={cover.isPending || remove.isPending} onChange={event => { const file = event.currentTarget.files?.[0]; event.currentTarget.value = ''; if (file) cover.mutate(file); }} />
            <Button className="button secondary" disabled={cover.isPending || remove.isPending} onClick={() => fileInput.current?.click()}>{cover.isPending ? '正在保存…' : shown.cover_revision ? '更换封面' : '上传封面'}</Button>
            {shown.cover_revision && <Button className="text-button danger-text" disabled={cover.isPending || remove.isPending} onClick={() => setConfirmCoverDelete(true)}>移除封面</Button>}
          </div>
        </div>
        {cover.isPending && <p className="muted" role="status">正在保存封面…</p>}
        {cover.isError && <p className="error-message" role="alert">{errorMessage(cover.error)}</p>}
        {confirmCoverDelete && <div className="delete-confirm"><strong>移除当前封面？</strong><p>番剧和观看记录会保留。需要恢复封面时，请重新上传图片。</p><div><Button className="button quiet" disabled={cover.isPending} onClick={() => setConfirmCoverDelete(false)}>保留封面</Button><Button className="button danger" disabled={cover.isPending || remove.isPending} onClick={() => cover.mutate(null)}>确认移除</Button></div></div>}
      </section>
      {shown.original_title && <p className="detail-original">{shown.original_title}</p>}
      <div className="detail-badges"><span>{formatLabels[shown.format]}</span><span>{statusLabels[shown.status]}</span>{shown.score !== null && <span className="score-badge"><Icon name="star" />{shown.score} / 10</span>}</div>
      <div className="detail-progress" data-accent={shown.accent}><div><span>已经看到</span><strong>{shown.watched_episodes}<small> / {shown.total_episodes || '—'} 话</small></strong></div><Button className="button ink" disabled={cover.isPending || remove.isPending} onClick={() => onWatch(shown)}><Icon name="plus" />记一次观看</Button></div>
      {shown.tags.length > 0 && <div className="entry-tags">{shown.tags.map(tag => <TagChip key={tag} name={tag} />)}</div>}
      {shown.notes && <section className="detail-notes"><h3>我的短评</h3><p>{shown.notes}</p></section>}
      {(shown.details.studio || shown.details.airing_period || shown.details.description || shown.details.reference_url) && <section className="detail-notes"><h3>作品资料</h3><p>{[shown.details.studio, shown.details.airing_period].filter(Boolean).join(' · ')}</p>{shown.details.description && <p>{shown.details.description}</p>}{shown.details.reference_url && <a href={shown.details.reference_url} target="_blank" rel="noreferrer noopener">查看资料来源 ↗</a>}</section>}
      <ShareControls entry={shown} userID={userID} />
      <section className="settings-section"><h3>资料来源</h3><p>{shown.source ? `已绑定 Bangumi #${shown.source.subject_id}` : '尚未绑定外部资料来源'}</p><Button className="button secondary" disabled={cover.isPending || remove.isPending} onClick={() => setSource(true)}>{shown.source ? '预览并刷新资料' : '搜索并绑定 Bangumi'}</Button></section>
      <section className="settings-section"><h3>与这部作品有关的记忆</h3><p>把长笔记、角色与截图留在独立记忆库。</p><a className="button secondary" href={`/memory?anime_id=${shown.anime_id}#notes`}>翻开作品记忆</a> <a className="button secondary" href={`/memory?anime_id=${shown.anime_id}#episodes`}>长篇与集数</a></section><RevisionHistory entryID={shown.id} userID={userID} version={shown.version} /><section className="detail-history"><div className="section-heading"><h3>观看足迹</h3><span>最近 100 条</span></div>{history.isPending ? <p className="muted" role="status">正在读取观看记录…</p> : history.isError ? <p className="error-message" role="alert">{errorMessage(history.error)}</p> : <HistoryList records={history.data.items} onEdit={record => { if (!cover.isPending && !remove.isPending) setEditingRecord(record); }} />}</section>
      {current.isError && <p className="error-message" role="alert">{errorMessage(current.error)}</p>}
      {remove.isError && <p className="error-message" role="alert">{errorMessage(remove.error)}</p>}
      {confirmDelete && <div className="delete-confirm" role="alert"><strong>删除「{shown.title}」？</strong><p>这部番剧、封面和当前观看清单将被移除；历史记忆仍保存在你的完整导出与实例备份中。</p><div><Button className="button quiet" onClick={() => setConfirmDelete(false)}>保留记录</Button><Button className="button danger" onClick={() => remove.mutate()} disabled={remove.isPending || cover.isPending}>{remove.isPending ? '正在删除…' : '确认删除'}</Button></div></div>}
    </div>
    <footer className="dialog-footer detail-footer"><Button type="button" className="text-button danger-text" onClick={() => setConfirmDelete(true)} disabled={remove.isPending || cover.isPending}><Icon name="trash" />删除番剧</Button><Button type="button" className="button secondary" disabled={remove.isPending || cover.isPending} onClick={() => onEdit(shown)}><Icon name="edit" />编辑记录</Button></footer>
  </Dialog>;
}
