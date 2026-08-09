export const SYNC_FIELD_LABELS = {
  watch_status: "观看状态",
  personal_score: "个人评分",
  review: "个人评价",
};

export const SYNC_STATE_LABELS = {
  in_sync: "已同步",
  uninitialized: "尚未确认差异",
  uninitialized_equal: "双方目前相同",
  local_changed: "AniMemo 已修改",
  remote_changed: "Bangumi 上的值发生了变化",
  converged: "双方相同",
  conflict: "AniMemo 和 Bangumi 都修改过",
  remote_missing: "Bangumi 尚未收藏",
  unsupported: "当前无法安全拉取",
};

const WATCH_STATUS_LABELS = {
  planned: "想看",
  watching: "在看",
  completed: "看过",
  on_hold: "搁置",
  dropped: "抛弃",
};

export function syncValueLabel(field, value) {
  if (!value?.present) return "未设置";
  if (field === "watch_status") return WATCH_STATUS_LABELS[value.value] || String(value.value);
  if (field === "personal_score") return String(value.value);
  if (field === "review" && value.value === "") return "空内容";
  return String(value.value ?? "");
}

export function syncEntryPatch(item = {}) {
  return {
    status: item.watch_status,
    statusLabel: item.watch_status_display || WATCH_STATUS_LABELS[item.watch_status],
    score: item.personal_score === null ? null : Number(item.personal_score),
    review: item.review || "",
    updatedAt: item.updated_at,
  };
}

export function syncUiActions(field = {}) {
  const recommended = Array.isArray(field.recommended_actions) ? field.recommended_actions : [];
  return recommended.filter((action) => action === "pull_remote" || action === "accept_equal");
}
