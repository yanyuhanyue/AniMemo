import { AUTH_ENDPOINTS, INSTALLATION_ENDPOINTS, withAntiAbuseChallenge } from "./apiCore.js";
import { createAuthSessionChangedError } from "./authSession.js";


// Security denylist: remove obsolete browser-stored tokens without accepting them.
const INSECURE_LEGACY_ACCESS_KEY = "anime_journal_access";
const INSECURE_LEGACY_REFRESH_KEY = "anime_journal_refresh";

export function createWebAuthAdapter({ api, cookieClient, session, browser = null } = {}) {
  let csrfToken = null;
  let csrfGeneration = null;
  let refreshPromise = null;
  let refreshGeneration = null;
  let initializationPromise = null;
  let initializationGeneration = null;
  const loginGenerations = new WeakMap();

  function assertCurrent(generation) {
    if (!session.isCurrent(generation)) throw createAuthSessionChangedError();
  }

  function assertCookieSessionReady(generation) {
    assertCurrent(generation);
    if (session.isChangingIdentity()) throw createAuthSessionChangedError();
  }

  function scrubLegacyTokens() {
    for (const name of ["localStorage", "sessionStorage"]) {
      let storage;
      try {
        storage = browser?.[name];
      } catch {
        continue;
      }
      for (const key of [INSECURE_LEGACY_ACCESS_KEY, INSECURE_LEGACY_REFRESH_KEY]) {
        try {
          storage?.removeItem(key);
        } catch {
          // Restricted storage must not prevent in-memory authentication cleanup.
        }
      }
    }
  }

  scrubLegacyTokens();

  async function ensureCsrfToken({ force = false, generation = session.getGeneration() } = {}) {
    assertCurrent(generation);
    if (csrfToken && csrfGeneration === generation && !force) return csrfToken;
    const { data } = await cookieClient.get(AUTH_ENDPOINTS.csrf);
    assertCurrent(generation);
    csrfToken = data?.csrf_token || null;
    csrfGeneration = generation;
    return csrfToken;
  }

  function clearCsrfToken() {
    csrfToken = null;
    csrfGeneration = null;
  }

  async function cookiePost(path, data = {}, { includeAccess = false, generation = session.getGeneration() } = {}) {
    assertCurrent(generation);
    if (path !== AUTH_ENDPOINTS.login && path !== AUTH_ENDPOINTS.staffLogin) assertCookieSessionReady(generation);
    const accessToken = session.getAccessToken();
    const token = await ensureCsrfToken({ generation });
    assertCurrent(generation);
    const headers = token ? { "X-CSRFToken": token } : {};
    if (includeAccess && accessToken) headers.Authorization = `Bearer ${accessToken}`;
    return cookieClient.post(path, data, { headers });
  }

  function clearTokens(generation = session.getGeneration()) {
    scrubLegacyTokens();
    if (!session.clear(generation)) return false;
    clearCsrfToken();
    return true;
  }

  function storeTokens(value = {}) {
    scrubLegacyTokens();
    const loginGeneration = loginGenerations.get(value);
    if (loginGeneration !== undefined && !session.isCurrent(loginGeneration)) return false;
    // The committed identity also invalidates requests issued while login was pending.
    return session.store(value);
  }

  function refreshAccessToken(generation = session.getGeneration()) {
    assertCookieSessionReady(generation);
    if (!refreshPromise || refreshGeneration !== generation) {
      const request = cookiePost(AUTH_ENDPOINTS.refresh, {}, { generation })
        .then(({ data }) => {
          assertCurrent(generation);
          session.store({ access: data.access || null, user: data.user ?? session.getUser() }, generation);
          return session.getAccessToken();
        })
        .catch((error) => {
          assertCurrent(generation);
          clearTokens(generation);
          throw error;
        })
        .finally(() => {
          if (refreshPromise === request) {
            refreshPromise = null;
            refreshGeneration = null;
          }
        });
      refreshPromise = request;
      refreshGeneration = generation;
    }
    return refreshPromise;
  }

  function initializeAuth() {
    const generation = session.getGeneration();
    if (!initializationPromise || initializationGeneration !== generation) {
      const request = (async () => {
        try {
          await refreshAccessToken(generation);
          assertCurrent(generation);
          const { data } = await api.get(AUTH_ENDPOINTS.me, { _authGeneration: generation });
          return session.mergeUser(data, generation);
        } catch {
          clearTokens(generation);
          return null;
        } finally {
          if (initializationPromise === request) {
            initializationPromise = null;
            initializationGeneration = null;
          }
        }
      })();
      initializationPromise = request;
      initializationGeneration = generation;
    }
    return initializationPromise;
  }

  async function loginWithCookie(path, payload) {
    const generation = session.beginIdentityChange();
    try {
      const { data } = await cookiePost(
        path,
        payload,
        { generation },
      );
      assertCurrent(generation);
      clearCsrfToken();
      await ensureCsrfToken({ force: true, generation });
      assertCurrent(generation);
      loginGenerations.set(data, generation);
      return { data };
    } catch (error) {
      // A response may already have changed cookies even when its body/CSRF step fails.
      clearTokens(generation);
      throw error;
    }
  }

  const authApi = Object.freeze({
    login: (username, password, challenge = "") => loginWithCookie(
      AUTH_ENDPOINTS.login,
      withAntiAbuseChallenge({ username, password }, challenge),
    ),
    staffLogin: (username, password, otp = "", recoveryCode = "", next = "", challenge = "") => loginWithCookie(
      AUTH_ENDPOINTS.staffLogin,
      withAntiAbuseChallenge({ username, password, otp, recovery_code: recoveryCode, next }, challenge),
    ),
    logout: async () => {
      const generation = session.advanceGeneration();
      try {
        await cookiePost(AUTH_ENDPOINTS.logout, {}, { includeAccess: true, generation });
      } finally {
        clearTokens(generation);
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
    changePassword: async (payload) => {
      assertCookieSessionReady(session.getGeneration());
      return api.post(AUTH_ENDPOINTS.passwordChange, payload);
    },
    deleteAccount: async (payload) => {
      assertCookieSessionReady(session.getGeneration());
      return api.delete(AUTH_ENDPOINTS.account, { data: payload });
    },
  });

  const csrfApi = Object.freeze({
    async post(path, data = {}, config = {}) {
      const generation = session.getGeneration();
      assertCookieSessionReady(generation);
      const token = await ensureCsrfToken({ generation });
      assertCurrent(generation);
      return api.post(path, data, {
        ...config,
        _authGeneration: generation,
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
      const generation = request._authGeneration ?? session.getGeneration();
      assertCurrent(generation);
      const access = await refreshAccessToken(generation);
      assertCurrent(generation);
      request._authGeneration = generation;
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
