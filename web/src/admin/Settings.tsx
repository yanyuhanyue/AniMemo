import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { Icon } from '../components/ui/Icon';
import { client, result } from '../api/client';
import { Pager, Problem } from '../public/Public';
import { auditLabel, useAudit } from './data';

export function SiteSettings() {
  const cache = useQueryClient();
  const q = useQuery({ queryKey: ['site'], queryFn: ({ signal }) => result(client.GET('/api/v1/site', { signal })) });
  const [saved, setSaved] = useState(false);
  const save = useMutation({ mutationFn: (form: FormData) => result(client.PUT('/api/v1/admin/site', { body: { name: String(form.get('name')), description: String(form.get('description')), registration_open: form.has('registration_open'), homepage_owner_slug: String(form.get('homepage_owner_slug')), version: Number(form.get('version')) } })), onSuccess: data => { cache.setQueryData(['site'], data); setSaved(true); } });
  if (q.isPending) return <div className="admin-empty" role="status">正在读取设置…</div>;
  if (q.error) return <Problem error={q.error} />;
  return <div className="admin-columns admin-settings-layout">
    <form key={q.data.version} className="admin-panel admin-settings-form" onChange={() => setSaved(false)} onSubmit={e => { e.preventDefault(); save.mutate(new FormData(e.currentTarget)); }}>
      <Input type="hidden" name="version" value={q.data.version} />
      <div className="admin-panel-heading"><div><h2>基本信息</h2><p>展示给访客的站点名片</p></div><span className="admin-tile-icon purple"><Icon name="settings" /></span></div>
      <fieldset disabled={save.isPending} className="admin-fields"><label>站点名称<Input name="name" defaultValue={q.data.name} maxLength={60} required /><small>最多 60 个字符，建议使用简短易记的名字。</small></label><label>站点简介<textarea name="description" defaultValue={q.data.description} maxLength={400} rows={4} placeholder="向同好们介绍一下这里吧…" /><small>最多 400 个字符。</small></label>
        <label>站点首页主人<Input name="homepage_owner_slug" defaultValue={q.data.homepage_owner_slug} maxLength={36} placeholder="公开手账地址 /u/ 后的标识"/><small>只展示指定主人的公开番剧。留空显示空首页，不自动选取管理员。<a href="/home" target="_blank" rel="noreferrer">预览站点首页 ↗</a></small></label><div className="admin-section-divider"><h3>加入方式</h3><label className="admin-checkbox-row"><span><strong>开放注册</strong><small>允许新用户自行创建账号，关闭后已有用户仍可登录。</small></span><Input type="checkbox" name="registration_open" defaultChecked={q.data.registration_open} /></label></div>
      </fieldset>
      <div className="admin-settings-footer"><span role="status">{saved ? '设置已保存' : '修改将在保存后生效'}</span><Button className="button primary" disabled={save.isPending}><Icon name="check" />{save.isPending ? '正在保存…' : '保存站点设置'}</Button></div>{save.error && <Problem error={save.error} />}
    </form>
    <aside className="admin-stack"><section className="admin-panel admin-site-preview"><div className="admin-site-cover" /><div className="admin-site-copy"><span className="admin-brand-icon"><Icon name="sparkle" /></span><p className="admin-eyebrow">当前站点名片</p><h2>{q.data.name}</h2><p>{q.data.description || '还没有填写站点简介。'}</p><span className={`admin-badge ${q.data.registration_open ? 'green' : 'neutral'}`}>{q.data.registration_open ? '欢迎新朋友加入' : '暂未开放注册'}</span></div></section><p className="admin-help">这里展示当前已保存的信息。</p></aside>
  </div>;
}
export function Audit() {
  const [page, setPage] = useState(1);
  const q = useAudit(page);
  const kindLabel: Record<string, string> = { user: '用户', plugin: '扩展', site: '站点', instance: '实例', entry: '番剧', column: '专栏', tag: '标签' };
  return <section className="admin-panel"><div className="admin-panel-heading"><div><h2>管理操作记录{q.data && <span className="admin-count">{q.data.total}</span>}</h2><p>按时间倒序排列，展开可查看操作对象与详情</p></div><Icon name="clock" /></div>{q.isPending ? <div className="admin-empty" role="status">正在读取审计…</div> : q.error ? <Problem error={q.error} /> : <><p className="admin-table-hint">左右滑动查看完整表格</p><div className="admin-table-wrap" tabIndex={0} role="region" aria-label="审计记录表格"><table className="admin-table admin-audit-table"><thead><tr><th>操作</th><th>操作人</th><th>时间</th><th>记录详情</th></tr></thead><tbody>{q.data.items.map(event => <tr key={event.id}><td><strong>{auditLabel(event.action)}</strong><small>{kindLabel[event.kind] || event.kind}</small></td><td>{event.actor || '已注销账号'}</td><td><time dateTime={event.created_at}>{new Date(event.created_at).toLocaleString('zh-CN', { hour12: false })}</time></td><td><details><summary>展开详情</summary><div className="admin-audit-detail"><p>对象：{event.target}</p><pre>{JSON.stringify(event.detail, null, 2)}</pre></div></details></td></tr>)}</tbody></table></div>{!q.data.items.length && <div className="admin-empty">暂无管理操作记录。</div>}<Pager page={page} total={q.data.total} size={30} onChange={setPage} /></>}</section>;
}
