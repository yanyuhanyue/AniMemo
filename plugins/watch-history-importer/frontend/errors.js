export function readablePluginError(error, fallback) {
  const payload = error?.response?.data;
  if (typeof payload?.detail === "string" && payload.detail.trim()) return payload.detail;
  if (typeof payload?.message === "string" && payload.message.trim()) return payload.message;
  if (error?.response?.status === 403) return "当前账号没有运行此插件接口的权限。";
  if (error?.response?.status === 401) return "登录状态已失效，请重新登录。";
  if (error?.response?.status === 429) return "操作过于频繁，请稍后重试。";
  return String(fallback || "插件请求失败。");
}
