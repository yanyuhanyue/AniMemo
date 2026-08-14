export const DEFAULT_TRUSTED_POSTER_HOSTS = [
  "lain.bgm.tv",
  "media.animemo.cc",
];

export function normalizeTrustedPosterHosts(values) {
  const normalized = Array.isArray(values)
    ? values.map((value) => String(value || "").trim().toLowerCase().replace(/\.$/, "")).filter(Boolean)
    : [];
  return [...new Set(normalized.length ? normalized : DEFAULT_TRUSTED_POSTER_HOSTS)];
}

export function validateTrustedPosterUrl(value, trustedHosts = DEFAULT_TRUSTED_POSTER_HOSTS) {
  const normalized = String(value || "").trim();
  if (!normalized) return "";
  try {
    const url = new URL(normalized);
    const hostname = url.hostname.toLowerCase().replace(/\.$/, "");
    const looksLikeIpv4 = /^\d{1,3}(?:\.\d{1,3}){3}$/.test(hostname);
    const looksLikeIpv6 = hostname.includes(":");
    if (url.protocol !== "https:" || url.username || url.password || looksLikeIpv4 || looksLikeIpv6) {
      return "封面必须使用受信任域名的 HTTPS 地址。";
    }
    if (!normalizeTrustedPosterHosts(trustedHosts).includes(hostname)) {
      return "该图片域名不在管理员维护的可信白名单中。";
    }
    return "";
  } catch {
    return "请输入有效的 HTTPS 图片地址。";
  }
}
