import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { Icon } from '../components/ui/Icon';
import { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { client, errorMessage, result } from '../api/client';
import type { Entry } from '../api/client';
import type { components } from '../api/schema';
import { Dialog } from '../components/ui/Dialog';
import { formatLabels } from './labels';

type Field = components['schemas']['ApplySource']['fields'][number];
type Preview = components['schemas']['SubjectPreview'];
export type SourceChoice = { preview: Preview; fields: Field[]; cover: boolean };
const fields: [Field, string][] = [['title', '番剧名称'], ['original_title', '原名'], ['format', '类型'], ['total_episodes', '总话数'], ['studio', '制作公司'], ['airing_period', '播出日期'], ['description', '作品简介'], ['reference_url', '资料链接']];
function value(source: components['schemas']['SourceMetadata'] | Entry, field: Field): string | number {
  if (field === 'studio' || field === 'airing_period' || field === 'description' || field === 'reference_url') return source.details[field] || '';
  return field === 'format' ? formatLabels[source.format] : source[field];
}

function SourceResults({ items, onSelect }: { items: Preview[]; onSelect: (subject: number) => void }) {
  // The API admits two concurrent cover jobs; reveal one more after each load.
  const [coverCount, setCoverCount] = useState(2);
  const allowed = new Set(items.filter(item => item.has_cover).slice(0, coverCount).map(item => item.metadata.subject_id));
  return <div className="source-results">{items.map(item => <Button className="source-result" key={item.metadata.subject_id} onClick={() => onSelect(item.metadata.subject_id)}>
    <span className="source-result-poster" aria-hidden="true"><Icon name="book" />{allowed.has(item.metadata.subject_id) && <img src={'/api/v1/providers/bangumi/subjects/' + item.metadata.subject_id + '/cover'} alt="" onLoad={() => setCoverCount(count => count + 1)} onError={event => { event.currentTarget.hidden = true; setCoverCount(count => count + 1); }} />}</span>
    <span className="source-result-copy"><strong>{item.metadata.title}</strong>{item.metadata.original_title !== item.metadata.title && <span>{item.metadata.original_title}</span>}<small>{formatLabels[item.metadata.format]} · {item.metadata.details.airing_period || '日期未提供'}{item.metadata.total_episodes > 0 ? ' · ' + item.metadata.total_episodes + ' 话' : ''}</small></span>
    <Icon name="arrow" />
  </Button>)}</div>;
}

export function SourcePicker({ entry, initialQuery, onClose, onSaved, onPick }: {
  entry: Entry | null;
  initialQuery?: string;
  onClose: () => void;
  onSaved: (message: string) => void;
  onPick?: (choice: SourceChoice) => void;
}) {
  const initial = initialQuery ?? entry?.title ?? '';
  const [input, setInput] = useState(initial);
  const [query, setQuery] = useState(initial.trim());
  const [page, setPage] = useState(1);
  const [subject, setSubject] = useState<number | null>(entry?.source?.subject_id || null);
  const search = useQuery({
    queryKey: ['bangumi', 'search', query, page], enabled: !!query && subject === null, retry: false, staleTime: 60000,
    queryFn: ({ signal }) => result(client.GET('/api/v1/providers/bangumi/subjects', { signal, params: { query: { query, page } } })),
  });
  const preview = useQuery({
    queryKey: ['bangumi', 'subject', subject], enabled: subject !== null, retry: false, staleTime: 60000,
    queryFn: ({ signal }) => result(client.GET('/api/v1/providers/bangumi/subjects/{subject}', { signal, params: { path: { subject: subject! } } })),
  });
  const save = useMutation({
    mutationFn: (choice: SourceChoice) => {
      const body = { subject_id: choice.preview.metadata.subject_id, snapshot: choice.preview.snapshot, fields: choice.fields, cover: choice.cover, ...(entry ? { version: entry.version } : {}) };
      return entry
        ? result(client.POST('/api/v1/entries/{id}/source', { params: { path: { id: entry.id } }, body }))
        : result(client.POST('/api/v1/entries/from-bangumi', { body }));
    },
    onSuccess: () => onSaved(entry ? '资料来源与选中字段已更新' : '已从 Bangumi 加入手账'),
  });
  const unbind = useMutation({
    mutationFn: () => result(client.DELETE('/api/v1/entries/{id}/source', { params: { path: { id: entry!.id }, query: { version: entry!.version } } })),
    onSuccess: () => onSaved('已解除资料绑定，番剧记录已保留'),
  });
  const busy = save.isPending || unbind.isPending;

  return <Dialog title={entry ? '作品资料来源' : '从 Bangumi 选择作品'} eyebrow="BANGUMI" onClose={onClose} wide>
    <div className="source-picker">
      {subject === null ? <>
        <p className="source-introduction">{entry ? '搜索并核对作品资料，再选择要补充的内容。' : '选好作品后，封面和资料会填入新建表单。'}</p>
        <form className="source-search" onSubmit={event => {
          event.preventDefault();
          const next = input.trim();
          if (next === query && page === 1) void search.refetch();
          else { setQuery(next); setPage(1); }
        }}>
          <label>搜索动画<Input type="search" value={input} onChange={event => setInput(event.target.value)} maxLength={160} required placeholder="输入作品中文名或原名" data-initial-focus /></label>
          <Button className="button primary" disabled={!input.trim() || search.isFetching}><Icon name="search" />{search.isFetching ? '搜索中…' : '搜索'}</Button>
        </form>
        {!query && <div className="source-empty"><Icon name="search" /><h3>找到你想记下的那部番</h3><p>可以搜索中文名或原名。没有找到，也可以手动填写。</p></div>}
        {search.isFetching && <div className="source-loading" role="status"><span className="spinner" />正在查询 Bangumi…</div>}
        {search.isError && <div className="source-empty"><p className="error-message" role="alert">{errorMessage(search.error)}</p><Button className="button secondary" onClick={() => search.refetch()}>重新搜索</Button></div>}
        {search.data && !search.isFetching && <>
          <p className="source-results-summary">{search.data.total ? '找到 ' + search.data.total + ' 部作品，请选择对应的季度或版本。' : '没有找到匹配的动画。试试原名或更短的关键词。'}</p>
          <SourceResults key={query + ':' + page} items={search.data.items} onSelect={id => { setSubject(id); save.reset(); }} />
          {search.data.total > 12 && <div className="pagination"><Button className="button quiet" disabled={page === 1} onClick={() => setPage(page - 1)}>上一页</Button><span>第 {page} 页</span><Button className="button quiet" disabled={page >= 50 || page * 12 >= search.data.total} onClick={() => setPage(page + 1)}>下一页</Button></div>}
        </>}
        <footer className="dialog-footer source-search-footer"><Button type="button" className="button quiet" onClick={onClose}>{onPick ? '返回手动填写' : '取消'}</Button></footer>
      </> : <>
        <Button className="text-button source-back" disabled={busy} onClick={() => { setSubject(null); save.reset(); }}><Icon name="arrow" />返回搜索结果</Button>
        {preview.isPending && <div className="source-loading" role="status"><span className="spinner" />正在读取作品资料…</div>}
        {preview.isError && <div className="source-empty"><p className="error-message" role="alert">{errorMessage(preview.error)}</p><Button className="button secondary" onClick={() => preview.refetch()}>重试</Button></div>}
        {preview.data && <form key={preview.data.snapshot} onSubmit={event => {
          event.preventDefault();
          const form = new FormData(event.currentTarget);
          const choice = { preview: preview.data!, fields: form.getAll('fields') as Field[], cover: form.has('cover') };
          if (onPick) onPick(choice); else save.mutate(choice);
        }}>
          <div className="source-preview-heading">
            {preview.data.has_cover && <img className="source-preview-poster" src={'/api/v1/providers/bangumi/subjects/' + subject + '/cover'} alt={preview.data.metadata.title + '的封面'} />}
            <div><span className="source-provider-label">Bangumi 作品资料</span><h3>{preview.data.metadata.title}</h3><p className="muted">{preview.data.metadata.original_title}</p><p>{formatLabels[preview.data.metadata.format]} · {preview.data.metadata.details.airing_period || '播出日期未提供'}{preview.data.metadata.total_episodes > 0 ? ' · 全 ' + preview.data.metadata.total_episodes + ' 话' : ''}</p></div>
          </div>
          {preview.data.metadata.details.description && <p className="source-synopsis">{preview.data.metadata.details.description}</p>}
          <p className="source-personal-note">{entry ? '评分、感想、标签和观看记录会保留。' : '只带入作品资料，不会把总话数算成你已经看过的集数。'}</p>
          <fieldset disabled={busy}>
            <details className="source-fields-details" open={entry ? true : undefined}><summary>{entry ? '选择要更新的资料' : '将带入的资料 · 可调整'}</summary><div className="source-fields">
              {fields.map(([field, label]) => <label className="source-field" key={field}><Input type="checkbox" name="fields" value={field} defaultChecked={!entry || !value(entry, field)} /><span><strong>{label}</strong>{entry && <small>当前：{String(value(entry, field) || '未填写')}</small>}<span className="source-value">{String(value(preview.data!.metadata, field) || '未提供')}</span></span></label>)}
            </div></details>
            {preview.data.has_cover && <label className="source-cover-choice"><Input type="checkbox" name="cover" defaultChecked={!entry?.cover_revision} /><span>使用这张封面{entry?.cover_revision && <small>会替换当前封面</small>}</span></label>}
          </fieldset>
          {save.isError && <p className="error-message" role="alert">{errorMessage(save.error)}</p>}
          <footer className="dialog-footer"><Button type="button" className="button quiet" disabled={busy} onClick={onClose}>{onPick ? '返回填写' : '取消'}</Button><Button className="button primary" disabled={busy}>{save.isPending ? '正在保存…' : onPick ? '使用这部作品' : entry ? '确认绑定并更新选中字段' : '确认加入手账'}</Button></footer>
        </form>}
      </>}
      {entry?.source && <details className="settings-section"><summary>解除资料绑定</summary><p>保留现有作品资料和观看记录，解除后可以重新选择来源。</p><Button className="button secondary" disabled={busy} onClick={() => unbind.mutate()}>确认解除绑定</Button>{unbind.isError && <p className="error-message" role="alert">{errorMessage(unbind.error)}</p>}</details>}
    </div>
  </Dialog>;
}
