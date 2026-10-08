import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { TwoFactor } from './TwoFactor';
import { EmailRequest } from './EmailAccess';
import { useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import type { User } from '../api/client';
import { ApiError, client, errorMessage, result } from '../api/client';
import { changeAccount } from './session';
import { Dialog } from '../components/ui/Dialog';
import { accentLabels } from '../journal/labels';

export function Settings({ user, onClose }: { user: User; onClose: () => void }) {
  const cache = useQueryClient();
  const [shown, setShown] = useState(user);
  const [message, setMessage] = useState('');
  const [deleteConfirmation, setDeleteConfirmation] = useState('');
  const avatarInput = useRef<HTMLInputElement>(null);
  async function updated(next: User) {
    setShown(next);
    // A late settings response must never sign a logged-out or switched user back in.
    if (cache.getQueryData<User | null>(['session'])?.id === next.id) cache.setQueryData(['session'], next);
    setMessage('设置已保存');
  }
  const profile = useMutation({ mutationFn: (form: FormData) => result(client.PUT('/api/v1/settings', { body: {
    version: shown.version, display_name: String(form.get('display_name')), bio: String(form.get('bio')),
    accent: String(form.get('accent')) as User['accent'], default_view: String(form.get('default_view')) as User['default_view'],
  } })), onSuccess: updated });
  const avatar = useMutation({ mutationFn: (file: File | null) => {
    const params = { query: { version: shown.version } };
    if (!file) return result(client.DELETE('/api/v1/avatar', { params }));
    if (!['image/jpeg', 'image/png'].includes(file.type) || file.size > 2 * 1024 * 1024) throw new ApiError(400, 'validation_error', '头像需要 JPG / PNG 图片，最多 2 MB。');
    return result(client.PUT('/api/v1/avatar', { params, headers: { 'Content-Type': file.type }, body: '', bodySerializer: () => file }));
  }, onSuccess: updated });
  const password = useMutation({ mutationFn: (form: FormData) => result(client.POST('/api/v1/auth/password', { body: { current_password: String(form.get('current_password')), new_password: String(form.get('new_password')) } })), onSuccess: async next => { await updated(next); setMessage('密码已更新，其他设备已退出登录'); } });
  const logoutAll = useMutation({ mutationFn: () => result(client.POST('/api/v1/auth/logout-all')), onSuccess: () => changeAccount(cache, null) });
  const remove = useMutation({ mutationFn: (form: FormData) => result(client.DELETE('/api/v1/auth/account', { body: { password: String(form.get('password')) } })), onSuccess: () => changeAccount(cache, null) });
  const publication = useMutation({ mutationFn: (action: 'enable-sharing' | 'disable-sharing' | 'request-public' | 'withdraw-public') => result(client.POST('/api/v1/settings/publication', { body: { action, version: shown.version } })), onSuccess: updated });
  const busy = publication.isPending || profile.isPending || avatar.isPending || password.isPending || logoutAll.isPending || remove.isPending;
  const problem = publication.error || profile.error || avatar.error || password.error || logoutAll.error || remove.error;
  return <Dialog title="账号与偏好" eyebrow="YOUR SPACE" onClose={onClose} wide>
    <div className="settings-content">
      <section className="settings-avatar"><span className="avatar large-avatar">{shown.avatar_revision ? <img src={`/api/v1/avatar/${shown.avatar_revision}`} alt="我的头像" /> : Array.from(shown.display_name)[0]}</span><div><strong>{shown.email}</strong><p className="muted">JPG / PNG，最多 2 MB</p><Input ref={avatarInput} type="file" accept="image/jpeg,image/png" aria-label="选择头像" hidden onChange={event => { const file = event.currentTarget.files?.[0]; event.currentTarget.value = ''; if (file) avatar.mutate(file); }} /><Button className="text-button" disabled={busy} onClick={() => avatarInput.current?.click()}>更换头像</Button>{shown.avatar_revision && <Button className="text-button" disabled={busy} onClick={() => avatar.mutate(null)}>移除头像</Button>}</div></section>
      <form className="editor-form compact-form" onSubmit={event => { event.preventDefault(); profile.mutate(new FormData(event.currentTarget)); }}><fieldset disabled={busy}>
        <label>昵称<Input name="display_name" defaultValue={shown.display_name} maxLength={32} required /></label>
        <label>个人简介<textarea name="bio" defaultValue={shown.bio} maxLength={240} rows={2} /></label>
        <div className="form-row"><label>强调色<select name="accent" defaultValue={shown.accent}>{Object.entries(accentLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label>默认视图<select name="default_view" defaultValue={shown.default_view}><option value="cards">海报卡片</option><option value="list">列表</option></select></label></div>
        <Button className="button primary">保存个人设置</Button>
      </fieldset></form>
      <section className="settings-section"><h3>分享与公开手账</h3><p>开启后，分享内容会展示昵称和简介。私密番剧仍仅自己可见；公开手账审核通过后会展示头像，并收录所有标为「公开」的番剧。</p><p>当前：{shown.sharing_enabled ? '分享已开启' : '分享已关闭'} · {{ private: '手账未公开', pending: '公开申请审核中', published: '手账已公开', rejected: '公开申请未通过 / 已下架' }[shown.public_state]}</p>{shown.public_reason && <p>{shown.public_reason}</p>}<div className="management-actions"><Button className="button secondary" disabled={busy} onClick={() => publication.mutate(shown.sharing_enabled ? 'disable-sharing' : 'enable-sharing')}>{shown.sharing_enabled ? '关闭全部分享' : '开启链接分享'}</Button>{shown.public_state === 'pending' || shown.public_state === 'published' ? <Button className="button secondary" disabled={busy} onClick={() => publication.mutate('withdraw-public')}>撤回公开手账</Button> : <Button className="button secondary" disabled={busy} onClick={() => publication.mutate('request-public')}>申请公开手账</Button>}{shown.public_state === 'published' && <a className="button quiet" href={`/u/${shown.public_slug}`}>查看公开手账</a>}</div></section>
      <details className="settings-section"><summary>修改密码</summary><form className="editor-form compact-form" onSubmit={event => { event.preventDefault(); password.mutate(new FormData(event.currentTarget)); }}><fieldset disabled={busy}><label>当前密码<Input name="current_password" type="password" autoComplete="current-password" required /></label><label>新密码<Input name="new_password" type="password" autoComplete="new-password" minLength={12} maxLength={72} required /></label><p className="muted">更新后，其他设备会退出登录。</p><Button className="button secondary">更新密码</Button></fieldset></form></details>
      <TwoFactor user={shown} onUpdated={updated} />
      <section className="settings-section"><h3>外部账号</h3><a className="button secondary" href="/connections">Bangumi 账号与收藏同步</a></section>
      <details className="settings-section"><summary>邮箱验证 · {shown.email_verified ? '已验证' : '未验证'}</summary>{shown.email_verified ? <p>已验证邮箱归属，可用于找回密码。</p> : <EmailRequest purpose="verify" email={shown.email} />}</details>
      <section className="settings-section"><Button className="button secondary" disabled={busy} onClick={() => logoutAll.mutate()}>退出所有设备</Button></section>
      {!shown.is_admin && <details className="settings-section"><summary className="danger-text">注销账号</summary><p>将永久删除账号、番剧、观看记录和图片。请先导出需要保留的数据。</p><form className="editor-form compact-form" onSubmit={event => { event.preventDefault(); if (deleteConfirmation === '删除我的账号') remove.mutate(new FormData(event.currentTarget)); }}><fieldset disabled={busy}><label>输入“删除我的账号”以确认<Input value={deleteConfirmation} onChange={event => setDeleteConfirmation(event.target.value)} /></label><label>确认当前密码<Input name="password" type="password" autoComplete="current-password" required /></label><Button className="button danger" disabled={deleteConfirmation !== '删除我的账号'}>永久注销账号</Button></fieldset></form></details>}
      {problem && <p className="error-message" role="alert">{errorMessage(problem)}</p>}{message && <p role="status">{message}</p>}
    </div>
  </Dialog>;
}
