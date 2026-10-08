import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { client, errorMessage, result } from '../api/client';
import type { Entry } from '../api/client';
import type { components } from '../api/schema';
import { Dialog } from '../components/ui/Dialog';
import { formatLabels } from './labels';

type Field = components['schemas']['ApplySource']['fields'][number];
const fields: [Field, string][] = [['title','番剧名称'],['original_title','原名'],['format','类型'],['total_episodes','总话数'],['studio','制作公司'],['airing_period','播出日期'],['description','作品简介'],['reference_url','资料链接']];
function value(source: components['schemas']['SourceMetadata'] | Entry, field: Field): string | number {
  if (field === 'studio' || field === 'airing_period' || field === 'description' || field === 'reference_url') return source.details[field] || '';
  return field === 'format' ? formatLabels[source.format] : source[field];
}

export function SourcePicker({ entry, onClose, onSaved }: { entry: Entry | null; onClose: () => void; onSaved: (message: string) => void }) {
  const [input, setInput] = useState(entry?.title || '');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [subject, setSubject] = useState<number | null>(entry?.source?.subject_id || null);
  const [showImage, setShowImage] = useState(false);
  const search = useQuery({ queryKey: ['bangumi','search',query,page], enabled: !!query && subject === null, retry: false, queryFn: ({ signal }) => result(client.GET('/api/v1/providers/bangumi/subjects', { signal, params: { query: { query, page } } })) });
  const preview = useQuery({ queryKey: ['bangumi','subject',subject], enabled: subject !== null, retry: false, queryFn: ({ signal }) => result(client.GET('/api/v1/providers/bangumi/subjects/{subject}', { signal, params: { path: { subject: subject! } } })) });
  const save = useMutation({ mutationFn: (form: FormData) => {
    const shown = preview.data!;
    const body = { subject_id: shown.metadata.subject_id, snapshot: shown.snapshot, fields: form.getAll('fields') as Field[], cover: form.has('cover'), ...(entry ? { version: entry.version } : {}) };
    return entry ? result(client.POST('/api/v1/entries/{id}/source', { params: { path: { id: entry.id } }, body })) : result(client.POST('/api/v1/entries/from-bangumi', { body }));
  }, onSuccess: () => onSaved(entry ? '资料来源与选中字段已更新' : '已从 Bangumi 加入手账') });
  const unbind = useMutation({ mutationFn: () => result(client.DELETE('/api/v1/entries/{id}/source', { params: { path: { id: entry!.id }, query: { version: entry!.version } } })), onSuccess: () => onSaved('已解除资料绑定，番剧记录已保留') });
  const busy = save.isPending || unbind.isPending;
  return <Dialog title={entry ? '作品资料来源' : '从 Bangumi 加入番剧'} eyebrow="BANGUMI" onClose={onClose} wide>
    <div className="source-picker">
      {subject === null ? <>
        <form className="source-search" onSubmit={event => { event.preventDefault(); setQuery(input.trim()); setPage(1); }}><label>搜索动画<Input type="search" value={input} onChange={event => setInput(event.target.value)} maxLength={160} required placeholder="作品中文名或原名" data-initial-focus /></label><Button className="button primary" disabled={search.isFetching}>搜索</Button></form>
        {search.isFetching && <p role="status">正在查询 Bangumi…</p>}
        {search.isError && <p className="error-message" role="alert">{errorMessage(search.error)}</p>}
        {search.data && <><p className="muted">找到 {search.data.total} 项，选择作品后核对资料。</p><div className="source-results">{search.data.items.map(item => <Button className="source-result" key={item.metadata.subject_id} onClick={() => { setSubject(item.metadata.subject_id); setShowImage(false); save.reset(); }}><strong>{item.metadata.title}</strong><span>{item.metadata.original_title}</span><small>{formatLabels[item.metadata.format]} · {item.metadata.details.airing_period || '日期未定'} · {item.metadata.total_episodes || '未知'} 话</small></Button>)}</div><div className="pagination"><Button className="button quiet" disabled={page === 1 || search.isFetching} onClick={() => setPage(page - 1)}>上一页</Button><span>第 {page} 页</span><Button className="button quiet" disabled={page >= 50 || page * 12 >= search.data.total || search.isFetching} onClick={() => setPage(page + 1)}>下一页</Button></div></>}
      </> : <>
        <Button className="text-button" disabled={busy} onClick={() => { setSubject(null); setQuery(entry?.title || query); save.reset(); }}>← 选择其他作品</Button>
        {preview.isPending && <p role="status">正在读取作品资料…</p>}
        {preview.isError && <p className="error-message" role="alert">{errorMessage(preview.error)} <Button className="text-button" onClick={() => preview.refetch()}>重试</Button></p>}
        {preview.data && <form key={preview.data.snapshot} onSubmit={event => { event.preventDefault(); save.mutate(new FormData(event.currentTarget)); }}>
          <h3>{preview.data.metadata.title}</h3><p className="muted">{entry ? '勾选需要更新的字段；评分、短评、标签和观看进度会保留。' : '核对资料后记为“看过，细节未记”，仅自己可见；不会补写观看日期或集数。'}</p>
          <fieldset disabled={busy} className="source-fields">{fields.map(([field,label]) => <label className="source-field" key={field}><Input type="checkbox" name="fields" value={field} defaultChecked={!entry || !value(entry,field)} /><span><strong>{label}</strong>{entry && <small>当前：{String(value(entry,field) || '未填写')}</small>}<span className="source-value">{String(value(preview.data!.metadata,field) || '未提供')}</span></span></label>)}
            {preview.data.has_cover && <><label className="source-field"><Input type="checkbox" name="cover" defaultChecked={!entry?.cover_revision} /><span>采用 Bangumi 封面{entry?.cover_revision && <small>将替换当前封面</small>}</span></label><Button className="text-button" type="button" onClick={() => setShowImage(!showImage)}>{showImage ? '收起封面' : '预览封面'}</Button>{showImage && <img className="source-cover" src={`/api/v1/providers/bangumi/subjects/${subject}/cover`} alt="Bangumi 提供的封面" />}</>}
          </fieldset>
          {save.isError && <p className="error-message" role="alert">{errorMessage(save.error)}</p>}
          <footer className="dialog-footer"><Button type="button" className="button quiet" disabled={busy} onClick={onClose}>取消</Button><Button className="button primary" disabled={busy}>{save.isPending ? '正在保存…' : entry ? '确认绑定并更新选中字段' : '确认加入手账'}</Button></footer>
        </form>}
      </>}
      {entry?.source && <details className="settings-section"><summary>解除资料绑定</summary><p>保留现有作品资料和观看记录，解除后可以重新选择来源。</p><Button className="button secondary" disabled={busy} onClick={() => unbind.mutate()}>确认解除绑定</Button>{unbind.isError && <p className="error-message" role="alert">{errorMessage(unbind.error)}</p>}</details>}
    </div>
  </Dialog>;
}
