import { Rating } from '../components/Rating';
import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { client, errorMessage, result } from '../api/client';
import type { components } from '../api/schema';
import { PageShell } from '../public/Public';
import { statusLabels } from '../journal/labels';

type Job = components['schemas']['SyncJob'];
type Item = components['schemas']['SyncItem'];
type Value = components['schemas']['SyncValue'];
type Action = Item['action'];
const states: Record<Job['state'], string> = { fetching: '正在生成预览', ready: '等待确认', applying: '正在同步', done: '已结束', failed: '未完成', cancelled: '已取消' };
const actions: Record<Action, string> = { pull: '采用 Bangumi → 手账', push: '采用手账 → Bangumi', skip: '跳过，保留现状' };

export function Connections({ userID }: { userID: string }) {
  const cache = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [disconnecting, setDisconnecting] = useState(false);
  const connection = useQuery({ queryKey: ['connections',userID], queryFn: ({ signal }) => result(client.GET('/api/v1/connections/bangumi', { signal })) });
  const jobs = useQuery({ queryKey: ['connections',userID,'jobs'], queryFn: ({ signal }) => result(client.GET('/api/v1/connections/bangumi/sync', { signal })), refetchInterval: query => query.state.data?.items.some(job => job.state === 'fetching' || job.state === 'applying') ? 1500 : false });
  const jobID = selected || jobs.data?.items[0]?.id;
  const current = useQuery({ queryKey: ['connections',userID,'job',jobID], enabled: !!jobID, queryFn: ({ signal }) => result(client.GET('/api/v1/connections/bangumi/sync/{id}', { signal, params: { path: { id: jobID! } } })), refetchInterval: query => query.state.data?.state === 'fetching' || query.state.data?.state === 'applying' ? 1200 : false });
  const refresh = () => cache.invalidateQueries({ queryKey: ['connections',userID] });
  const authorize = useMutation({ mutationFn: () => result(client.POST('/api/v1/connections/bangumi/authorize')), onSuccess: data => { window.location.assign(data.url); } });
  const verify = useMutation({ mutationFn: () => result(client.POST('/api/v1/connections/bangumi/verify')), onSuccess: refresh });
  const disconnect = useMutation({ mutationFn: () => result(client.DELETE('/api/v1/connections/bangumi')), onSuccess: async () => { setDisconnecting(false); await refresh(); } });
  const start = useMutation({ mutationFn: (form: FormData) => result(client.POST('/api/v1/connections/bangumi/sync', { body: { mode: String(form.get('mode')) as Job['mode'], include_progress: form.has('progress'), request_id: crypto.randomUUID() } })), onSuccess: async job => { setSelected(job.id); cache.setQueryData(['connections',userID,'job',job.id],job); await refresh(); } });
  const change = useMutation({ mutationFn: ({ id, body }: { id: string; body: components['schemas']['SyncAction'] }) => result(client.POST('/api/v1/connections/bangumi/sync/{id}', { params: { path: { id } }, body })), onSuccess: async job => { cache.setQueryData(['connections',userID,'job',job.id],job); await refresh(); } });
  const busy = authorize.isPending || disconnect.isPending || verify.isPending;
  const problem = authorize.error || verify.error || disconnect.error || start.error || change.error || connection.error || jobs.error || current.error;
  const outcome = new URLSearchParams(window.location.search).get('connection');
  const active = jobs.data?.items.some(job => ['fetching','ready','applying'].includes(job.state));
  return <PageShell title="外部账号与收藏同步"><div className="connections-page">
    {outcome === 'connected' && connection.data?.state === 'connected' && <p className="connection-notice" role="status">Bangumi 账号已连接。接下来可以生成收藏预览。</p>}
    {outcome === 'failed' && <p className="error-message" role="alert">授权未完成或已过期，请重新连接。</p>}
    <section className="connection-card"><h2>Bangumi</h2>{connection.isPending ? <p role="status">正在读取连接…</p> : connection.data && <>
      <p>{connection.data.state === 'connected' ? `已连接 ${connection.data.nickname || connection.data.username}（${connection.data.username}）` : connection.data.state === 'reauthorize' ? '需要重新授权' : connection.data.state === 'authorizing' ? '等待完成授权' : '尚未连接账号'}</p>
      {!connection.data.configured ? <p className="muted">实例尚未启用 Bangumi 账号连接，请联系管理员配置应用。</p> : <div className="management-actions">{connection.data.state === 'connected' ? <Button className="button secondary" disabled={busy} onClick={() => verify.mutate()}>验证连接</Button> : <Button className="button primary" disabled={busy} onClick={() => authorize.mutate()}>前往 Bangumi 授权</Button>}{connection.data.state !== 'disconnected' && <Button className="button quiet" disabled={busy} onClick={() => setDisconnecting(true)}>断开连接</Button>}</div>}
      {verify.isSuccess && <p role="status">账号身份验证通过。</p>}
      {disconnecting && <div className="delete-confirm"><p>断开后取消后续同步；已经发出的写回请求可能已生效。手账会保留。</p><div><Button className="button quiet" disabled={busy} onClick={() => setDisconnecting(false)}>保持连接</Button><Button className="button danger" disabled={busy} onClick={() => disconnect.mutate()}>确认断开</Button></div></div>}
    </>}</section>
    <section className="connection-card"><h2>生成差异预览</h2><p className="muted">对照已绑定的 Bangumi 条目。先核对每项差异，确认后才会修改手账或外部收藏。</p>
      <form className="editor-form compact-form" onSubmit={event => { event.preventDefault(); start.mutate(new FormData(event.currentTarget)); }}><fieldset disabled={connection.data?.state !== 'connected' || !!active || start.isPending}>
        <label>同步方向<select name="mode" defaultValue="pull"><option value="pull">Bangumi → 手账</option><option value="push">手账 → Bangumi</option><option value="two_way">双向对照，逐项选择</option></select></label>
        <label className="source-field"><Input type="checkbox" name="progress" /><span>同时同步观看进度<small>写回时将第 1 话至当前进度设为看过，后续正片设为未收藏；观看日期、刷次和逐次笔记留在手账中。</small></span></label>
        <p className="field-hint">默认对照收藏状态、评分、短评和标签。Bangumi 评分为整数，写回时的取整结果会列入预览。新建外部收藏默认私密，不自动删除任何一边的条目。</p><Button className="button primary">{start.isPending ? '正在创建预览…' : '生成预览'}</Button>
      </fieldset></form>{active && <p className="muted">请先完成或取消下方的任务，再生成新预览。</p>}
    </section>
    {jobs.data && jobs.data.items.length > 0 && <label className="sync-history">查看任务<select value={jobID} onChange={event => setSelected(event.target.value)}>{jobs.data.items.map(job => <option value={job.id} key={job.id}>{new Date(job.created_at).toLocaleString()} · {states[job.state]}</option>)}</select></label>}
    {current.data && <section className="connection-card"><div className="section-heading"><h2>{states[current.data.state]}</h2>{['fetching','ready','applying'].includes(current.data.state) && <Button className="button quiet" disabled={change.isPending} onClick={() => change.mutate({ id: current.data!.id, body: { action: 'cancel' } })}>取消后续操作</Button>}</div>
      {current.data.state === 'fetching' && <p role="status">已读取 {current.data.cursor} / {current.data.remote_total || '…'} 项收藏，可稍后回到本页继续。</p>}
      {current.data.error && <p className="error-message" role="alert">{current.data.error}</p>}
      {current.data.state !== 'fetching' && <SyncPreview key={current.data.id} job={current.data} busy={change.isPending} onApply={choices => change.mutate({ id: current.data!.id, body: { action: 'apply', choices } })} />}
    </section>}
    {problem && <p className="error-message" role="alert">{errorMessage(problem)}</p>}
  </div></PageShell>;
}

