import { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { ApiError, client, errorMessage, result } from '../api/client';
import type { components } from '../api/schema';

export function PluginImport({ userID, busy, onCreated }: { userID: string; busy: boolean; onCreated: (job: components['schemas']['ImportJob']) => Promise<void> }) {
  const [slug, setSlug] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const list = useQuery({ queryKey: ['plugins', userID], queryFn: ({ signal }) => result(client.GET('/api/v1/plugins', { signal })) });
  const selected = list.data?.items.find(item => item.manifest.slug === slug);
  const convert = useMutation({ mutationFn: async () => {
    if (!file || !selected) throw new Error('请选择插件和文本文件。');
    if (!file.size || file.size > 2 * 1024 * 1024) throw new ApiError(413, 'import_too_large', '文本文件需要大于 0 且不超过 2 MiB。');
    return result(client.POST('/api/v1/plugins/{slug}/imports', { params: { path: { slug }, query: { filename: file.name } }, headers: { 'Content-Type': 'application/octet-stream' }, body: '', bodySerializer: () => file }));
  }, onSuccess: onCreated });
  if (list.isPending) return null;
  if (list.error) return <p role="alert">插件列表加载失败：{errorMessage(list.error)}</p>;
  if (!list.data.items.length) return null;
  return <section className="settings-section plugin-import"><h3>使用插件导入文本</h3><p className="muted">插件只读取你选择的文件。解析后仍需在下方核对预览并确认，才会写入手账。</p><label htmlFor="plugin-converter">导入插件</label><select id="plugin-converter" value={slug} disabled={busy || convert.isPending} onChange={e => { setSlug(e.target.value); convert.reset(); }}><option value="">请选择转换器</option>{list.data.items.map(item => <option key={item.manifest.slug} value={item.manifest.slug}>{item.manifest.name} · {item.manifest.version}</option>)}</select>{selected && <><p className="muted">{selected.manifest.description}</p><label htmlFor="plugin-source">选择 UTF-8 文本</label><input id="plugin-source" type="file" accept=".txt,.tsv" disabled={busy || convert.isPending} onChange={e => { setFile(e.currentTarget.files?.[0] || null); convert.reset(); }} />{slug === 'watch-history-text' && <details><summary>TXT 观看记录格式</summary><p>文件名包含年份，例如 2026观看记录.txt。填写月日、标题和明确话数；默认首刷，可写二刷、三刷等。暂不支持日期区间、模糊话数或自动匹配资料。</p><pre>10月1日{'\n'}首刷 夏目友人帐 第1-3集{'\n'}10月2日{'\n'}二刷 夏目友人帐 第1集 -- 重温</pre><p>也可每行使用制表符分隔：完整日期、标题、话数（如 1-3），可追加刷次和笔记。仅导入新番剧；已有同名番剧整体跳过。</p></details>}<button className="button secondary" disabled={!file || busy || convert.isPending} onClick={() => convert.mutate()}>{convert.isPending ? '正在解析文本…' : '解析并生成预览'}</button></>}{convert.error && <p role="alert" className="error-message">{errorMessage(convert.error)}</p>}</section>;
}
