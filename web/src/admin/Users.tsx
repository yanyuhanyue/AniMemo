import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { Dialog } from '../components/ui/Dialog';
import { Icon } from '../components/ui/Icon';
import { client, result } from '../api/client';
import type { components } from '../api/schema';
import { Pager, Problem } from '../public/Public';
import { auditLabel } from './data';

type User = components['schemas']['AdminUser'];
type Action = components['schemas']['AdminAction']['action'];
const publicStates = { private: '未公开', pending: '公开待审', published: '已公开', rejected: '已拒绝 / 下架' };
function actionsFor(user: User): Action[] {
  return [user.is_admin ? 'remove-admin' : 'grant-admin', user.disabled ? 'enable' : 'disable', 'revoke-sessions', ...(user.public_state === 'pending' ? ['approve-public', 'reject-public'] as const : []), ...(user.public_state === 'published' ? ['hide-public'] as const : [])];
}
export function Users() {
  const cache = useQueryClient();
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState('');
  const [state, setState] = useState<'' | 'pending' | 'published' | 'disabled'>('');
  const [selected, setSelected] = useState<User | null>(null);
  const [action, setAction] = useState<Action | ''>('');
  const q = useQuery({ queryKey: ['admin', 'users', search, state, page], queryFn: ({ signal }) => result(client.GET('/api/v1/admin/users', { signal, params: { query: { search, state, page } } })) });
  const apply = useMutation({
    mutationFn: (form: FormData) => {
      if (!selected || !action) throw new Error('请选择账号和管理操作');
      return result(client.POST('/api/v1/admin/users/{id}', { params: { path: { id: selected.id } }, body: { action, version: selected.version, reason: String(form.get('reason') || '') } }));
    },
    onSuccess: async () => { setSelected(null); await cache.invalidateQueries({ queryKey: ['admin'] }); await cache.invalidateQueries({ queryKey: ['session'] }); },
  });
  return <>
    <section className="admin-panel">
      <div className="admin-panel-heading"><div><h2>用户列表{q.data && <span className="admin-count">{q.data.total}</span>}</h2><p>账号状态与公开权限，一目了然</p></div><span className="admin-badge purple">权限变更留有记录</span></div>
      <div className="admin-toolbar"><label className="admin-search"><Icon name="search" /><Input aria-label="搜索用户" placeholder="搜索昵称或邮箱…" value={search} maxLength={160} onChange={e => { setSearch(e.target.value); setPage(1); }} /></label><label className="admin-filter"><span>账号筛选</span><select aria-label="账号筛选" value={state} onChange={e => { setState(e.target.value as typeof state); setPage(1); }}><option value="">全部用户</option><option value="pending">待审核公开申请</option><option value="published">已公开</option><option value="disabled">已停用</option></select></label></div>
      {q.isPending ? <div className="admin-empty" role="status">正在读取用户…</div> : q.error ? <Problem error={q.error} /> : <><p className="admin-table-hint">左右滑动查看完整表格</p><div className="admin-table-wrap" tabIndex={0} role="region" aria-label="用户列表表格"><table className="admin-table"><thead><tr><th>用户</th><th>角色</th><th>账号状态</th><th>公开手账</th><th className="admin-align-right">操作</th></tr></thead><tbody>{q.data.items.map(user => <tr key={user.id}><td><div className="admin-person"><span className={`admin-avatar ${user.is_admin ? 'purple' : 'pink'}`}>{user.display_name.slice(0, 1)}</span><div><strong>{user.display_name}</strong><span>{user.email}</span></div></div></td><td><span className={`admin-badge ${user.is_admin ? 'purple' : 'neutral'}`}>{user.is_admin ? '管理员' : '普通用户'}</span></td><td><span className={`admin-status ${user.disabled ? 'red' : 'green'}`}>{user.disabled ? '已停用' : '正常'}</span></td><td><span className={`admin-badge ${user.public_state === 'pending' ? 'amber' : user.public_state === 'published' ? 'green' : 'neutral'}`}>{publicStates[user.public_state]}</span></td><td className="admin-align-right"><Button className="button secondary" aria-label={`管理账号 ${user.display_name}`} onClick={() => { apply.reset(); setAction(''); setSelected(user); }}>管理账号<Icon name="chevron" /></Button></td></tr>)}</tbody></table></div>{!q.data.items.length && <div className="admin-empty"><Icon name="users" /><h3>没有符合条件的用户</h3><p>试试其他昵称、邮箱或筛选条件。</p></div>}<Pager page={page} total={q.data.total} size={20} onChange={setPage} /></>}
    </section>
    <p className="admin-help"><Icon name="activity" />停用、角色变更或撤销登录后，账号会退出所有设备。</p>
    {selected && <Dialog title={`管理账号 · ${selected.display_name}`} eyebrow="ACCOUNT MANAGEMENT" onClose={() => { if (!apply.isPending) setSelected(null); }}><form className="editor-form admin-dialog-form" onSubmit={e => { e.preventDefault(); apply.mutate(new FormData(e.currentTarget)); }}><p>{selected.email}</p>{selected.bio && <p>{selected.bio}</p>}{selected.public_reason && <p>审核说明：{selected.public_reason}</p>}<label>管理操作<select aria-label="管理操作" disabled={apply.isPending} data-initial-focus value={action} onChange={e => { setAction(e.target.value as Action); apply.reset(); }} required><option value="">请选择操作</option>{actionsFor(selected).map(item => <option value={item} key={item}>{auditLabel(item)}</option>)}</select></label><label>操作原因{['reject-public', 'hide-public'].includes(action) ? '（必填）' : '（选填）'}<textarea disabled={apply.isPending} name="reason" maxLength={400} required={['reject-public', 'hide-public'].includes(action)} placeholder="填写说明，便于之后追溯" /></label>{action && <p className="admin-callout">即将执行「{auditLabel(action)}」。权限变更、停用和撤销登录会让账号退出所有设备；操作会写入审计记录。</p>}<div className="admin-form-actions"><Button type="button" className="button secondary" disabled={apply.isPending} onClick={() => setSelected(null)}>取消</Button><Button className={`button ${['disable', 'remove-admin', 'hide-public'].includes(action) ? 'danger' : 'primary'}`} disabled={!action || apply.isPending}>{apply.isPending ? '正在执行…' : '确认执行'}</Button></div>{apply.error && <Problem error={apply.error} />}</form></Dialog>}
  </>;
}
