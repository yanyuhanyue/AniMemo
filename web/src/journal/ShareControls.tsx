import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { client, errorMessage, result } from '../api/client';
import type { Entry } from '../api/client';
export function ShareControls({ entry, userID }: { entry: Entry; userID: string }) {
  const cache = useQueryClient(); const [confirm, setConfirm] = useState(false); const [copied, setCopied] = useState('');
  const reset = useMutation({ mutationFn: () => result(client.POST('/api/v1/entries/{id}/share/reset', { params: { path: { id: entry.id } }, body: { version: entry.version } })), onSuccess: async () => { setConfirm(false); await cache.invalidateQueries({ queryKey: ['journal', userID] }); } });
  const url = `${window.location.origin}/s/${entry.share_slug}`;
  return <section className="settings-section"><h3>分享这部番剧</h3><p>可见性：{{ private: '私密', unlisted: '持链接可见', public: '公开' }[entry.visibility]}</p>{entry.visibility === 'private' ? <p className="muted">编辑番剧即可调整可见性。</p> : <><p className="muted">需要在账号设置开启分享。链接包含作品资料、评分、短评、标签与封面。</p><label>分享链接<input value={url} readOnly onFocus={e => e.target.select()} /></label><div className="management-actions"><a className="button secondary" href={url} target="_blank" rel="noopener noreferrer">打开分享</a><button className="button secondary" onClick={async () => { try { await navigator.clipboard.writeText(url); setCopied('链接已复制'); } catch { setCopied('请选中上方链接手动复制'); } }}>复制链接</button><button className="text-button" onClick={() => setConfirm(true)}>更换链接</button></div>{copied && <p role="status">{copied}</p>}</>}{confirm && <div className="delete-confirm"><p>更换后旧链接和旧图片地址立即失效。</p><button className="button primary" disabled={reset.isPending} onClick={() => reset.mutate()}>确认更换链接</button><button className="button quiet" onClick={() => setConfirm(false)}>取消</button></div>}{reset.error && <p role="alert" className="error-message">{errorMessage(reset.error)}</p>}</section>;
}
