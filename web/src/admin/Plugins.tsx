import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { useRef, useState } from 'react';
import { Icon } from '../components/ui/Icon';
import { Dialog } from '../components/ui/Dialog';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { client, result } from '../api/client';
import { Problem } from '../public/Public';
import type { components } from '../api/schema';

type Manifest = components['schemas']['PluginManifest'];
export function Plugins() {
  const cache = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [candidate, setCandidate] = useState<{ file: File; manifest: Manifest } | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [notice, setNotice] = useState('');
  const key = ['admin', 'plugins'];
  const list = useQuery({ queryKey: key, queryFn: ({ signal }) => result(client.GET('/api/v1/admin/plugins', { signal })) });
  const install = useMutation({ mutationFn: (file: File) => result(client.POST('/api/v1/admin/plugins', { headers: { 'Content-Type': 'application/octet-stream' }, body: '', bodySerializer: () => file })), onSuccess: async () => { setCandidate(null); setNotice('插件版本已安装。请核对后单独启用；已有启用版本保持原状。'); await cache.invalidateQueries({ queryKey: key }); } });
  const change = useMutation({ mutationFn: ({ item, action }: { item: components['schemas']['PluginRelease']; action: 'activate' | 'disable' | 'uninstall' }) => result(client.POST('/api/v1/admin/plugins/{slug}', { params: { path: { slug: item.manifest.slug } }, body: { action, version: item.manifest.version, revision: item.revision } })), onSuccess: async () => { setNotice('插件状态已更新。'); await cache.invalidateQueries({ queryKey: key }); await cache.invalidateQueries({ queryKey: ['plugins'] }); }, onError: () => { void cache.invalidateQueries({ queryKey: key }); } });
  async function inspect(file: File) {
    setError(null); setCandidate(null); setNotice(''); install.reset();
    try {
      if (!file.size || file.size > 12 * 1024 * 1024) throw new Error('插件包需要大于 0 且不超过 12 MiB。');
      const data: unknown = JSON.parse(await file.text());
      if (!data || typeof data !== 'object' || !('manifest' in data)) throw new Error('插件包缺少清单。');
      const m = data.manifest as Partial<Manifest>;
      if (!m || !['name', 'slug', 'version', 'description', 'module_sha256'].every(k => typeof m[k as keyof Manifest] === 'string') || !Array.isArray(m.capabilities) || !m.capabilities.every(c => typeof c === 'string') || !Number.isInteger(m.host_api_min) || !Number.isInteger(m.host_api_max)) throw new Error('插件清单格式无效。');
      setCandidate({ file, manifest: m as Manifest });
    } catch (e) { setError(e instanceof Error ? e : new Error('无法读取插件包。')); }
  }
  return <div className="admin-stack">
    <section className="admin-panel admin-plugin-upload"><span className="admin-tile-icon purple"><Icon name="puzzle" /></span><div><h2>为手账添一点新能力</h2><p>安装已审核的扩展包，按需启用文件导入等能力。</p></div><Input ref={fileInput} className="admin-file-input" type="file" aria-label="选择插件包" accept=".animemo-plugin" disabled={install.isPending} onChange={e => { const file = e.currentTarget.files?.[0]; e.currentTarget.value = ''; if (file) void inspect(file); }} /><Button className="button primary" disabled={install.isPending} onClick={() => fileInput.current?.click()}><Icon name="plus" />安装本地扩展</Button></section>
    {candidate && <Dialog title={`核对待安装插件 · ${candidate.manifest.name}`} eyebrow="REVIEW EXTENSION" onClose={() => { if (!install.isPending) setCandidate(null); }}><div className="admin-dialog-form"><p>{candidate.manifest.slug} · {candidate.manifest.version}</p><p>{candidate.manifest.description}</p><div className="admin-callout"><strong>申请权限</strong><p>{candidate.manifest.capabilities.join('、')}（转换所选文件）</p></div><p>支持宿主接口：{candidate.manifest.host_api_min}–{candidate.manifest.host_api_max}；当前为 1。</p><details><summary>模块 SHA-256</summary><code className="plugin-digest">{candidate.manifest.module_sha256}</code></details><div className="admin-form-actions"><Button className="button secondary" disabled={install.isPending} onClick={() => setCandidate(null)}>取消</Button><Button className="button primary" disabled={install.isPending} onClick={() => install.mutate(candidate.file)}>{install.isPending ? '正在校验并安装…' : '已审核，安装此版本'}</Button></div>{install.error && <Problem error={install.error} />}</div></Dialog>}
    {notice && <p className="admin-notice" role="status"><Icon name="check" />{notice}</p>}{(error || change.error) && <Problem error={error || change.error} />}
    <section className="admin-panel"><div className="admin-panel-heading"><div><h2>已安装扩展{list.data && <span className="admin-count">{list.data.items.length}</span>}</h2><p>每个扩展版本独立管理，安装后需单独启用</p></div><span className="admin-badge neutral">本地扩展包</span></div>
    {list.isPending ? <div className="admin-empty" role="status">正在读取插件…</div> : list.error ? <Problem error={list.error} /> : list.data.items.length === 0 ? <div className="admin-empty"><Icon name="puzzle" /><h3>扩展位，等待你的新灵感</h3><p>选择一个信任的 .animemo-plugin 文件开始安装。</p></div> : <div className="admin-plugin-list">{list.data.items.map(item => <article key={`${item.manifest.slug}@${item.manifest.version}`} className="admin-plugin-card">
      <div className="admin-plugin-header"><span className={`admin-tile-icon ${item.publisher_id === 'ANIMEMO_FIRST_PARTY' ? 'purple' : 'blue'}`}><Icon name="puzzle" /></span><div><h3>{item.manifest.name}<span className="admin-version">v{item.manifest.version}</span></h3><div className="admin-plugin-badges"><span className={`admin-badge ${item.publisher_id === 'ANIMEMO_FIRST_PARTY' ? 'purple' : 'neutral'}`}>{item.publisher_id === 'ANIMEMO_FIRST_PARTY' ? '✦ 官方随附扩展' : '本地审阅的扩展'}</span><span className={`admin-status ${item.active && item.enabled ? 'green' : 'neutral'}`}>{item.active ? item.enabled ? '已启用' : '已停用' : '待启用'}</span></div></div><div className="management-actions">{item.active && item.enabled ? <Button className="button secondary" disabled={change.isPending || install.isPending} onClick={() => change.mutate({ item, action: 'disable' })}>停用</Button> : <Button className="button primary" disabled={change.isPending || install.isPending} onClick={() => change.mutate({ item, action: 'activate' })}>启用此版本</Button>}</div></div>
      <p className="admin-plugin-description">{item.manifest.description}</p>
      {item.health !== 'ready' && <p className="admin-callout" role="status">{item.health_reason || '恢复后需要重新审阅并启用。'}</p>}
      <div className="admin-plugin-permissions"><span><Icon name="check" />独立进程运行</span><span><Icon name="download" />{item.manifest.capabilities.includes('import.convert') ? '转换用户选择的文件' : item.manifest.capabilities.join('、')}</span></div>
      <div className="admin-plugin-meta"><details><summary>版本与包详情</summary><div className="admin-detail-copy"><p>{item.active ? item.enabled ? '当前启用' : '当前版本已停用' : '已安装，可切换'} · {item.manifest.slug}</p><p>权限：{item.manifest.capabilities.join('、')}</p><p>宿主接口 {item.manifest.host_api_min}–{item.manifest.host_api_max}</p><code className="plugin-digest">{item.digest}</code></div></details>{item.distribution !== 'bundled' && <details><summary>卸载扩展</summary><div className="admin-detail-copy"><p>删除这个扩展的安装与本地包；已导入的手账、观看记录和回执会保留。</p><Button className="button danger" disabled={change.isPending || install.isPending} onClick={() => change.mutate({ item, action: 'uninstall' })}>确认卸载</Button></div></details>}</div>
    </article>)}</div>}</section>
    <section className="admin-plugin-guide"><span className="admin-tile-icon pink"><Icon name="book" /></span><div><h3>关于扩展权限</h3><p>仅安装你审核并信任的插件包。当前扩展只接收用户主动选择的文本，不读取已有手账。切换版本会丢弃旧版本运行中的转换结果；用户已生成的导入预览可以继续确认或取消。历史包保留，便于切回旧版本。</p></div></section>
  </div>;
}
