import { useState } from 'react';
import type { ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { client, errorMessage, result } from '../api/client';
import type { components } from '../api/schema';
import { statusLabels, formatLabels } from '../journal/labels';

type PublicItem = components['schemas']['PublicItem'];
export function PageShell({ title, children }: { title: string; children: ReactNode }) {
  const site = useQuery({ queryKey: ['site'], queryFn: ({ signal }) => result(client.GET('/api/v1/site', { signal })) });
  return <div className="app-shell"><header className="site-header"><div className="header-inner"><a className="brand" href="/">{site.data?.name || 'AniMemo'}.</a><nav className="public-nav" aria-label="主要导航"><a href="/">我的手账</a><a href="/explore">公开手账</a><a href="/catalog">发现番剧</a><a href="/featured">专栏</a></nav></div></header><main className="main-content public-content"><section className="page-intro"><div><p className="eyebrow">STORIES WORTH SHARING</p><h1>{title}</h1></div></section>{children}<footer className="site-footer"><p>{site.data?.description || '故事会完结，记忆继续放映。'}</p></footer></main></div>;
}
export function Problem({ error }: { error: unknown }) { return <p className="error-message" role="alert">{errorMessage(error)}</p>; }
export function Pager({ page, total, size, onChange }: { page: number; total: number; size: number; onChange: (page: number) => void }) {
  return <div className="collection-footer"><span>共 {total} 条</span>{total > size && <div className="pagination"><button className="button quiet" disabled={page <= 1} onClick={() => onChange(page - 1)}>上一页</button><span>{page} / {Math.ceil(total / size)}</span><button className="button quiet" disabled={page * size >= total} onClick={() => onChange(page + 1)}>下一页</button></div>}</div>;
}
export function PublicCard({ item, detail = false }: { item: PublicItem; detail?: boolean }) {
  const { entry: e, owner } = item;
  return <article className={`public-card ${detail ? 'public-detail' : ''}`} data-accent={e.accent}>
    {e.cover_revision && <a href={`/s/${e.slug}`} tabIndex={detail ? -1 : 0}><img className="public-cover" src={`/api/v1/public/shared/${e.slug}/cover/${e.cover_revision}`} alt={`${e.title} 封面`} loading="lazy" /></a>}
    <div className="public-card-body"><p className="eyebrow">{formatLabels[e.format]} · {statusLabels[e.status]}</p><h2>{detail ? e.title : <a href={`/s/${e.slug}`}>{e.title}</a>}</h2>{e.original_title && <p className="muted">{e.original_title}</p>}<p>已看 {e.watched_episodes} / {e.total_episodes || '—'} 话 {e.score !== null && ` · ${e.score} 分`}</p><div className="entry-tags">{e.tags.map(t => <span key={t}>{t}</span>)}</div>{e.notes && <p className={detail ? 'preserve-lines' : 'public-excerpt'}>{e.notes}</p>}{detail && <><p>{[e.details.studio, e.details.airing_period].filter(Boolean).join(' · ')}</p><p className="preserve-lines">{e.details.description}</p>{e.details.reference_url && <a href={e.details.reference_url} target="_blank" rel="noopener noreferrer">作品资料 ↗</a>}</>}<p className="muted">收藏于 <a href={`/u/${owner.slug}`}>{owner.name}</a> 的手账</p></div>
  </article>;
}
export function Explore() {
  const [search, setSearch] = useState(''); const [page, setPage] = useState(1);
  const q = useQuery({ queryKey: ['public', 'directory', search, page], queryFn: ({ signal }) => result(client.GET('/api/v1/public/showcases', { signal, params: { query: { search, page } } })) });
  return <PageShell title="打开别人的一页手账"><label className="search-field">搜索昵称<input type="search" value={search} maxLength={160} onChange={e => { setSearch(e.target.value); setPage(1); }} /></label>{q.isPending ? <p role="status">正在读取公开手账…</p> : q.error ? <Problem error={q.error} /> : <><div className="public-grid">{q.data.items.map(o => <a className="showcase-card" key={o.slug} href={`/u/${o.slug}`}><span className="avatar large-avatar">{o.avatar_revision ? <img src={`/api/v1/public/showcases/${o.slug}/avatar/${o.avatar_revision}`} alt="" /> : Array.from(o.name)[0]}</span><h2>{o.name}</h2><p>{o.bio}</p><span className="muted">{o.entries} 部公开番剧</span></a>)}</div>{!q.data.items.length && <p className="empty-state">还没有符合条件的公开手账。</p>}<Pager page={page} total={q.data.total} size={12} onChange={setPage} /></>}</PageShell>;
}
export function PublicJournal({ slug }: { slug?: string }) {
  const [search, setSearch] = useState(''); const [page, setPage] = useState(1); const [status, setStatus] = useState<components['schemas']['Entry']['status'] | ''>(''); const [sort, setSort] = useState<'updated' | 'title' | 'score'>('updated');
  const q = useQuery({ queryKey: ['public', 'entries', slug, search, page, status, sort], queryFn: ({ signal }) => { const query = { search, page, sort, ...(status ? { status } : {}) }; return slug ? result(client.GET('/api/v1/public/showcases/{slug}', { signal, params: { path: { slug }, query } })) : result(client.GET('/api/v1/public/catalog', { signal, params: { query } })); } });
  return <PageShell title={q.data?.owner ? `${q.data.owner.name} 的手账` : slug ? '公开手账' : '发现值得记住的番剧'}>{q.data?.owner && <p className="intro-description preserve-lines">{q.data.owner.bio}</p>}<div className="search-toolbar"><label className="search-field">搜索番剧<input value={search} type="search" maxLength={160} onChange={e => { setSearch(e.target.value); setPage(1); }} /></label><label>观看状态<select value={status} onChange={e => { setStatus(e.target.value as typeof status); setPage(1); }}><option value="">全部</option>{Object.entries(statusLabels).map(([k,v]) => <option key={k} value={k}>{v}</option>)}</select></label><label>排序<select value={sort} onChange={e => { setSort(e.target.value as typeof sort); setPage(1); }}><option value="updated">最近更新</option><option value="title">名称顺序</option><option value="score">评分</option></select></label></div>{q.isPending ? <p role="status">正在翻开手账…</p> : q.error ? <Problem error={q.error} /> : <><div className="public-grid">{q.data.items.map(item => <PublicCard key={item.entry.slug} item={item} />)}</div>{!q.data.items.length && <p className="empty-state">这里还没有匹配的公开番剧。</p>}<Pager page={page} total={q.data.total} size={12} onChange={setPage} /></>}</PageShell>;
}
export function SharedEntry({ slug }: { slug: string }) {
  const q = useQuery({ queryKey: ['public', 'shared', slug], retry: false, queryFn: ({ signal }) => result(client.GET('/api/v1/public/shared/{slug}', { signal, params: { path: { slug } } })) });
  return <PageShell title="分享一段记忆">{q.isPending ? <p role="status">正在打开分享…</p> : q.error ? <Problem error={q.error} /> : <PublicCard item={q.data} detail />}</PageShell>;
}
