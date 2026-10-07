import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { client, errorMessage, result } from '../api/client';
import type { Entry, Status, User } from '../api/client';
import { Icon } from '../ui/Icon';
import { EntryCard } from './EntryCard';
import { EntryEditor } from './EntryEditor';
import { EntryDetail } from './EntryDetail';
import { WatchEditor } from './WatchEditor';
import { HistoryPanel } from './HistoryPanel';
import { TransferDialog } from './TransferDialog';
import { Settings } from '../accounts/Settings';
import { BulkToolbar, SavedFilters, TagColors, TagEditor } from './ManageTools';
import { statusLabels, statuses } from './labels';

type Modal = { kind: 'create' | 'settings' | 'tags' | 'transfer' } | { kind: 'edit' | 'detail' | 'watch'; entry: Entry } | null;

export function Journal({ user, onLogout, loggingOut }: { user: User; onLogout: () => void; loggingOut: boolean }) {
  const queryClient = useQueryClient();
  const [section, setSection] = useState<'journal' | 'history'>('journal');
  const [status, setStatus] = useState<Status | ''>('');
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState<'updated' | 'title' | 'score'>('updated');
  const [page, setPage] = useState(1);
  const [view, setView] = useState<'cards' | 'list'>(user.default_view);
  const [modal, setModal] = useState<Modal>(null);
  const [toast, setToast] = useState('');
  const [bulk, setBulk] = useState(false);
  const [selected, setSelected] = useState<Entry[]>([]);
  useEffect(() => setView(user.default_view), [user.default_view]);

  useEffect(() => { const timer = setTimeout(() => { setSearch(searchInput); setPage(1); }, 180); return () => clearTimeout(timer); }, [searchInput]);
  useEffect(() => { if (!toast) return; const timer = setTimeout(() => setToast(''), 3600); return () => clearTimeout(timer); }, [toast]);

  const entries = useQuery({
    queryKey: ['journal', user.id, 'entries', { status, search, sort, page }],
    queryFn: ({ signal }) => result(client.GET('/api/v1/entries', { signal, params: { query: { ...(status ? { status } : {}), search, sort, page, page_size: 12 } } })),
    enabled: section === 'journal',
  });
  const stats = useQuery({ queryKey: ['journal', user.id, 'stats'], queryFn: ({ signal }) => result(client.GET('/api/v1/stats', { signal })) });
  const tags = useQuery({ queryKey: ['journal', user.id, 'tags'], queryFn: ({ signal }) => result(client.GET('/api/v1/tags', { signal })) });
  const download = useMutation({
    mutationFn: async () => {
      const data = await result(client.GET('/api/v1/export'));
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url; link.download = `animemo-journal-${new Date().toISOString().slice(0, 10)}.json`;
      link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    },
    onSuccess: () => setToast('手账已导出（不含封面图片），请查看浏览器下载'),
  });

  async function saved(message: string) {
    setModal(null); setToast(message);
    await queryClient.invalidateQueries({ queryKey: ['journal', user.id] });
  }
  const statItems = [
    { label: '收进手账', value: stats.data?.total, unit: '部', icon: 'book' as const, color: 'violet' },
    { label: '正在追看', value: stats.data?.watching, unit: '部', icon: 'play' as const, color: 'coral' },
    { label: '已经看完', value: stats.data?.completed, unit: '部', icon: 'check' as const, color: 'green' },
    { label: '看到这里', value: stats.data?.watched_episodes, unit: '话', icon: 'clock' as const, color: 'blue' },
  ];

  return <TagColors.Provider value={Object.fromEntries((tags.data?.items || []).map(tag => [tag.name, tag.color]))}><div className="app-shell" data-accent={user.accent}>
    <a href="#main-content" className="skip-link">跳到主要内容</a>
    <header className="site-header"><div className="header-inner">
      <a className="brand" href="/" aria-label="AniMemo 首页"><span className="brand-mark"><Icon name="play" /></span>AniMemo<span className="brand-dot">.</span></a>
      <nav className="main-nav" aria-label="主要导航"><a href="/explore">发现</a><button aria-current={section === 'journal' ? 'page' : undefined} onClick={() => setSection('journal')}><Icon name="book" />我的番剧</button><button aria-current={section === 'history' ? 'page' : undefined} onClick={() => setSection('history')}><Icon name="clock" />观看足迹</button></nav>
      <details className="account-menu"><summary><span className="avatar">{user.avatar_revision ? <img src={`/api/v1/avatar/${user.avatar_revision}`} alt="" /> : Array.from(user.display_name)[0]}</span><span className="account-name">{user.display_name}</span><Icon name="chevron" /></summary><div className="account-dropdown"><strong>{user.display_name}</strong><span>{user.email}</span><button type="button" onClick={() => setModal({ kind: 'settings' })}>账号与偏好</button><a href="/my-columns">我的专栏</a>{user.is_admin && <a href="/admin">实例管理</a>}<button type="button" onClick={onLogout} disabled={loggingOut}><Icon name="logout" />{loggingOut ? '正在退出…' : '退出登录'}</button></div></details>
    </div></header>
    <main id="main-content" className="main-content">
      <section className="page-intro"><div><p className="eyebrow">THE STORIES WE KEEP</p><h1>{section === 'journal' ? <>我的番剧<span className="title-dot">.</span></> : <>观看足迹<span className="title-dot">.</span></>}</h1><p className="intro-description">{section === 'journal' ? '每一部喜欢的动画，都有一段属于你的故事。' : '走过的每一话，和当时的心情，都在这里。'}</p></div><div className="intro-actions"><button className="button secondary" onClick={() => setModal({ kind: 'transfer' })}>导入与备份</button><button className="button secondary export-button" onClick={() => download.mutate()} disabled={download.isPending}><Icon name="download" />{download.isPending ? '导出中…' : '导出手账'}</button><button className="button primary" onClick={() => setModal({ kind: 'create' })}><Icon name="plus" />加入番剧</button></div></section>
      {download.isError && <p className="error-message" role="alert">{errorMessage(download.error)}</p>}
      <section className="stats-grid" aria-label="我的观看统计">{statItems.map(item => <div className="stat-card" key={item.label} data-accent={item.color}><div className="stat-top"><span>{item.label}</span><span className="stat-icon"><Icon name={item.icon} /></span></div><div className="stat-value"><strong>{item.value ?? '—'}</strong><span>{item.unit}</span></div></div>)}</section>
      {stats.isError && <p className="error-message" role="alert">统计读取失败：{errorMessage(stats.error)}</p>}
      {section === 'journal' ? <section className="collection" aria-label="番剧清单">
        <div className="collection-toolbar"><div className="filter-tabs" aria-label="按观看状态筛选"><button aria-pressed={status === ''} onClick={() => { setStatus(''); setPage(1); }}>全部<span>{stats.data?.total ?? '—'}</span></button>{statuses.map(value => <button key={value} aria-pressed={status === value} onClick={() => { setStatus(value); setPage(1); }}>{statusLabels[value]}<span>{stats.data?.[value] ?? '—'}</span></button>)}</div><div className="view-toggle" aria-label="显示方式"><button title="卡片视图" aria-label="卡片视图" aria-pressed={view === 'cards'} onClick={() => setView('cards')}><Icon name="grid" /></button><button title="列表视图" aria-label="列表视图" aria-pressed={view === 'list'} onClick={() => setView('list')}><Icon name="book" /></button></div></div>
        <div className="search-toolbar"><label className="search-field"><Icon name="search" /><span className="sr-only">搜索番剧或标签</span><input type="search" placeholder="搜索番剧名称、原名或标签…" value={searchInput} onChange={event => setSearchInput(event.target.value)} maxLength={160} /></label><label className="sort-field"><span>排序</span><select value={sort} onChange={event => { setSort(event.target.value as typeof sort); setPage(1); }} aria-label="番剧排序"><option value="updated">最近更新</option><option value="title">名称顺序</option><option value="score">我的评分</option></select></label></div>
        <div className="management-actions"><button className="text-button" onClick={() => { setBulk(!bulk); setSelected([]); }}>{bulk ? '退出批量管理' : '批量管理'}</button><button className="text-button" onClick={() => setModal({ kind: 'tags' })}>标签颜色</button></div>
        <SavedFilters userID={user.id} search={search} status={status} sort={sort} onApply={filter => { setSearchInput(filter.search); setSearch(filter.search); setStatus(filter.status); setSort(filter.sort); setPage(1); }} />
        {bulk && <BulkToolbar selected={selected} onCancel={() => { setBulk(false); setSelected([]); }} onSaved={async () => { setSelected([]); setBulk(false); await saved('批量修改已完成'); }} />}
        {entries.isPending ? <div className="loading-state" role="status"><span className="spinner" />正在翻开你的手账…</div> : entries.isError ? <div className="empty-state"><h2>手账暂时没能打开</h2><p role="alert">{errorMessage(entries.error)}</p><button className="button secondary" onClick={() => entries.refetch()}>重新加载</button></div> : entries.data.items.length === 0 ? <div className="empty-state"><div className="empty-symbol"><Icon name={search || status ? 'search' : 'book'} /></div><p className="eyebrow">{search || status ? 'KEEP LOOKING' : 'YOUR FIRST MEMORY'}</p><h2>{search || status ? '这里还没有匹配的番剧' : '把第一部喜欢的动画，放进来。'}</h2><p>{search || status ? '试试其他关键词，或者切换观看状态。' : '想看的、正在追的、已经看完的，都可以成为手账的第一篇。'}</p><button className="button primary" onClick={() => { if (search || status) { setSearchInput(''); setSearch(''); setStatus(''); setPage(1); } else setModal({ kind: 'create' }); }}>{search || status ? '查看全部番剧' : '加入第一部番剧'}<Icon name={search || status ? 'arrow' : 'plus'} /></button></div> : <>
          <div className={`entry-grid ${view === 'list' ? 'list-view' : ''}`}>{entries.data.items.map(entry => <EntryCard key={entry.id} entry={entry} selected={bulk ? selected.some(item => item.id === entry.id) : undefined} onSelect={() => setSelected(previous => previous.some(item => item.id === entry.id) ? previous.filter(item => item.id !== entry.id) : [...previous, entry])} onOpen={entry => setModal({ kind: 'detail', entry })} onWatch={entry => setModal({ kind: 'watch', entry })} />)}</div>
          <div className="collection-footer"><span>{entries.data.total} 部番剧 · 每一部都是一段记忆</span>{entries.data.total > 12 && <div className="pagination"><button className="button quiet" disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button><span>{page} / {Math.ceil(entries.data.total / 12)}</span><button className="button quiet" disabled={page * 12 >= entries.data.total} onClick={() => setPage(page + 1)}>下一页</button></div>}</div>
        </>}
      </section> : <HistoryPanel userID={user.id} />}
      <footer className="site-footer"><span>AniMemo<span className="brand-dot">.</span></span><p>故事会完结，记忆继续放映。</p><span className="private-note"><span className="status-dot" />私人手账</span></footer>
    </main>
    {modal?.kind === 'transfer' && <TransferDialog userID={user.id} onClose={() => setModal(null)} />}
    {modal?.kind === 'settings' && <Settings user={user} onClose={() => setModal(null)} />}
    {modal?.kind === 'tags' && <TagEditor userID={user.id} onClose={() => setModal(null)} />}
    {modal?.kind === 'create' && <EntryEditor onClose={() => setModal(null)} onSaved={saved} entry={null} />}
    {modal?.kind === 'edit' && <EntryEditor onClose={() => setModal(null)} onSaved={saved} entry={modal.entry} />}
    {modal?.kind === 'watch' && <WatchEditor onClose={() => setModal(null)} onSaved={saved} entry={modal.entry} />}
    {modal?.kind === 'detail' && <EntryDetail entry={modal.entry} userID={user.id} onClose={() => setModal(null)} onEdit={entry => setModal({ kind: 'edit', entry })} onWatch={entry => setModal({ kind: 'watch', entry })} onDeleted={saved} />}
    <div className={`toast ${toast ? 'toast-visible' : ''}`} role="status">{toast && <><Icon name="check" />{toast}</>}</div>
  </div></TagColors.Provider>;
}
