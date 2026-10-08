import { Button } from '../components/ui/Button';
import { Icon } from '../components/ui/Icon';
import { Dialog } from '../components/ui/Dialog';
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { client, result } from '../api/client';
import { Problem } from '../public/Public';
import { formatSize, formatTime } from './data';

export function Storage() {
  const cache = useQueryClient();
  const [target, setTarget] = useState<'postgres' | 'r2' | null>(null);
  const q = useQuery({ queryKey: ['admin', 'media'], queryFn: ({ signal }) => result(client.GET('/api/v1/admin/media/storage', { signal })), refetchInterval: 5000 });
  const change = useMutation({ mutationFn: (backend: 'postgres' | 'r2') => result(client.PUT('/api/v1/admin/media/storage', { body: { backend, version: q.data!.version } })), onSuccess: async () => { setTarget(null); await cache.invalidateQueries({ queryKey: ['admin'] }); } });
  const probe = useMutation({ mutationFn: () => result(client.POST('/api/v1/admin/media/storage/probe')) });
  if (q.isPending) return <div className="admin-empty" role="status">正在检查图片存储…</div>;
  if (q.error) return <Problem error={q.error} />;
  const storage = q.data;
  return <section className="admin-panel"><div className="admin-panel-heading"><div><h2>图片存储</h2><p>存储位置与后台迁移</p></div><span className="admin-badge purple">当前使用 {storage.backend === 'r2' ? 'Cloudflare R2' : 'PostgreSQL'}</span></div>
    <div className="admin-storage-options"><div className={`admin-storage-option ${storage.backend === 'postgres' ? 'selected' : ''}`}><span className="admin-tile-icon purple"><Icon name="server" /></span><div><h3>PostgreSQL</h3><p>随实例一起存储，无需额外配置</p><strong>{storage.postgres_images} 张图片</strong></div><Button className="button secondary" disabled={storage.backend === 'postgres' || change.isPending} onClick={() => { change.reset(); setTarget('postgres'); }}>{storage.backend === 'postgres' ? '当前使用' : '切回 PostgreSQL'}</Button></div><div className={`admin-storage-option ${storage.backend === 'r2' ? 'selected' : ''}`}><span className="admin-tile-icon blue"><Icon name="upload" /></span><div><h3>Cloudflare R2<span className={`admin-badge ${storage.configured ? 'green' : 'neutral'}`}>{storage.configured ? '已配置' : '未配置'}</span></h3><p>{storage.configured ? `${storage.remote_images} 张图片 · ${storage.bucket}` : '部署配置完成后，可迁移图片至对象存储'}</p></div><Button className="button secondary" disabled={!storage.configured || storage.backend === 'r2' || change.isPending} onClick={() => { change.reset(); setTarget('r2'); }}>迁移到 R2</Button></div></div>
    <div className="admin-storage-summary"><span>图片总量 <strong>{formatSize(storage.media_bytes)}</strong></span><span>待迁移 <strong>{storage.pending}</strong></span><span>待清理 <strong>{storage.cleanup_pending}</strong></span><span>等待重试 <strong>{storage.failures}</strong></span></div>
    <div className="admin-panel-body admin-storage-details"><details><summary>连接测试与迁移说明</summary><div className="admin-detail-copy"><p>迁移会逐张上传并回读校验；访问继续受手账权限控制。迁移前的原图保留，可切回 PostgreSQL。迁移后新上传的图片在远端校验成功后释放数据库临时副本。</p><p>迁移原图保留 {formatSize(storage.originals_bytes)}。完整实例备份包含 R2 图片，恢复到新实例后图片会落入 PostgreSQL，并使用新的 R2 对象目录。</p>{storage.configured ? <><p>存储桶：{storage.bucket} · {storage.endpoint}</p><p>请保持此专用存储桶私有。连接测试会写入、读取并删除一张测试图片。</p><Button className="button secondary" disabled={probe.isPending} onClick={() => probe.mutate()}>{probe.isPending ? '正在测试…' : '测试 R2 读写与删除'}</Button></> : <p>部署者尚未配置 R2。配置生效后可测试连接并开始迁移。</p>}{probe.data && <p role="status">{probe.data.message}</p>}{probe.error && <Problem error={probe.error} />}</div></details>
      <details><summary>迁移记录<span className="admin-count">{storage.migrations.length}</span></summary>{storage.migrations.length === 0 ? <p className="admin-muted">暂无迁移记录。</p> : <div className="admin-migration-list">{storage.migrations.map(migration => <div key={migration.id}><span>{formatTime(migration.created_at)} · {migration.backend === 'r2' ? 'R2' : 'PostgreSQL'} · {{ running: '进行中', done: '完成', superseded: '已切换方向' }[migration.state]} · {migration.copied}/{migration.total}</span><a className="button quiet" href={`/api/v1/admin/media/migrations/${migration.id}`}><Icon name="download" />下载 SHA-256 清单</a></div>)}</div>}</details>
    </div>
    {target && <Dialog title={`切换图片存储至 ${target === 'r2' ? 'R2' : 'PostgreSQL'}`} onClose={() => { if (!change.isPending) setTarget(null); }}><div className="admin-dialog-form"><p>已有图片会在后台迁移，失败项自动重试。请保留原存储配置，直到迁移和旧图清理完成。</p><div className="admin-form-actions"><Button className="button secondary" disabled={change.isPending} onClick={() => setTarget(null)}>取消</Button><Button className="button primary" disabled={change.isPending} onClick={() => change.mutate(target)}>确认切换</Button></div>{change.error && <Problem error={change.error} />}</div></Dialog>}
  </section>;
}
