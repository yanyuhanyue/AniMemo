import { useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { Icon } from '../components/ui/Icon';
import { ApiError, client, result } from '../api/client';
import { Pager, Problem } from '../public/Public';
import { auditLabel, useAudit } from './data';
import { SiteCard, siteImageURL, useSite, type SiteSettings as SiteData } from '../components/SiteIdentity';

export function SiteSettings() {
  const q = useSite();
  if (q.isPending) return <div className="admin-empty" role="status">正在读取设置…</div>;
  if (q.error) return <Problem error={q.error} />;
  return <SiteSettingsEditor initial={q.data} />;
}
function SiteSettingsEditor({ initial }: { initial: SiteData }) {
  const cache = useQueryClient();
  const [draft, setDraft] = useState(initial);
  const [saved, setSaved] = useState(false);
  const save = useMutation({ mutationFn: () => result(client.PUT('/api/v1/admin/site', { body: draft })), onSuccess: data => { cache.setQueryData(['site'], data); setDraft(data); setSaved(true); } });
  const image = useMutation({
    mutationFn: ({ kind, file }: { kind: 'icon' | 'cover'; file: File | null }) => {
      const params = { path: { kind }, query: { version: draft.version } };
      if (!file) return result(client.DELETE('/api/v1/admin/site/images/{kind}', { params }));
      if (!['image/jpeg', 'image/png'].includes(file.type)) throw new ApiError(415, 'unsupported_media_type', '请选择 JPG 或 PNG 图片。');
      if (file.size > 2 * 1024 * 1024) throw new ApiError(413, 'cover_too_large', '图片不能超过 2 MB。');
      return result(client.PUT('/api/v1/admin/site/images/{kind}', { params, headers: { 'Content-Type': file.type }, body: '', bodySerializer: () => file }));
    },
    onSuccess: data => {
      cache.setQueryData(['site'], data);
      setDraft(previous => ({ ...previous, version: data.version, icon_revision: data.icon_revision, cover_revision: data.cover_revision }));
    },
  });
  const busy = save.isPending || image.isPending;
  function set<K extends keyof SiteData>(key: K, value: SiteData[K]) { setDraft(previous => ({ ...previous, [key]: value })); setSaved(false); }
  return <div className="admin-columns admin-settings-layout">
    <form className="admin-panel admin-settings-form" onSubmit={e => { e.preventDefault(); save.mutate(); }}>
      <div className="admin-panel-heading"><div><h2>基本信息</h2><p>展示给访客的站点名片</p></div><span className="admin-tile-icon purple"><Icon name="settings" /></span></div>
      <fieldset disabled={busy} className="admin-fields"><label>站点名称<Input name="name" value={draft.name} onChange={e => set('name', e.target.value)} maxLength={60} required /><small>用于站点导航与浏览器标签页。</small></label><label>站点简介<textarea name="description" value={draft.description} onChange={e => set('description', e.target.value)} maxLength={400} rows={4} placeholder="向同好们介绍一下这里吧…" /><small>最多 400 个字符，显示在站点名片中。</small></label>
        <SiteImageField kind="icon" title="网站图标" revision={draft.icon_revision} disabled={busy} onChange={file => image.mutate({ kind: 'icon', file })} />
        <SiteImageField kind="cover" title="名片封面" revision={draft.cover_revision} disabled={busy} onChange={file => image.mutate({ kind: 'cover', file })} />
        {image.isPending && <p role="status">正在保存图片…</p>}{image.isSuccess && <p role="status">图片已更新。</p>}{image.error && <Problem error={image.error} />}
        <label>站点首页主人<Input name="homepage_owner_slug" value={draft.homepage_owner_slug} onChange={e => set('homepage_owner_slug', e.target.value)} maxLength={36} placeholder="公开手账地址 /u/ 后的标识"/><small>只展示指定主人的公开番剧。留空时展示站点名片。<a href="/home" target="_blank" rel="noreferrer">预览站点首页 ↗</a></small></label><div className="admin-section-divider"><h3>加入方式</h3><label className="admin-checkbox-row"><span><strong>开放注册</strong><small>允许新用户自行创建账号，关闭后已有用户仍可登录。</small></span><Input type="checkbox" name="registration_open" checked={draft.registration_open} onChange={e => set('registration_open', e.target.checked)} /></label></div>
      </fieldset>
      <div className="admin-settings-footer"><span role="status">{saved ? '设置已保存' : '文字与加入方式保存后生效'}</span><Button className="button primary" disabled={busy}><Icon name="check" />{save.isPending ? '正在保存…' : '保存站点设置'}</Button></div>{save.error && <Problem error={save.error} />}
    </form>
    <aside className="admin-stack"><SiteCard site={draft} heading="h2" /><p className="admin-help">名片预览 · 文字保存后显示在站点首页。</p></aside>
  </div>;
}
function SiteImageField({ kind, title, revision, disabled, onChange }: { kind: 'icon' | 'cover'; title: string; revision?: string; disabled: boolean; onChange: (file: File | null) => void }) {
  const input = useRef<HTMLInputElement>(null);
  return <section className="admin-branding-field" aria-label={title}>
    <div><h3>{title}</h3><p>{kind === 'icon' ? '建议正方形，用于浏览器标签页和站点标识。' : '建议横向图片，用于站点名片背景。'} JPG / PNG，最多 2 MB；上传后立即生效。</p></div>
    {revision && <img className={`admin-branding-thumbnail ${kind}`} src={siteImageURL(kind, revision)} alt={`当前${title}`} />}
    <div className="management-actions"><Input ref={input} hidden type="file" accept="image/jpeg,image/png" aria-label={`选择${title}`} disabled={disabled} onChange={e => { const file = e.target.files?.[0]; e.target.value = ''; if (file) onChange(file); }} /><Button type="button" className="button secondary" disabled={disabled} onClick={() => input.current?.click()}>{revision ? '更换' : '上传'}{title}</Button>{revision && <Button type="button" className="text-button" disabled={disabled} onClick={() => onChange(null)}>恢复默认{title}</Button>}</div>
  </section>;
}
export function Audit() {
  const [page, setPage] = useState(1);
  const q = useAudit(page);
  const kindLabel: Record<string, string> = { user: '用户', plugin: '扩展', site: '站点', instance: '实例', entry: '番剧', column: '专栏', tag: '标签' };
  return <section className="admin-panel"><div className="admin-panel-heading"><div><h2>管理操作记录{q.data && <span className="admin-count">{q.data.total}</span>}</h2><p>按时间倒序排列，展开可查看操作对象与详情</p></div><Icon name="clock" /></div>{q.isPending ? <div className="admin-empty" role="status">正在读取审计…</div> : q.error ? <Problem error={q.error} /> : <><p className="admin-table-hint">左右滑动查看完整表格</p><div className="admin-table-wrap" tabIndex={0} role="region" aria-label="审计记录表格"><table className="admin-table admin-audit-table"><thead><tr><th>操作</th><th>操作人</th><th>时间</th><th>记录详情</th></tr></thead><tbody>{q.data.items.map(event => <tr key={event.id}><td><strong>{auditLabel(event.action)}</strong><small>{kindLabel[event.kind] || event.kind}</small></td><td>{event.actor || '已注销账号'}</td><td><time dateTime={event.created_at}>{new Date(event.created_at).toLocaleString('zh-CN', { hour12: false })}</time></td><td><details><summary>展开详情</summary><div className="admin-audit-detail"><p>对象：{event.target}</p><pre>{JSON.stringify(event.detail, null, 2)}</pre></div></details></td></tr>)}</tbody></table></div>{!q.data.items.length && <div className="admin-empty">暂无管理操作记录。</div>}<Pager page={page} total={q.data.total} size={30} onChange={setPage} /></>}</section>;
}
