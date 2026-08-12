export const ACTIVE_UPDATE_STATES = new Set([
  "idle", "preflight", "fetching", "verifying", "backup", "pulling",
  "migrating", "switching", "verifying_health", "rolling_back",
]);

const STATE_LABELS = {
  idle: "等待执行",
  preflight: "环境预检",
  fetching: "读取发布资料",
  verifying: "验证签名与兼容性",
  backup: "创建数据库备份",
  pulling: "拉取不可变镜像",
  migrating: "执行数据库迁移",
  switching: "切换 API / Web",
  verifying_health: "稳定窗口检查",
  succeeded: "更新完成",
  failed_pre_switch: "切换前失败",
  failed_post_switch: "切换后失败",
  rolling_back: "正在回退应用",
  rolled_back: "应用已回退",
  manual_recovery_required: "需要人工恢复",
};

export function updateStateLabel(value) {
  return STATE_LABELS[value] || `未知状态（${value || "—"}）`;
}

export function compatibilityPresentation(value = {}) {
  if (value.allowed === false) return { tone: "blocked", label: "不可切换", detail: "当前数据库、配置或插件契约不兼容" };
  if (value.decision === "application_rollback") return { tone: "warning", label: "应用层可回退", detail: "数据库保持当前版本，不执行反向迁移" };
  if (value.rollbackMode === "application") return { tone: "warning", label: "可切换 / 条件回退", detail: "迁移后仅在旧应用兼容当前 schema 时回退" };
  return { tone: "safe", label: "可安全切换", detail: "API 与 Web 可按不可变 digest 切换" };
}

export function channelLabel(channel) {
  return ({ stable: "Stable", rc: "Release Candidate", beta: "Beta / Experimental" })[channel] || channel;
}

export function shortDigest(value = "") {
  return value.startsWith("sha256:") ? `${value.slice(0, 15)}…${value.slice(-8)}` : value || "—";
}