function ValueSummary({ value, absent }: { value?: Value | null; absent: string }) {
  if (!value) return <p className="muted">{absent}</p>;
  return <dl className="sync-values"><div><dt>状态</dt><dd>{statusLabels[value.status]}</dd></div><div><dt>评分</dt><dd><Rating score={value.score || null} label="评分" /></dd></div><div><dt>进度</dt><dd>{value.progress} 话</dd></div><div><dt>标签</dt><dd>{value.tags.join('、') || '无'}</dd></div><div><dt>短评</dt><dd>{value.notes || '无'}</dd></div></dl>;
}
function SyncPreview({ job, busy, onApply }: { job: Job; busy: boolean; onApply: (choices: components['schemas']['SyncAction']['choices']) => void }) {
  const [choices, setChoices] = useState<Record<string, Action>>({});
  const [page, setPage] = useState(1);
  const actionFor = (item: Item): Action => choices[item.id] || (item.decision === 'pull' || item.decision === 'push' ? item.decision : 'skip');
  const selected = job.items.map(item => ({ id: item.id, action: actionFor(item) }));
  const pulls = selected.filter(item => item.action === 'pull').length, pushes = selected.filter(item => item.action === 'push').length;
  const completed = job.items.filter(item => ['done','failed','conflict','skipped'].includes(item.state)).length;
  return <>
    <p className="muted">{job.items.length} 项对照 · {job.state === 'ready' ? '冲突项默认跳过，请明确选择同步方向。' : `已处理 ${completed} 项；下方对照为确认时的快照。`}</p>
    <div className="sync-items">{job.items.slice((page - 1) * 20,page * 20).map(item => <article className="sync-item" key={item.id}>
      <h3>{item.title || `Bangumi #${item.subject_id}`}</h3><a href={`https://bgm.tv/subject/${item.subject_id}`} target="_blank" rel="noreferrer noopener">Bangumi #{item.subject_id} ↗</a>
      <div className="sync-compare"><div><h4>我的手账</h4><ValueSummary value={item.local?.value} absent="尚未加入手账" /></div><div><h4>Bangumi 收藏</h4><ValueSummary value={item.remote?.value} absent="尚未收藏" />{item.remote && <p className="field-hint">{item.remote.private ? '私密收藏' : '公开收藏'}</p>}</div></div>
      {item.reason && <p className="sync-reason">{item.reason}</p>}
      {job.state === 'ready' ? <><label>处理方式<select value={actionFor(item)} disabled={busy} onChange={event => setChoices(previous => ({ ...previous, [item.id]: event.target.value as Action }))}>{item.allowed.map(action => <option key={action} value={action}>{actions[action]}</option>)}</select></label>
        {actionFor(item) !== 'skip' && <details className="sync-target"><summary>确认后的目标内容</summary><ValueSummary value={actionFor(item) === 'push' ? item.push_target?.value : item.pull_target} absent="无可用目标" />{actionFor(item) === 'push' && <p className="field-hint">外部收藏：{item.push_target?.private ? '私密' : '保持公开'}{job.include_progress ? '；将按连续话数调整正片观看状态。' : '；不单独修改逐话状态。'}</p>}</details>}
      </> : <p role="status">{{ pending:'等待处理',running:'正在核对与执行',done:'已完成',conflict:'需要重新核对',failed:'未完成',skipped:'已跳过' }[item.state]}{item.result && ` · ${item.result}`}</p>}
    </article>)}</div>
    {job.items.length > 20 && <div className="pagination"><Button className="button quiet" disabled={page === 1} onClick={() => setPage(page - 1)}>上一页</Button><span>{page} / {Math.ceil(job.items.length / 20)}</span><Button className="button quiet" disabled={page * 20 >= job.items.length} onClick={() => setPage(page + 1)}>下一页</Button></div>}
    {job.state === 'ready' && <div className="sync-confirm"><p>将导入 / 更新手账 {pulls} 项，写回 Bangumi {pushes} 项，其余跳过。</p><Button className="button primary" disabled={busy} onClick={() => onApply(selected)}>确认执行所选操作</Button></div>}
  </>;
}
