import { AUTH_ENDPOINTS, INSTALLATION_ENDPOINTS, withAntiAbuseChallenge } from "./apiCore.js";


// Security denylist: remove obsolete browser-stored tokens without accepting them.
const INSECURE_LEGACY_ACCESS_KEY = "anime_journal_access";
const INSECURE_LEGACY_REFRESH_KEY = "anime_journal_refresh";

export function createWebAuthAdapter({ api, cookieClient, session, browser = null } = {}) {
  let csrfToken = null;
  let refreshPromise = null;

  function scrubLegacyTokens() {
    for (const storage of [browser?.localStorage, browser?.sessionStorage]) {
      storage?.removeItem(INSECURE_LEGACY_ACCESS_KEY);
      storage?.removeItem(INSECURE_LEGACY_REFRESH_KEY);
    }
  }

  scrubLegacyTokens();

  async function ensureCsrfToken({ force = false } = {}) {
    if (csrfToken && !force) return csrfToken;
    const { data } = await cookieClient.get(AUTH_ENDPOINTS.csrf);
    csrfToken = data?.csrf_token || null;
    return csrfToken;
  }

  function clearCsrfToken() {
    csrfToken = null;
  }

  async function cookiePost(path, data = {}, { includeAccess = false } = {}) {
    const token = await ensureCsrfToken();
    const headers = token ? { "X-CSRFToken": token } : {};
    const accessToken = session.getAccessToken();
    if (includeAccess && accessToken) headers.Authorization = `Bearer ${accessToken}`;
    return cookieClient.post(path, data, { headers });
  }

  function clearTokens() {
    scrubLegacyTokens();
    session.clear();
  }

  function storeTokens(value = {}) {
    scrubLegacyTokens();
    session.store(value);
  }

  function refreshAccessToken() {
    if (!refreshPromise) {
      refreshPromise = cookiePost(AUTH_ENDPOINTS.refresh)
        .then(({ data }) => {
          session.store({ access: data.access || null, user: data.user ?? session.getUser() });
          return session.getAccessToken();
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

  async function initializeAuth() {
    try {
      await refreshAccessToken();
      const { data } = await api.get(AUTH_ENDPOINTS.me);
      return session.mergeUser(data);
    } catch {
      clearTokens();
      return null;
    }
  }

  const authApi = Object.freeze({
    login: async (username, password, challenge = "") => {
      const { data } = await cookiePost(
        AUTH_ENDPOINTS.login,
        withAntiAbuseChallenge({ username, password }, challenge),
      );
      clearCsrfToken();
      await ensureCsrfToken({ force: true });
      return { data };
    },
    staffLogin: async (username, password, otp = "", recoveryCode = "", next = "", challenge = "") => {
      const { data } = await cookiePost(
        AUTH_ENDPOINTS.staffLogin,
        withAntiAbuseChallenge({ username, password, otp, recovery_code: recoveryCode, next }, challenge),
      );
      clearCsrfToken();
      await ensureCsrfToken({ force: true });
      return { data };
    },
    logout: async () => {
      try {
        await cookiePost(AUTH_ENDPOINTS.logout, {}, { includeAccess: true });
      } finally {
        clearTokens();
        clearCsrfToken();
      }
    },
    registerRequest: (email, challenge = "") => cookiePost(
      AUTH_ENDPOINTS.registerRequest,
      withAntiAbuseChallenge({ email }, challenge),
    ),
    verifyRegistration: (token) => cookiePost(AUTH_ENDPOINTS.registerVerify, { token }),
    completeRegistration: (payload, challenge = "") => cookiePost(
      AUTH_ENDPOINTS.registerComplete,
      withAntiAbuseChallenge(payload, challenge),
    ),
    reset: (email, challenge = "") => api.post(
      AUTH_ENDPOINTS.passwordReset,
      withAntiAbuseChallenge({ email }, challenge),
    ),
    resetConfirm: (payload, challenge = "") => api.post(
      AUTH_ENDPOINTS.passwordResetConfirm,
      withAntiAbuseChallenge(payload, challenge),
    ),
    changePassword: (payload) => api.post(AUTH_ENDPOINTS.passwordChange, payload),
    deleteAccount: (payload) => api.delete(AUTH_ENDPOINTS.account, { data: payload }),
  });

  const csrfApi = Object.freeze({
    async post(path, data = {}, config = {}) {
      const token = await ensureCsrfToken();
      return api.post(path, data, {
        ...config,
        headers: { ...(config.headers || {}), ...(token ? { "X-CSRFToken": token } : {}) },
      });
    },
  });

  const setupApi = Object.freeze({
    status: () => cookieClient.get(INSTALLATION_ENDPOINTS.status),
    complete: (payload) => cookiePost(INSTALLATION_ENDPOINTS.complete, payload),
  });

  return Object.freeze({
    authApi,
    csrfApi,
    clearCsrfToken,
    clearTokens,
    getStoredTokens: () => ({ access: session.getAccessToken(), refresh: null }),
    handleUnauthorized: async ({ request, client }) => {
      const access = await refreshAccessToken();
      request.headers = request.headers || {};
      request.headers.Authorization = `Bearer ${access}`;
      return client(request);
    },
    initializeAuth,
    refreshAccessToken,
    scrubLegacyTokens,
    setupApi,
    storeTokens,
  });
}
