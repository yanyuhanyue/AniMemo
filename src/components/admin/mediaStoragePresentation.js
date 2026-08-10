export const SNAPSHOT_STALE_SECONDS = 2 * 60 * 60;

const STORAGE_STATE_LABELS = {
  AVAILABLE: "可用",
  WARNING: "容量预警",
  OFFLINE: "离线",
  WRITE_BLOCKED: "已停止新写入",
  DISABLED: "已停用",
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
  if (Number.isFinite(ageSeconds) && ageSeconds >= SNAPSHOT_STALE_SECONDS) {
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
