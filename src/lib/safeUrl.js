export function normalizeHttpUrl(value, baseUrl) {
  try {
    const candidate = String(value || "").trim();
    if (!candidate || candidate.startsWith("//")) return "";
    const url = baseUrl ? new URL(candidate, baseUrl) : new URL(candidate);
    if (!["http:", "https:"].includes(url.protocol)) return "";
    if (url.username || url.password) return "";
    return url.href;
  } catch {
    return "";
  }
}

export function isHttpUrl(value) {
  return Boolean(normalizeHttpUrl(value));
}
