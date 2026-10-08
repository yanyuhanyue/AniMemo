import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { createContext, useContext, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { client, errorMessage, result } from '../api/client';
import type { Entry, Status } from '../api/client';
import { Dialog } from '../components/ui/Dialog';
import { statusLabels } from './labels';

export const TagColors = createContext<Record<string, string>>({});
export function TagChip({ name }: { name: string }) {
  const color = useContext(TagColors)[name];
  return <span style={color ? { backgroundColor: `color-mix(in srgb, ${color} 22%, white)`, color: '#272238', borderColor: color } : undefined}>{name}</span>;
}

export function TagEditor({ userID, onClose }: { userID: string; onClose: () => void }) {
  const cache = useQueryClient();
  const tags = useQuery({ queryKey: ['journal', userID, 'tags'], queryFn: ({ signal }) => result(client.GET('/api/v1/tags', { signal })) });
  const presets = useQuery({ queryKey: ['presets'], queryFn: ({ signal }) => result(client.GET('/api/v1/presets', { signal })) });
  const applyPreset = useMutation({ mutationFn: (preset: { name: string; color: string }) => result(client.PUT('/api/v1/tags', { body: preset })), onSuccess: () => cache.invalidateQueries({ queryKey: ['journal', userID, 'tags'] }) });
  const save = useMutation({ mutationFn: (form: FormData) => result(client.PUT('/api/v1/tags', { body: { name: String(form.get('name')), color: String(form.get('color')) } })), onSuccess: () => cache.invalidateQueries({ queryKey: ['journal', userID, 'tags'] }) });
  return <Dialog title="标签与颜色" onClose={onClose}><form className="editor-form" onSubmit={event => { event.preventDefault(); save.mutate(new FormData(event.currentTarget)); }}><fieldset disabled={save.isPending}><label>标签名称<Input name="name" list="my-tag-names" maxLength={24} required /></label><datalist id="my-tag-names">{tags.data?.items.map(tag => <option key={tag.name}>{tag.name}</option>)}</datalist><label>标签颜色<Input name="color" type="color" defaultValue="#8974cc" /></label><Button className="button primary">保存标签颜色</Button></fieldset>{(save.error || tags.error) && <p className="error-message" role="alert">{errorMessage(save.error || tags.error)}</p>}<div className="entry-tags">{tags.data?.items.map(tag => <TagChip key={tag.name} name={tag.name} />)}</div><h3>全站标签预设</h3><div className="management-actions">{presets.data?.items.map(p => <Button type="button" className="button secondary" key={p.name} disabled={applyPreset.isPending} onClick={() => applyPreset.mutate({ name: p.name, color: p.color })}>使用 {p.name}</Button>)}</div>{(presets.error || applyPreset.error) && <p className="error-message" role="alert">{errorMessage(presets.error || applyPreset.error)}</p>}</form></Dialog>;
}

export function BulkToolbar({ selected, onSaved, onCancel }: { selected: Entry[]; onSaved: () => void; onCancel: () => void }) {
  const [action, setAction] = useState<'status' | 'tag-add' | 'tag-remove' | 'visibility'>('status');
  const [value, setValue] = useState('recorded');
  const save = useMutation({ mutationFn: () => result(client.POST('/api/v1/entries/bulk', { body: { entries: selected.map(({ id, version }) => ({ id, version })), action, value } })), onSuccess: onSaved });
  return <section className="bulk-toolbar" aria-label="批量操作"><strong>已选 {selected.length} 部</strong><label>操作<select value={action} onChange={event => { setAction(event.target.value as typeof action); setValue(event.target.value === 'status' ? 'recorded' : event.target.value === 'visibility' ? 'private' : ''); }}><option value="status">修改状态</option><option value="tag-add">添加标签</option><option value="tag-remove">移除标签</option><option value="visibility">修改可见性</option></select></label>{action === 'status' ? <label>目标状态<select value={value} onChange={event => setValue(event.target.value)}>{Object.entries(statusLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label> : action === 'visibility' ? <label>可见性<select value={value} onChange={event => setValue(event.target.value)}><option value="private">私密</option><option value="unlisted">持链接可见</option><option value="public">公开</option></select></label> : <label>标签<Input value={value} maxLength={24} onChange={event => setValue(event.target.value)} /></label>}<Button className="button primary" disabled={!selected.length || !value.trim() || save.isPending} onClick={() => save.mutate()}>应用批量修改</Button><Button className="button quiet" disabled={save.isPending} onClick={onCancel}>退出多选</Button>{save.error && <p className="error-message" role="alert">{errorMessage(save.error)}</p>}</section>;
}

export function SavedFilters({ userID, search, status, sort, onApply }: { userID: string; search: string; status: Status | ''; sort: 'updated' | 'title' | 'score'; onApply: (filter: { search: string; status: Status | ''; sort: 'updated' | 'title' | 'score' }) => void }) {
  const [name, setName] = useState('');
  const cache = useQueryClient();
  const queryKey = ['journal', userID, 'filters'];
  const filters = useQuery({ queryKey, queryFn: ({ signal }) => result(client.GET('/api/v1/filters', { signal })) });
  const save = useMutation({ mutationFn: () => result(client.POST('/api/v1/filters', { body: { name, search, status, sort } })), onSuccess: async () => { setName(''); await cache.invalidateQueries({ queryKey }); } });
  const remove = useMutation({ mutationFn: (id: string) => result(client.DELETE('/api/v1/filters/{id}', { params: { path: { id } } })), onSuccess: () => cache.invalidateQueries({ queryKey }) });
  return <details className="saved-filters"><summary>快捷筛选</summary><div className="saved-filter-items">{filters.data?.items.map(filter => <span key={filter.id}><Button className="button quiet" onClick={() => onApply(filter)}>{filter.name}</Button><Button className="icon-button" aria-label={`删除筛选 ${filter.name}`} disabled={remove.isPending} onClick={() => remove.mutate(filter.id)}>×</Button></span>)}</div><form onSubmit={event => { event.preventDefault(); save.mutate(); }}><label>保存当前筛选<Input value={name} onChange={event => setName(event.target.value)} maxLength={40} placeholder="给当前筛选起个名字" required /></label><Button className="button secondary" disabled={save.isPending}>保存筛选</Button></form>{(save.error || remove.error || filters.error) && <p className="error-message" role="alert">{errorMessage(save.error || remove.error || filters.error)}</p>}</details>;
}
