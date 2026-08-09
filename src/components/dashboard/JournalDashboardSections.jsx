import { Icon } from "../Icon.jsx";
import {
  buildSmartReminders,
  formatEpisodeRange,
  sortContinueWatching,
} from "../../lib/journalExperience.js";

const STATUS_METRICS = [
  ["watching", "在看"],
  ["completed", "看过"],
  ["planned", "想看"],
  ["on_hold", "搁置"],
  ["dropped", "弃番"],
];

function dateLabel(value, fallback = "还没有观看记录") {
  if (!value) return fallback;
  const parsed = new Date(`${value}T00:00:00`);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleDateString("zh-CN", { month: "short", day: "numeric" });
}

export function ContinueWatchingSection({ records, onOpen, onRecord, onComplete, onAdd }) {
  const watching = sortContinueWatching(records).slice(0, 6);
  return (
    <section className="journal-dashboard-band continue-watching" aria-labelledby="continue-watching-title">
      <header className="journal-dashboard-band__heading">
        <div><span>DAILY FLOW</span><h2 id="continue-watching-title"><Icon name="history" /> 继续观看</h2></div>
        <strong>{watching.length} 部在看</strong>
      </header>
      {watching.length ? (
        <div className="continue-watching__grid">
          {watching.map((record) => (
            <article className="continue-watching-card" key={record.id}>
              <button className="continue-watching-card__poster" type="button" onClick={(event) => onOpen(record, event.currentTarget)} aria-label={`打开 ${record.title}`}>
                <img src={record.poster} alt="" />
              </button>
              <div className="continue-watching-card__copy">
                <span className="continue-watching-card__status">在看</span>
                <h3>{record.title}</h3>
                <p>{formatEpisodeRange(record)} · {dateLabel(record.lastWatchedOn)}</p>
                <div>
                  <button type="button" className="is-primary" onClick={(event) => onRecord(record, event.currentTarget)}><Icon name="plus" /> 记录观看</button>
                  <button type="button" onClick={(event) => onOpen(record, event.currentTarget)}><Icon name="edit" /> 详情</button>
                  <button type="button" onClick={() => onComplete(record)}><Icon name="check" /> 看完</button>
                </div>
              </div>
            </article>
          ))}
        </div>
      ) : (
        <div className="journal-dashboard-empty"><Icon name="history" /><div><strong>目前没有正在看的作品</strong><p>把准备追的番剧切换为“在看”，它会出现在这里。</p></div><button type="button" onClick={onAdd}><Icon name="plus" /> 去添加</button></div>
      )}
    </section>
  );
}

export function DashboardAnalyticsSection({ analytics, error, onOpenEntry }) {
  const summary = analytics?.summary || {};
  const statuses = analytics?.status_distribution || {};
  const activity = analytics?.activity_summary || {};
  const recent = analytics?.recent_activity || [];
  const metrics = [
    ["总作品", summary.total ?? "—"],
    ...STATUS_METRICS.map(([key, label]) => [label, statuses[key] ?? "—"]),
    ["观看记录", summary.watch_history_count ?? "—"],
    ["活跃天数", summary.active_days ?? "—"],
  ];
  return (
    <section className="journal-dashboard-band dashboard-analytics" aria-labelledby="dashboard-analytics-title">
      <header className="journal-dashboard-band__heading">
        <div><span>CORE ANALYTICS</span><h2 id="dashboard-analytics-title"><Icon name="chart" /> 手账统计与最近动态</h2></div>
        <div className="dashboard-activity-summary"><span>今天 <b>{activity.today ?? 0}</b></span><span>近 7 天 <b>{activity.last_7_days ?? 0}</b></span><span>本月 <b>{activity.current_month ?? 0}</b></span></div>
      </header>
      {error ? <div className="journal-dashboard-error" role="alert"><Icon name="warning" /> {error}</div> : !analytics ? <div className="journal-dashboard-loading" role="status"><Icon name="spinner" spin /> 正在读取统计...</div> : (
        <div className="dashboard-analytics__layout">
          <div className="dashboard-metric-grid">{metrics.map(([label, value]) => <div className="dashboard-metric" key={label}><span>{label}</span><strong>{value}</strong></div>)}</div>
          <div className="dashboard-activity-list">
            <h3>最近观看动态</h3>
            {recent.length ? recent.map((item) => (
              <button type="button" key={item.id} onClick={() => onOpenEntry(item.entry_id)}>
                <time>{dateLabel(item.watched_on, "日期未知")}</time>
                <span><strong>{item.title}</strong><small>{formatEpisodeRange(item)} · {item.brush_label || "观看记录"}</small></span>
                <Icon name="arrow-right" />
              </button>
            )) : <p>还没有观看动态，记录一次观看后会显示在这里。</p>}
          </div>
        </div>
      )}
    </section>
  );
}

export function SmartReminderSection({ records, onOpen }) {
  const reminders = buildSmartReminders(records);
  if (!reminders.length) return null;
  return (
    <section className="journal-dashboard-band smart-reminders" aria-labelledby="smart-reminders-title">
      <header className="journal-dashboard-band__heading"><div><span>SMALL WINS</span><h2 id="smart-reminders-title"><Icon name="wand" /> 待完善</h2></div><strong>{reminders.length} 条建议</strong></header>
      <div className="smart-reminders__list">{reminders.map(({ type, record, message }) => (
        <button type="button" key={`${type}-${record.id}`} onClick={(event) => onOpen(record, event.currentTarget)}><Icon name={type === "unrated" ? "star" : type === "external" ? "link" : type === "poster" ? "film" : "hourglass"} /><span><strong>{record.title}</strong><small>{message}</small></span><Icon name="arrow-right" /></button>
      ))}</div>
    </section>
  );
}
