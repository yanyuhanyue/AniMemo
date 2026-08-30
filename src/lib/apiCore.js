export const API_VERSION = "v1";
export const API_ROOT_PATH = `/api/${API_VERSION}`;

export const AUTH_ENDPOINTS = Object.freeze({
  csrf: "auth/csrf/",
  login: "token/",
  refresh: "token/refresh/",
  me: "auth/me/",
  staffLogin: "auth/staff-login/",
  logout: "auth/logout/",
  registerRequest: "auth/register/request/",
  registerVerify: "auth/register/verify/",
  registerComplete: "auth/register/complete/",
  passwordReset: "auth/password-reset/",
  passwordResetConfirm: "auth/password-reset-confirm/",
  passwordChange: "auth/password-change/",
  account: "auth/account/",
});

export const INSTALLATION_ENDPOINTS = Object.freeze({
  status: "setup/status/",
  complete: "setup/",
});

export function resolveApiBaseUrl({ configuredBaseUrl = "", origin = "", fallbackOrigin = "http://localhost:8000" } = {}) {
  const configured = String(configuredBaseUrl || "").trim();
  if (configured) return configured.replace(/\/$/, "");
  return `${String(origin || fallbackOrigin).replace(/\/$/, "")}${API_ROOT_PATH}`;
}

export function isAuthInfrastructureRequest(url = "") {
  const raw = String(url || "").trim();
  let pathname = raw;
  try {
    pathname = new URL(raw, "http://animemo.local").pathname;
  } catch {
    // Keep the original value for unusual transport URLs.
  }
  const normalized = pathname
    .replace(/^\/+/, "")
    .replace(/^api\/(?:v1\/)?/, "");
  return [AUTH_ENDPOINTS.login, AUTH_ENDPOINTS.refresh, AUTH_ENDPOINTS.csrf, AUTH_ENDPOINTS.logout]
    .some((path) => normalized.startsWith(path));
}

export function normalizeAntiAbuseChallenge(value, provider = "turnstile") {
  if (typeof value === "string") {
    const token = value.trim();
    return token ? Object.freeze({ provider, token }) : null;
  }
  if (!value || typeof value !== "object") return null;
  const normalizedProvider = String(value.provider || provider).trim().toLowerCase();
  const token = String(value.token || "").trim();
  if (!normalizedProvider || !token) return null;
  return Object.freeze({ provider: normalizedProvider, token });
}

export function withAntiAbuseChallenge(payload, value) {
  const challenge = normalizeAntiAbuseChallenge(value);
  if (!challenge) return { ...payload };
  const next = { ...payload, challenge };
  if (challenge.provider === "turnstile") next["cf-turnstile-response"] = challenge.token;
  return next;
}

export function parseApiError(error, fallback = "操作失败，请稍后重试。") {
  const response = error?.response;
  const payload = response?.data;
  const status = response?.status ?? error?.status ?? null;
  const headers = response?.headers || {};
  const retryHeader = typeof headers.get === "function"
    ? headers.get("retry-after")
    : headers["retry-after"] ?? headers["Retry-After"];
  const retryAfter = Number(retryHeader ?? 0);
  const detail = payload && typeof payload === "object" && typeof payload.detail === "string"
    ? payload.detail
    : fallback;
  const statusCode = ({
    400: "invalid_request",
    401: "authentication_required",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    408: "request_timeout",
    409: "conflict",
    410: "not_found",
    413: "payload_too_large",
    422: "invalid_request",
    429: "rate_limited",
    500: "internal_error",
    502: "service_unavailable",
    503: "service_unavailable",
    504: "service_unavailable",
    507: "storage_exhausted",
  }[status] || "internal_error");
  const code = payload && typeof payload === "object" && typeof payload.code === "string"
    ? payload.code
    : statusCode;
  const correlationId = payload
    && typeof payload === "object"
    && typeof payload.correlation_id === "string"
    && /^[0-9a-f]{32}$/.test(payload.correlation_id)
    ? payload.correlation_id
    : null;
  return {
    code,
    detail: String(detail),
    correlationId,
    status,
    retryAfterSeconds: Number.isFinite(retryAfter) && retryAfter > 0 ? Math.ceil(retryAfter) : null,
  };
}

export function readableApiError(error, fallback = "操作失败，请稍后重试。") {
  const status = error?.response?.status;
  const parsed = parseApiError(error, fallback);
  if (parsed.code === "updater_unavailable") return parsed.detail || fallback;
  if (status === 503 || parsed.code === "service_unavailable") return "安全服务暂时繁忙，请稍后重试。";
  if (parsed.code === "csrf_failed") return "安全验证已过期，请刷新页面后重试。";
  if (status === 429 || parsed.code === "rate_limited") {
    return parsed.retryAfterSeconds
      ? `操作过于频繁，请在 ${parsed.retryAfterSeconds} 秒后重试。`
      : "操作过于频繁，请稍后再试。";
  }
  return parsed.detail || fallback;
}
