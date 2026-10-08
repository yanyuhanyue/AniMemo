import { Button } from '../components/ui/Button';
import { Icon } from '../components/ui/Icon';
import { Problem } from '../public/Public';
import type { AdminPage } from './Admin';
import { useAdminStatus, useRuntime, useAudit, formatTime, auditLabel } from './data';

export function Overview({ navigate }: { navigate: (page: AdminPage) => void }) {
  const status = useAdminStatus();
  const runtime = useRuntime();
  const audit = useAudit(1);
  return <div className="admin-stack">
    <section className="admin-welcome">
      <div><p className="admin-eyebrow"><Icon name="sparkle" /> A LITTLE PLACE FOR ALL YOUR STORIES</p><h2>让每一份热爱，<br />安心留在这里。</h2><p>整理故事，连接同好。今天也一起照顾好这座记忆库。</p><Button className="button primary" onClick={() => navigate('resources')}>管理内容<Icon name="arrow" /></Button></div>
    </section>
    {status.isPending ? <div className="admin-empty" role="status">正在读取实例概况…</div> : status.error ? <Problem error={status.error} /> : <div className="admin-metrics">{[
      { label: '注册用户', value: status.data.users, hint: '一起记录热爱的伙伴', icon: 'users', tone: 'purple' },
      { label: '番剧条目', value: status.data.entries, hint: '所有人的收藏 · 含回收站', icon: 'book', tone: 'pink' },
      { label: '专栏文章', value: status.data.columns, hint: '写下的故事 · 含回收站', icon: 'edit', tone: 'blue' },
      { label: '活动导入', value: status.data.imports_active, hint: '正在处理的导入任务', icon: 'download', tone: 'green' },
    ].map(item => <section className="admin-metric" key={item.label}><div><p>{item.label}</p><strong>{item.value.toLocaleString()}</strong><small>{item.hint}</small></div><span className={`admin-tile-icon ${item.tone}`}><Icon name={item.icon as 'users' | 'book' | 'edit' | 'download'} /></span></section>)}</div>}
    <div className="admin-columns">
      <section className="admin-panel"><div className="admin-panel-heading"><div><h2>常用管理</h2><p>从这里开始今天的工作</p></div><Icon name="sparkle" /></div><div className="admin-shortcuts">{[
        { page: 'users', title: '用户与公开审核', text: '账号权限 · 公开申请', icon: 'users' },
        { page: 'resources', title: '内容与专栏', text: '内容检查 · 公开审核', icon: 'book' },
        { page: 'plugins', title: '扩展管理', text: '安装版本 · 启停扩展', icon: 'puzzle' },
      ].map(item => <Button className="admin-shortcut" key={item.page} onClick={() => navigate(item.page as AdminPage)}><span className="admin-tile-icon purple"><Icon name={item.icon as 'users' | 'book' | 'puzzle'} /></span><span><strong>{item.title}</strong><small>{item.text}</small></span><Icon name="chevron" /></Button>)}</div></section>
      <section className="admin-panel"><div className="admin-panel-heading"><div><h2>服务状态</h2><p>数据库与后台任务</p></div><Button className="admin-link" onClick={() => navigate('health')}>查看详情<Icon name="arrow" /></Button></div><div className="admin-service-list"><div><span><Icon name="server" />数据库</span><span className={`admin-badge ${status.data?.database === 'ready' ? 'green' : 'neutral'}`}>{status.data ? status.data.database === 'ready' ? '连接正常' : status.data.database : status.error ? '读取失败' : '检查中'}</span></div><div><span><Icon name="activity" />后台任务进程</span><span className={`admin-badge ${runtime.data?.worker_ready ? 'green' : 'amber'}`}>{runtime.data ? runtime.data.worker_ready ? '运行中' : '暂时离线' : runtime.error ? '读取失败' : '检查中'}</span></div></div>{runtime.error && <Problem error={runtime.error} />}{runtime.data && <div className="admin-job-counts"><div><strong>{runtime.data.pending}</strong><span>待处理</span></div><div><strong>{runtime.data.running}</strong><span>执行中</span></div><div><strong>{runtime.data.failed}</strong><span>失败</span></div></div>}<p className="admin-panel-note">{status.data ? `最近检查 ${formatTime(status.data.server_time)} · 自动刷新` : '服务状态由实例实时提供'}</p></section>
    </div>
    <section className="admin-panel"><div className="admin-panel-heading"><div><h2>最近操作</h2><p>管理变更记录</p></div><Button className="admin-link" onClick={() => navigate('audit')}>全部记录<Icon name="arrow" /></Button></div>{audit.isPending ? <div className="admin-empty" role="status">正在读取记录…</div> : audit.error ? <Problem error={audit.error} /> : audit.data.items.length === 0 ? <div className="admin-empty">这里还没有管理记录，之后的变更会自动记录。</div> : <div className="admin-activity-list">{audit.data.items.slice(0, 4).map(event => <div key={event.id}><span className="admin-activity-dot" /><div><strong>{auditLabel(event.action)}</strong><p>{event.actor || '已注销账号'}</p></div><time dateTime={event.created_at}>{formatTime(event.created_at)}</time></div>)}</div>}</section>
  </div>;
}
