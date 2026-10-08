import { ImportChoices, type ImportSelection } from './ImportChoices';
import { watchDate } from './labels';
import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { PluginImport } from './PluginImport';
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, client, errorMessage, result } from '../api/client';
import { Dialog } from '../components/ui/Dialog';

const stateLabels = { validating: '正在校验文件', ready: '预览已准备好', applying: '正在导入', done: '导入完成', failed: '导入未完成', cancelled: '已取消' };

export function TransferDialog({ userID, onClose }: { userID: string; onClose: () => void }) {
  const [selection,setSelection]=useState<ImportSelection[]|undefined>();
  const [activeID, setActiveID] = useState('');
  const [notice, setNotice] = useState('');
  const cache = useQueryClient();
  const queryKey = ['journal', userID, 'imports'];
  const jobs = useQuery({ queryKey, queryFn: ({ signal }) => result(client.GET('/api/v1/imports', { signal })), refetchInterval: query => query.state.data?.items.some(job => job.state === 'validating' || job.state === 'applying') ? 800 : false });
  const job = jobs.data?.items.find(item => item.id === activeID) || jobs.data?.items[0];
  const upload = useMutation({ mutationFn: (file: File) => {
    const format = file.name.split('.').at(-1)?.toLowerCase();
    if (format !== 'json' && format !== 'csv' && format !== 'zip') throw new ApiError(400, 'validation_error', '请选择 .json、.csv 或 .zip 文件。');
    const limit = format === 'csv' ? 2 : format === 'json' ? 144 : 256;
    if (!file.size || file.size > limit * 1024 * 1024) throw new ApiError(413, 'import_too_large', `文件需要大于 0 且不超过 ${limit} MiB。`);
    return result(client.POST('/api/v1/imports', { params: { query: { format } }, headers: { 'Content-Type': 'application/octet-stream' }, body: '', bodySerializer: () => file }));
  }, onSuccess: async next => { setActiveID(next.id); await cache.invalidateQueries({ queryKey }); } });
  const action = useMutation({ mutationFn: (operation: 'apply' | 'cancel') => result(client.POST('/api/v1/imports/{id}', { params: { path: { id: job!.id } }, body: { action: operation, ...(operation==='apply'&&!job!.preview.complete_memory?{selection}: {}) } })), onSuccess: () => cache.invalidateQueries({ queryKey: ['journal', userID] }) });
  const backup = useMutation({ mutationFn: async () => {
    const response = await client.GET('/api/v1/backup', { parseAs: 'blob' });
    if (!response.response.ok) {
      let message = '备份未完成，请稍后重试。';
      if (response.error && typeof response.error === 'object' && 'error' in response.error) message = String(response.error.error.message);
      throw new ApiError(response.response.status, 'backup_failed', message);
    }
    const url = URL.createObjectURL(response.data!); const link = document.createElement('a'); link.href = url; link.download = `animemo-backup-${new Date().toISOString().slice(0, 10)}.zip`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }, onSuccess: () => setNotice('含封面备份已下载，请妥善保存。') });
  const pending = jobs.data?.items.some(item => ['validating', 'ready', 'applying'].includes(item.state));
  return <Dialog title="导入与备份" eyebrow="KEEP YOUR MEMORIES" onClose={() => { void cache.invalidateQueries({ queryKey: ['journal', userID] }); onClose(); }} wide>
    <div className="settings-content transfer-content">
      <section><h3>完整备份你的记忆</h3><p className="muted">ZIP 包含封面原图；JSON 与 ZIP 都包含独立记忆、私人图片原件、角色、集数、收藏、年度修订和成就历史。备份不包含账号密码和个人设置。</p><Button className="button secondary" disabled={backup.isPending} onClick={() => backup.mutate()}>{backup.isPending ? '正在生成备份…' : '下载含封面备份'}</Button></section>
      <section className="settings-section"><h3>导入到当前手账</h3><p className="muted">先校验预览，再由你确认。完整记忆包（v2 / v3）需导入空手账，以保留全部关系；CSV、TXT 转换结果可逐项选择。v3 保留内容可见性，但不会恢复分享凭证；任务可在刷新或容器重启后继续。</p><label className="import-file">选择数据文件<Input type="file" accept=".json,.csv,.zip" disabled={upload.isPending || pending} onChange={event => { const file = event.currentTarget.files?.[0]; event.currentTarget.value = ''; if (file) upload.mutate(file); }} /></label><p className="muted">CSV ≤ 2 MiB / 500 部；JSON ≤ 144 MiB；ZIP ≤ 256 MiB。手账最多导入至 5000 部、20000 条观看记录。</p><details><summary>CSV 格式说明</summary><p className="muted">使用 UTF-8 编码，第一行至少包含 title。支持 original_title、format、status、total_episodes、watched_episodes、score、notes、tags、accent、studio、airing_period、description、reference_url。tags 用 | 分隔；format 可填 tv/movie/ova/other，status 留空或填 recorded 表示看过但细节未记，也可填 watching/caught_up/completed/on_hold/dropped；planned 仅用于保留已有分类。</p><pre>title,total_episodes,status,tags{'\n'}我的第一部番剧,,recorded,治愈|冒险</pre></details></section>
      <PluginImport userID={userID} busy={!!pending || upload.isPending} onCreated={async next => { setActiveID(next.id); await cache.invalidateQueries({ queryKey }); }} />
      {upload.isPending && <p role="status">正在上传文件…</p>}
      {job && <section className="import-preview settings-section" aria-live="polite"><h3>{stateLabels[job.state]}</h3>{job.state === 'ready' && <><p>共 {job.preview.total} 部：可新建 {job.preview.ready} 部，需核对重复 {job.preview.duplicates} 部。</p><p>包含 {job.preview.records} 条观看记录、{job.preview.covers} 张封面。</p>{job.preview.warnings.map(warning => <p className="muted" key={warning}>{warning}</p>)}<details><summary>查看待导入名称（最多显示 100 部）</summary><ul>{job.preview.titles.map((title, index) => <li key={index}>{title}</li>)}</ul></details>{job.preview.history.length > 0 && <details><summary>核对观看记录（最多显示 100 条）</summary><ul className="import-records">{job.preview.history.map((record, index) => <li key={index}><strong>{record.title}</strong><span>{watchDate(record.watched_on, record.time_precision)} · 第 {record.episode_from}–{record.episode_to} 话 · 第 {record.rewatch} 刷</span>{record.note && <p>{record.note}</p>}</li>)}</ul></details>}{!job.preview.complete_memory&&<ImportChoices key={job.id} userID={userID} choices={job.preview.choices} onChange={setSelection}/>}<p className="muted">预览后若修改手账，需要重新上传。预览保留 24 小时。</p><Button className="button primary" disabled={action.isPending||(!job.preview.complete_memory&&(!selection||selection.length===0))} onClick={() => action.mutate('apply')}>确认导入 {job.preview.complete_memory?job.preview.ready:selection?.length??0} 项</Button></>}{['validating', 'ready', 'applying'].includes(job.state) && <Button className="button quiet" disabled={action.isPending} onClick={() => action.mutate('cancel')}>取消导入任务</Button>}{job.state === 'done' && <p>已处理 {job.created} 个作品项目，关闭窗口后即可查看。</p>}{job.error && <p className="error-message" role="alert">{job.error}</p>}</section>}
      {(upload.error || action.error || jobs.error || backup.error) && <p className="error-message" role="alert">{errorMessage(upload.error || action.error || jobs.error || backup.error)}</p>}{notice && <p role="status">{notice}</p>}
    </div>
  </Dialog>;
}
