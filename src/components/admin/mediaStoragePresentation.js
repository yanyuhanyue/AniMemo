export const SNAPSHOT_STALE_SECONDS = 2 * 60 * 60;

const STORAGE_STATE_LABELS = {
  AVAILABLE: "可用",
  WARNING: "容量预警",
  OFFLINE: "离线",
  WRITE_BLOCKED: "已停止新写入",
  DISABLED: "已停用",
};

const REFRESH_FEEDBACK_LABELS = {
  CLOUDFLARE_ANALYTICS_UPDATED: "容量数据已更新",
  CLOUDFLARE_ANALYTICS_NO_DATA: "Cloudflare 暂未生成该 Bucket 的统计数据。",
  CLOUDFLARE_ANALYTICS_AUTH_FAILED: "Cloudflare Analytics 权限验证失败，请检查 Analytics 凭证权限。",
};

export function bytesLabel(value, unit = 1_000_000_000, suffix = "GB", emptyLabel = "暂无统计数据") {
  if (value == null || !Number.isFinite(Number(value))) return emptyLabel;
  return `${(Number(value) / unit).toFixed(2)} ${suffix}`;
}

export function storageStateLabel(status) {
  const normalized = String(status || "").trim().toUpperCase();
  if (!normalized) return "未知";
  return STORAGE_STATE_LABELS[normalized] || `未知（${normalized}）`;
}

export function analyticsSnapshotPresentation(item) {
  const hasSnapshot = item?.usage_refreshed_at && item?.usage?.actual_bytes != null;
  if (!hasSnapshot) {
    return {
      status: "NO_DATA",
      label: "暂无统计数据",
      detail: "尚无成功同步的 Analytics 快照",
      tone: "is-empty",
    };
  }
  const ageSeconds = Number(item?.usage?.snapshot_age_seconds);
  if (Number.isFinite(ageSeconds) && ageSeconds > SNAPSHOT_STALE_SECONDS) {
    return {
      status: "STALE",
      label: "快照已过期",
      detail: "数据已超过 2 小时未更新",
      tone: "is-stale",
    };
  }
  return {
    status: "FRESH",
    label: "数据新鲜",
    detail: "最近一次 Analytics 同步有效",
    tone: "is-fresh",
  };
}

export function refreshFeedbackLabel(refresh) {
  const code = String(refresh?.code || "").trim().toUpperCase();
  if (REFRESH_FEEDBACK_LABELS[code]) return REFRESH_FEEDBACK_LABELS[code];
  if (String(refresh?.status || "").toUpperCase() === "FAILED") {
    return "Analytics 读取失败，上次成功快照已保留。";
  }
  return "存储容量刷新已完成。";
}

export function formatLocalDateTime(value, locales) {
  if (!value) return "暂无成功同步";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间格式无效";
  return new Intl.DateTimeFormat(locales, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZoneName: "short",
  }).format(date);
}
