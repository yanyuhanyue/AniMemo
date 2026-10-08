import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { useState } from 'react';
import type { FormEvent } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { client, errorMessage, result } from '../api/client';
import type { User } from '../api/client';
import { Icon } from '../components/ui/Icon';
import { EmailRequest } from './EmailAccess';

export function Auth({ onAuthenticated }: { onAuthenticated: (user: User) => void }) {
  const [mode, setMode] = useState<'login' | 'register'>('register');
  const [mailMode, setMailMode] = useState<'verify' | 'reset' | null>(null);
  const [message, setMessage] = useState('');
  const options = useQuery({ queryKey: ['auth-options'], retry: false, queryFn: ({ signal }) => result(client.GET('/api/v1/auth/options', { signal })) });
  const submit = useMutation({
    mutationFn: async (data: FormData) => {
      const credentials = { email: String(data.get('email')), password: String(data.get('password') || '') };
      return mode === 'register'
        ? result(client.POST('/api/v1/auth/register', { body: { ...credentials, display_name: String(data.get('display_name')) } }))
        : result(client.POST('/api/v1/auth/login', { body: { ...credentials, code: String(data.get('code') || '') } }));
    },
    onSuccess: response => { if ('id' in response) onAuthenticated(response); else setMessage(response.message); },
  });
  function handleSubmit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); submit.mutate(new FormData(event.currentTarget)); }
  return <main className="auth-page">
    <section className="auth-story" aria-labelledby="welcome-title">
      <a className="brand" href="/"><span className="brand-mark"><Icon name="play" /></span>AniMemo<span className="brand-dot">.</span></a>
      <div className="auth-intro">
        <p className="eyebrow">MY ANIME MEMORY</p>
        <h1 id="welcome-title">故事会完结。<br /><span>记忆，继续放映。</span></h1>
        <p className="auth-description">记下看过的番剧，留下当时的心情。<br />把那些不舍得忘记的瞬间，留给以后的自己。</p>
      </div>
      <div className="memory-ticket" aria-hidden="true"><span className="ticket-sticker">✦ 心动存档</span>
        <div className="ticket-top"><span>ANIMEMO / PERSONAL ARCHIVE</span><Icon name="sparkle" /></div>
        <div className="ticket-main"><span className="ticket-play"><Icon name="play" /></span><div><span>KEEP THE FEELING.</span><strong>此刻的喜欢，<br />值得被记住。</strong></div></div>
        <div className="ticket-bottom"><span>一部动画 · 一段时光</span><span className="ticket-bars" /></div>
      </div>
      <p className="auth-footer">你的观看记录和短评，默认仅自己可见。</p>
    </section>
    <section className="auth-panel" aria-label={mode === 'register' ? '创建账号' : '登录'}>
      <div className="auth-form-wrap">
        <span className="small-label"><span className="status-dot" /> YOUR PRIVATE JOURNAL</span>
        <h2>{mode === 'register' ? '开启你的番剧手账' : '欢迎回来'}</h2>
        <p className="muted">{mode === 'register' ? '从第一部想记住的动画开始。' : '来看看故事又进行到哪里了。'}</p>
        <div className="auth-tabs" aria-label="账号操作">
          <Button type="button" aria-pressed={mode === 'register'} onClick={() => { setMode('register'); submit.reset(); }}>创建账号</Button>
          <Button type="button" aria-pressed={mode === 'login'} onClick={() => { setMode('login'); submit.reset(); }}>已有账号</Button>
        </div>
        {mailMode ? <EmailRequest purpose={mailMode} onBack={() => setMailMode(null)} /> : <form key={mode} onSubmit={handleSubmit} className="auth-form">
          {mode === 'register' && <label>怎么称呼你<Input name="display_name" autoComplete="nickname" placeholder="你的昵称" required maxLength={32} autoFocus /></label>}
          <label>邮箱地址<Input name="email" type="email" autoComplete="email" placeholder="you@example.com" required maxLength={254} autoFocus={mode === 'login'} /></label>
          {mode === 'register' && options.data?.email_verification ? <p className="field-hint">我们会发送验证链接；打开邮件后设置密码，完成账号创建。</p> : <label>密码<Input name="password" type="password" autoComplete={mode === 'register' ? 'new-password' : 'current-password'} placeholder={mode === 'register' ? '至少 12 个字符' : '输入你的密码'} minLength={mode === 'register' ? 12 : undefined} maxLength={72} required />{mode === 'register' && <span className="field-hint">至少 12 个字符，建议使用一段容易记住的长密码。</span>}</label>}
          {mode === 'login' && <label>验证码或恢复码 <span className="optional">已开启两步验证时填写</span><Input name="code" autoComplete="one-time-code" maxLength={64} /></label>}
          {submit.isError && <p className="error-message" role="alert">{errorMessage(submit.error)}</p>}
          <Button className="button primary auth-submit" disabled={submit.isPending}>{submit.isPending ? '正在处理…' : mode === 'register' ? '创建我的手账' : '进入我的手账'}<Icon name="arrow" /></Button>
          {message && <p role="status">{message}</p>}
        </form>}
        {options.data?.password_reset && <div className="auth-email-actions"><Button className="text-button" onClick={() => setMailMode('reset')}>忘记密码</Button><Button className="text-button" onClick={() => setMailMode('verify')}>重发验证邮件</Button></div>}
        <a href="/explore">先看看公开手账 →</a><p className="auth-note"><Icon name="book" /> 一次相遇，一次记录。</p>
      </div>
    </section>
  </main>;
}
