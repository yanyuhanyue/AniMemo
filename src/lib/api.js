import axios from "axios";

function defaultApiUrl() {
  if (typeof window !== "undefined") return `${window.location.origin}/api`;
  return "http://localhost:8000/api";
}

const API_URL = (import.meta.env.VITE_API_BASE_URL || import.meta.env.VITE_API_URL || defaultApiUrl()).replace(/\/$/, "");
const LEGACY_ACCESS_KEY = "anime_journal_access";
const LEGACY_REFRESH_KEY = "anime_journal_refresh";

let accessToken = null;
let authUser = null;
let csrfToken = null;
let refreshPromise = null;
const authListeners = new Set();

function scrubLegacyTokens() {
  if (typeof window === "undefined") return;
  localStorage.removeItem(LEGACY_ACCESS_KEY);
  localStorage.removeItem(LEGACY_REFRESH_KEY);
  sessionStorage.removeItem(LEGACY_ACCESS_KEY);
  sessionStorage.removeItem(LEGACY_REFRESH_KEY);
}

scrubLegacyTokens();

function notifyAuthListeners() {
  const snapshot = { access: accessToken, user: authUser };
  authListeners.forEach((listener) => listener(snapshot));
}

export function setAccessToken(value) {
  accessToken = value || null;
  notifyAuthListeners();
}

export function getAccessToken() {
  return accessToken;
}

export function getAuthUser() {
  return authUser;
}

export function subscribeAuth(listener) {
  authListeners.add(listener);
  return () => authListeners.delete(listener);
}

export const getStoredTokens = () => ({ access: accessToken, refresh: null });

export function storeTokens({ access, user } = {}) {
  scrubLegacyTokens();
  accessToken = access || null;
  if (user !== undefined) authUser = user || null;
  notifyAuthListeners();
}

export function clearTokens() {
  scrubLegacyTokens();
  accessToken = null;
  authUser = null;
  notifyAuthListeners();
}

const cookieClient = axios.create({
  baseURL: `${API_URL}/`,
  timeout: 12000,
  withCredentials: true,
});

export const api = axios.create({
  baseURL: `${API_URL}/`,
  timeout: 12000,
  withCredentials: true,
});

async function ensureCsrfToken({ force = false } = {}) {
  if (csrfToken && !force) return csrfToken;
  const { data } = await cookieClient.get("auth/csrf/");
  csrfToken = data?.csrf_token || null;
  return csrfToken;
}

export function clearCsrfToken() {
  csrfToken = null;
}

async function cookiePost(path, data = {}, { includeAccess = false } = {}) {
  const token = await ensureCsrfToken();
  const headers = token ? { "X-CSRFToken": token } : {};
  if (includeAccess && accessToken) headers.Authorization = `Bearer ${accessToken}`;
  return cookieClient.post(path, data, {
    headers,
  });
}

export function refreshAccessToken() {
  if (!refreshPromise) {
    refreshPromise = cookiePost("token/refresh/")
      .then(({ data }) => {
        accessToken = data.access || null;
        authUser = data.user || authUser;
        notifyAuthListeners();
        return accessToken;
      })
      .catch((error) => {
        clearTokens();
        throw error;
      })
      .finally(() => {
        refreshPromise = null;
      });
  }
  return refreshPromise;
}

export async function initializeAuth() {
  try {
    await refreshAccessToken();
    const { data } = await api.get("auth/me/");
    authUser = { ...(authUser || {}), ...(data || {}) };
    notifyAuthListeners();
    return authUser;
  } catch {
    clearTokens();
    return null;
  }
}

api.interceptors.request.use((config) => {
  if (accessToken) config.headers.Authorization = `Bearer ${accessToken}`;
  return config;
});

function isAuthInfrastructureRequest(url = "") {
  return ["token/", "token/refresh/", "auth/csrf/", "auth/logout/"].some((path) => url.startsWith(path));
}

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const request = error.config;
    if (error.response?.status !== 401 || request?._retry || isAuthInfrastructureRequest(request?.url)) {
      return Promise.reject(error);
    }

    request._retry = true;
    try {
      const access = await refreshAccessToken();
      request.headers = request.headers || {};
      request.headers.Authorization = `Bearer ${access}`;
      return api(request);
    } catch (refreshError) {
      return Promise.reject(refreshError);
    }
  },
);

export function readableApiError(error, fallback = "操作失败，请稍后重试。") {
  if (error?.response?.status === 503) {
    return "安全服务暂时繁忙，请稍后重试。";
  }
  if (error?.response?.status === 403) {
    const detail = String(error?.response?.data?.detail || "");
    if (detail.toLowerCase().includes("csrf")) return "安全验证已过期，请刷新页面后重试。";
  }
  if (error?.response?.status === 429) {
    const retryAfter = Number(error.response.headers?.["retry-after"] || 0);
    return retryAfter > 0
      ? `操作过于频繁，请在 ${Math.ceil(retryAfter)} 秒后重试。`
      : "操作过于频繁，请稍后再试。";
  }
  const payload = error?.response?.data;
  if (!payload) return error?.message || fallback;
  if (typeof payload === "string") return payload;
  if (payload.detail) return payload.detail;
  const first = Object.values(payload).flat()[0];
  return first || fallback;
}

export const authApi = {
  login: async (username, password, turnstileToken = "") => {
    const { data } = await cookiePost("token/", { username, password, "cf-turnstile-response": turnstileToken });
    clearCsrfToken();
    await ensureCsrfToken({ force: true });
    return { data };
  },
  staffLogin: async (username, password, otp = "", recoveryCode = "", next = "", turnstileToken = "") => {
    const { data } = await cookiePost("auth/staff-login/", {
      username,
      password,
      otp,
      recovery_code: recoveryCode,
      next,
      "cf-turnstile-response": turnstileToken,
    });
    clearCsrfToken();
    await ensureCsrfToken({ force: true });
    return { data };
  },
  logout: async () => {
    try {
      await cookiePost("auth/logout/", {}, { includeAccess: true });
    } finally {
      clearTokens();
      clearCsrfToken();
    }
  },
  registerRequest: (email, turnstileToken = "") => cookiePost("auth/register/request/", { email, "cf-turnstile-response": turnstileToken }),
  verifyRegistration: (token) => cookiePost("auth/register/verify/", { token }),
  completeRegistration: (payload, turnstileToken = "") => cookiePost("auth/register/complete/", { ...payload, "cf-turnstile-response": turnstileToken }),
  reset: (email, turnstileToken = "") => api.post("auth/password-reset/", { email, "cf-turnstile-response": turnstileToken }),
  resetConfirm: (payload, turnstileToken = "") => api.post("auth/password-reset-confirm/", { ...payload, "cf-turnstile-response": turnstileToken }),
  changePassword: (payload) => api.post("auth/password-change/", payload),
  deleteAccount: (payload) => api.delete("auth/account/", { data: payload }),
};
