import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { client, errorMessage, result } from '../api/client';
import { PageShell } from '../public/Public';
import { changeAccount } from './session';

export function EmailRequest({ purpose, email = '', onBack }: { purpose: 'verify' | 'reset'; email?: string; onBack?: () => void }) {
  const send = useMutation({ mutationFn: (address: string) => result(client.POST('/api/v1/auth/email/request', { body: { email: address, purpose } })) });
  return <form className="editor-form compact-form" onSubmit={event => { event.preventDefault(); send.mutate(String(new FormData(event.currentTarget).get('email'))); }}>
    <h3>{purpose === 'reset' ? '找回密码' : '重新发送验证邮件'}</h3>
    <label>邮箱地址<Input name="email" type="email" autoComplete="email" defaultValue={email} maxLength={254} required /></label>
    <p className="muted">通过邮件中的链接设置密码。开启两步验证的账号还需要验证码或恢复码。</p>
    {send.isError && <p className="error-message" role="alert">{errorMessage(send.error)}</p>}
    {send.data && <p role="status">{send.data.message}</p>}
    <div className="management-actions"><Button className="button primary" disabled={send.isPending}>{send.isPending ? '正在申请…' : '发送邮件'}</Button>{onBack && <Button className="button quiet" type="button" onClick={onBack}>返回登录</Button>}</div>
  </form>;
}

export function EmailConfirmation({ purpose }: { purpose: 'verify' | 'reset' }) {
  const cache = useQueryClient();
  // Fragments are never sent in HTTP requests; retain the token only in this page's memory.
  const [token] = useState(() => new URLSearchParams(window.location.hash.slice(1)).get('token') || '');
  useEffect(() => { window.history.replaceState(null, '', window.location.pathname); }, []);
  const confirm = useMutation({ mutationFn: (form: FormData) => {
    const body = { token, password: String(form.get('password')), code: String(form.get('code') || '') };
    return purpose === 'verify' ? result(client.POST('/api/v1/auth/email/verify', { body })) : result(client.POST('/api/v1/auth/password/reset', { body }));
  }, onSuccess: () => changeAccount(cache, null) });
  return <PageShell title={purpose === 'verify' ? '验证邮箱并设置密码' : '设置新密码'}>
    <div className="email-access-card">
      {token.length !== 43 ? <><p role="alert">链接不完整，请重新申请验证邮件。</p><a className="button secondary" href="/">返回登录</a></> : confirm.data ? <><p role="status">{confirm.data.message}</p><a className="button primary" href="/">前往登录</a></> : <form className="editor-form" onSubmit={event => { event.preventDefault(); confirm.mutate(new FormData(event.currentTarget)); }}><fieldset disabled={confirm.isPending}>
        <label>设置登录密码<Input name="password" type="password" autoComplete="new-password" minLength={12} maxLength={72} required data-initial-focus /><span className="field-hint">至少 12 个字符，最多 72 字节。</span></label>
        <label>验证码或恢复码 <span className="optional">已开启两步验证时填写</span><Input name="code" autoComplete="one-time-code" maxLength={64} /></label>
        <p className="muted">完成后需要重新登录，其他设备的登录也会失效。</p><Button className="button primary">{confirm.isPending ? '正在确认…' : '确认设置密码'}</Button>
      </fieldset>{confirm.isError && <p className="error-message" role="alert">{errorMessage(confirm.error)}</p>}</form>}
    </div>
  </PageShell>;
}
