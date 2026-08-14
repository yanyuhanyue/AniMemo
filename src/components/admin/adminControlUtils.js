export const EMPTY_PAGE = { count: 0, page: 1, pages: 1, page_size: 20, results: [] };

export const resourceMeta = {
  columns: { kicker: "CONTENT MODERATION", title: "专栏审核队列", placeholder: "搜索专栏或作者", capability: "moderate_content" },
  journals: { kicker: "PUBLIC JOURNAL REVIEW", title: "公开手账审核", placeholder: "搜索昵称或邮箱", capability: "moderate_content" },
  users: { kicker: "ACCOUNT DIRECTORY", title: "用户、角色与安全", placeholder: "搜索用户名或邮箱", capability: "manage_users" },
  entries: { kicker: "JOURNAL MONITOR", title: "全部番剧记录", placeholder: "搜索番剧、用户或邮箱", capability: "moderate_content" },
  recycle: { kicker: "RECYCLE BIN", title: "内容回收站", placeholder: "搜索已移除内容", capability: "moderate_content" },
  audit: { kicker: "AUDIT TRAIL", title: "管理员操作审计", placeholder: "搜索动作、对象或管理员", capability: "view_audit" },
};

export const statusOptions = {
  columns: [["", "全部状态"], ["pending", "待审核"], ["approved", "已通过"], ["rejected", "已驳回"], ["removal_requested", "申请下架"], ["draft", "草稿"]],
  journals: [["", "全部状态"], ["pending", "待审核"], ["approved", "已公开"], ["private", "未公开"]],
  users: [["", "全部用户"], ["active", "已激活"], ["disabled", "已停用"], ["staff", "工作人员"]],
  entries: [["", "全部状态"], ["completed", "看过"], ["watching", "在看"], ["planned", "想看"], ["on_hold", "搁置"]],
};

export const auditActionMeta = {
  "settings.update": ["更新站点设置", "teal"],
  "settings.test_email": ["发送测试邮件", "teal"],
  "plugin.install": ["安装插件", "yellow"],
  "plugin.upgrade": ["升级插件", "pink"],
  "plugin.update": ["修改插件状态", "teal"],
  "column.review": ["审核精选专栏", "yellow"],
  "column.approve": ["通过精选专栏", "teal"],
  "column.reject": ["驳回精选专栏", "coral"],
  "column.recycle": ["回收精选专栏", "coral"],
  "column.restore": ["恢复精选专栏", "teal"],
  "journal.review": ["审核公开手账", "yellow"],
  "journal.approve": ["通过公开手账", "teal"],
  "journal.reject": ["撤销公开手账", "coral"],
  "entry.recycle": ["回收番剧记录", "coral"],
  "entry.restore": ["恢复番剧记录", "teal"],
  "user.permissions": ["修改用户权限", "coral"],
  "user.force_logout": ["强制用户退出", "coral"],
  "user.resend_activation": ["重发激活邮件", "teal"],
  "user.role_change": ["调整后台角色", "coral"],
  "system.backup_export": ["导出系统数据", "yellow"],
  "security.two_factor_enabled": ["启用两步验证", "teal"],
  "security.two_factor_disabled": ["关闭两步验证", "coral"],
  "tag.create": ["创建公共标签", "yellow"],
  "tag.update": ["更新公共标签", "teal"],
  "tag.delete": ["删除公共标签", "coral"],
};

export const auditTargetLabels = {
  User: "用户账号",
  UserSettings: "用户手账",
  JournalEntry: "番剧记录",
  Column: "精选专栏",
  PluginDeployment: "插件部署",
  SiteSettings: "站点设置",
  TagDefinition: "公共标签",
  system: "系统数据",
};

export const auditFieldLabels = {
  allow_sharing: "允许公开分享",
  clear_resend_api_key: "清除 Resend 密钥",
  clear_turnstile_secret: "清除 Turnstile Secret",
  config: "插件配置",
  deleted_at: "删除时间",
  effective_email_from: "实际发件地址",
  email_delivery_enabled: "邮件发送开关",
  email_delivery_ready: "邮件服务状态",
  email_sender_address: "发件邮箱",
  email_sender_name: "发件人名称",
  enabled: "启用状态",
  featured: "精选状态",
  homepage_description: "首页说明文字",
  homepage_title: "首页主标题",
  is_active: "账号状态",
  is_staff: "后台权限",
  is_quick_preset: "加入快捷预设",
  moderation_reason: "审核原因",
  name: "标签名称",
  public_status: "公开状态",
  reason: "操作原因",
  registration_enabled: "开放用户注册",
  resend_api_key: "Resend API 密钥",
  resend_api_key_configured: "Resend 密钥状态",
  resend_api_key_source: "Resend 密钥来源",
  turnstile_enabled: "启用 Turnstile",
  turnstile_site_key: "Turnstile Site Key",
  turnstile_secret_configured: "Turnstile Secret 状态",
  turnstile_ready: "Turnstile 服务状态",
  review_reason: "审核反馈",
  role: "后台角色",
  session_version: "会话版本",
  site_avatar: "站点头像",
  site_avatar_url: "站点头像地址",
  site_name: "站点名称",
  social_handle: "社交账号文字",
  sort_order: "标签排序",
  status: "内容状态",
  color: "默认颜色",
  universe_description: "共创宇宙说明",
  updated_at: "更新时间",
};

export function dateTimeLabel(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

export function auditActionInfo(action = "") {
  if (auditActionMeta[action]) return { label: auditActionMeta[action][0], tone: auditActionMeta[action][1] };
  const fallback = action.split(".").filter(Boolean).join(" / ") || "未知操作";
  return { label: fallback, tone: "plain" };
}

export function auditTargetLabel(item) {
  const explicit = String(item?.target_label || "").trim();
  if (explicit) return explicit;
  const type = auditTargetLabels[item?.target_type] || item?.target_type || "未知对象";
  return item?.target_id ? `${type} #${item.target_id}` : type;
}

export function auditValueLabel(value, missing = "未设置") {
  if (value === undefined) return missing;
  if (value === null || value === "") return "空值";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

export function hasCapability(viewer, capability) {
  return viewer?.is_superuser || viewer?.capabilities?.includes(capability);
}

export function resourceStatus(item, kind) {
  if (kind === "entries") {
    const tone = item.watch_status === "completed"
      ? "approved"
      : item.watch_status === "watching"
        ? "teal"
        : item.watch_status === "planned"
          ? "pending"
          : "private";
    return { label: item.status || "未设置", tone };
  }
  if (kind === "users") {
    const roleLabel = item.staff_role_display
      || (item.is_superuser ? "超级管理员" : item.is_staff ? "管理员" : "普通用户");
    const roleTone = item.is_superuser ? "superuser" : item.is_staff ? "staff" : "user";
    return {
      label: item.is_active ? "已激活" : "已停用",
      tone: item.is_active ? "approved" : "pending",
      secondaryLabel: roleLabel,
      secondaryTone: roleTone,
    };
  }
  const label = item.status_display || item.public_status_display || item.event_type || item.staff_role_display
    || (item.is_active === undefined ? "—" : item.is_active ? "已激活" : "已停用");
  const tone = item.status || item.public_status || (item.is_active === undefined ? "plain" : item.is_active ? "approved" : "pending");
  return { label, tone };
}

export function downloadBlob(blob, disposition, fallback) {
  const match = disposition?.match(/filename="?([^";]+)"?/i);
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = match?.[1] || fallback;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
