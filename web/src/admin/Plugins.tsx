import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { client, result } from '../api/client';
import { Problem } from '../public/Public';
import type { components } from '../api/schema';

type Manifest = components['schemas']['PluginManifest'];
export function Plugins() {
  const cache = useQueryClient();
  const [candidate, setCandidate] = useState<{ file: File; manifest: Manifest } | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [notice, setNotice] = useState('');
  const key = ['admin', 'plugins'];
  const list = useQuery({ queryKey: key, queryFn: ({ signal }) => result(client.GET('/api/v1/admin/plugins', { signal })) });
  const install = useMutation({ mutationFn: (file: File) => result(client.POST('/api/v1/admin/plugins', { headers: { 'Content-Type': 'application/octet-stream' }, body: '', bodySerializer: () => file })), onSuccess: async () => { setCandidate(null); setNotice('插件版本已安装。请核对后单独启用；已有启用版本保持原状。'); await cache.invalidateQueries({ queryKey: key }); } });
  const change = useMutation({ mutationFn: ({ item, action }: { item: components['schemas']['PluginRelease']; action: 'activate' | 'disable' }) => result(client.POST('/api/v1/admin/plugins/{slug}', { params: { path: { slug: item.manifest.slug } }, body: { action, version: item.manifest.version, revision: item.revision } })), onSuccess: async () => { setNotice('插件状态已更新。'); await cache.invalidateQueries({ queryKey: key }); await cache.invalidateQueries({ queryKey: ['plugins'] }); }, onError: () => { void cache.invalidateQueries({ queryKey: key }); } });
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
  return <section className="plugin-management"><h2>插件管理</h2><p className="muted">仅安装你审核并信任的插件包。当前支持文件导入转换；插件只接收用户主动选择的文本，不读取已有手账。</p><label className="import-file">选择插件包<input type="file" accept=".animemo-plugin" disabled={install.isPending} onChange={e => { const file = e.currentTarget.files?.[0]; e.currentTarget.value = ''; if (file) void inspect(file); }} /></label>
    {candidate && <section className="admin-confirm"><h3>核对待安装插件：{candidate.manifest.name}</h3><p>{candidate.manifest.slug} · {candidate.manifest.version}</p><p>{candidate.manifest.description}</p><p>申请权限：{candidate.manifest.capabilities.join('、')}（转换所选文件）</p><p>支持宿主接口：{candidate.manifest.host_api_min}–{candidate.manifest.host_api_max}；当前为 1。</p><details><summary>模块 SHA-256</summary><code className="plugin-digest">{candidate.manifest.module_sha256}</code></details><div className="management-actions"><button className="button primary" disabled={install.isPending} onClick={() => install.mutate(candidate.file)}>{install.isPending ? '正在校验并安装…' : '已审核，安装此版本'}</button><button className="button quiet" disabled={install.isPending} onClick={() => setCandidate(null)}>取消</button></div></section>}
    {notice && <p role="status">{notice}</p>}{(error || install.error || change.error) && <Problem error={error || install.error || change.error} />}
    {list.isPending ? <p>正在读取插件…</p> : list.error ? <Problem error={list.error} /> : list.data.items.length === 0 ? <p>尚未安装插件。</p> : <div className="admin-list">{list.data.items.map(item => <article key={`${item.manifest.slug}@${item.manifest.version}`} className="admin-row"><div><h3>{item.manifest.name} · {item.manifest.version}</h3><p>{item.manifest.description}</p><p>{item.active ? item.enabled ? '当前启用' : '当前版本已停用' : '已安装，可切换'} · {item.manifest.slug}</p><details><summary>包摘要</summary><code className="plugin-digest">{item.digest}</code></details></div><div className="management-actions">{item.active && item.enabled ? <button className="button secondary" disabled={change.isPending || install.isPending} onClick={() => change.mutate({ item, action: 'disable' })}>停用</button> : <button className="button secondary" disabled={change.isPending || install.isPending} onClick={() => change.mutate({ item, action: 'activate' })}>启用此版本</button>}</div></article>)}</div>}
    <p className="muted">切换版本会丢弃旧版本尚在运行的转换结果。已经生成的导入预览属于用户，可继续确认或取消。历史包保留，便于切回旧版本；所有管理变更会记录审计。</p>
  </section>;
}
