import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { Icon } from '../components/ui/Icon';
import { client, result } from '../api/client';
import { changeAccount } from '../accounts/session';
import { PageShell, Problem } from '../public/Public';
import { Plugins } from './Plugins';
import { Maintenance, Presets } from './Maintenance';
import { Resources } from './Resources';
import { Users } from './Users';
import { SiteSettings, Audit } from './Settings';
import { Overview } from './Overview';
import { AdminAchievements } from './Achievements';
import './admin.css';

export function Setup() {
  const cache = useQueryClient();
  const status = useQuery({ queryKey: ['setup'], queryFn: ({ signal }) => result(client.GET('/api/v1/setup', { signal })) });
  const create = useMutation({ mutationFn: (f: FormData) => result(client.POST('/api/v1/setup', { body: { token: String(f.get('token')), email: String(f.get('email')), display_name: String(f.get('display_name')), password: String(f.get('password')) } })), onSuccess: async u => { await changeAccount(cache, u); window.location.assign('/admin'); } });
  return <PageShell title="初始化你的 AniMemo">{status.isPending ? <p>正在检查实例…</p> : status.error ? <Problem error={status.error} /> : !status.data.available ? <p>初始化入口已关闭。已初始化的实例请登录管理员账号；新实例请由部署者配置初始化口令后再打开此页。</p> : <form className="editor-form setup-form" onSubmit={e => { e.preventDefault(); create.mutate(new FormData(e.currentTarget)); }}><p>使用部署时生成的初始化口令创建第一位管理员。此入口只能完成一次。</p><fieldset disabled={create.isPending}><label>初始化口令<Input name="token" type="password" autoComplete="off" minLength={32} required /></label><label>管理员昵称<Input name="display_name" maxLength={32} required /></label><label>管理员邮箱<Input name="email" type="email" required /></label><label>管理员密码<Input name="password" type="password" autoComplete="new-password" minLength={12} maxLength={72} required /></label><Button className="button primary">创建管理员</Button></fieldset>{create.error && <Problem error={create.error} />}</form>}</PageShell>;
}

const pages = [
  { id: 'overview', label: '控制台概览', short: '概览', icon: 'grid', description: '看看这座小小记忆库，今天的运转情况。' },
  { id: 'users', label: '用户与公开审核', short: '用户管理', icon: 'users', description: '管理账号权限，审核公开手账申请。' },
  { id: 'resources', label: '资源与专栏审核', short: '内容管理', icon: 'book', description: '检查番剧与专栏内容，让分享有序发生。' },
  { id: 'presets', label: '标签预设', short: '标签预设', icon: 'tag', description: '为大家的手账准备一组好用的标签颜色。' },
  { id: 'plugins', label: '插件', short: '扩展管理', icon: 'puzzle', description: '按需扩展手账能力，清楚掌握每个版本与权限。' },
  { id: 'site', label: '站点设置', short: '站点设置', icon: 'settings', description: '设置站点名片，以及新朋友的加入方式。' },
  { id: 'health', label: '健康与维护', short: '健康与维护', icon: 'activity', description: '查看服务运行状态、任务处理与图片存储。' },
  { id: 'achievements', label: '成就管理', short: '成就管理', icon: 'star', description: '管理纪念徽章、受控规则与历史补算。' },
  { id: 'audit', label: '操作审计', short: '操作审计', icon: 'clock', description: '每一次管理操作，都有迹可循。' },
] as const;
export type AdminPage = typeof pages[number]['id'];
function pageFromHash(): AdminPage {
  const hash = window.location.hash.slice(1);
  return pages.find(page => page.id === hash)?.id ?? 'overview';
}
export function Admin({ displayName }: { displayName: string }) {
  const [tab, setTab] = useState(pageFromHash);
  useEffect(() => {
    const sync = () => setTab(pageFromHash());
    window.addEventListener('hashchange', sync);
    return () => window.removeEventListener('hashchange', sync);
  }, []);
  const navigate = (page: AdminPage) => { window.location.hash = page; setTab(page); };
  const current = pages.find(page => page.id === tab)!;
  return <div className="admin-shell">
    <a className="admin-skip" href="#admin-content" onClick={event => { event.preventDefault(); document.getElementById('admin-content')?.focus(); }}>跳到管理内容</a>
    <aside className="admin-sidebar">
      <a className="admin-brand" href="/admin"><span className="admin-brand-icon"><Icon name="sparkle" /></span><span>AniMemo<span className="admin-brand-caption">管理控制台</span></span></a>
      <nav className="admin-nav" aria-label="管理导航">{pages.map((page, i) => <div key={page.id}>
        {(i === 0 || i === 1 || i === 5) && <p className="admin-nav-group">{i === 0 ? '工作台' : i === 1 ? '内容与社区' : '实例管理'}</p>}
        <Button type="button" aria-label={page.label} aria-current={tab === page.id ? 'page' : undefined} aria-pressed={tab === page.id} onClick={() => navigate(page.id)}><Icon name={page.icon} /><span>{page.short}</span>{tab === page.id && <span className="admin-nav-dot" />}</Button>
      </div>)}</nav>
      <div className="admin-sidebar-note"><Icon name="sparkle" /><p>收藏热爱，<br />也守护每一份记忆。</p><span>YOUR LITTLE ANIME UNIVERSE</span></div>
      <a className="admin-back" href="/"><Icon name="logout" />返回我的手账<Icon name="arrow" /></a>
    </aside>
    <div className="admin-workspace">
      <header className="admin-topbar"><div className="admin-breadcrumb">管理控制台<Icon name="chevron" /><span>{current.short}</span></div><div className="admin-account"><span className="admin-badge purple">管理员</span><span className="admin-avatar">{displayName.slice(0, 1)}</span><span>{displayName}</span></div></header>
      <main className="admin-content" id="admin-content" tabIndex={-1}>
        <div className="admin-page-heading"><div><p className="admin-eyebrow">ANIMEMO / CONSOLE</p><h1>{current.short === '概览' ? '控制台概览' : current.short}</h1><p>{current.description}</p></div><span className="admin-date">{new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' }).format(new Date())}</span></div>
        {tab === 'overview' ? <Overview navigate={navigate} /> : tab === 'users' ? <Users /> : tab === 'resources' ? <Resources /> : tab === 'plugins' ? <Plugins /> : tab === 'site' ? <SiteSettings /> : tab === 'presets' ? <Presets /> : tab === 'health' ? <Maintenance /> : tab === 'achievements' ? <AdminAchievements /> : <Audit />}
        <footer className="admin-footer"><span>AniMemo · 给热爱一个归处</span><span>每一份记忆，都值得认真守护。</span></footer>
      </main>
    </div>
  </div>;
}
