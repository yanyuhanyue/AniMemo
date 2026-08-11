import { parseApiError, readableApiError, resolveApiBaseUrl } from "./apiCore.js";
import { createAuthSession } from "./authSession.js";
import { invalidateServerStateForRequest } from "./serverState.js";
import { createWebApiTransport } from "./webApiTransport.js";
import { createWebAuthAdapter } from "./webAuthAdapter.js";


const runtimeEnv = import.meta.env || {};
const browser = typeof window !== "undefined" ? window : null;
const API_URL = resolveApiBaseUrl({
  configuredBaseUrl: runtimeEnv.VITE_API_BASE_URL || runtimeEnv.VITE_API_URL,
  origin: browser?.location?.origin,
});
const session = createAuthSession();
const transport = createWebApiTransport({
  baseURL: API_URL,
  session,
  onMutationSuccess: invalidateServerStateForRequest,
});
const webAuth = createWebAuthAdapter({
  api: transport.api,
  cookieClient: transport.cookieClient,
  session,
  browser,
});

transport.setUnauthorizedHandler(webAuth.handleUnauthorized);

export const api = transport.api;
export const authApi = webAuth.authApi;
export const clearCsrfToken = webAuth.clearCsrfToken;
export const clearTokens = webAuth.clearTokens;
export const getAccessToken = session.getAccessToken;
export const getAuthUser = session.getUser;
export const getStoredTokens = webAuth.getStoredTokens;
export const initializeAuth = webAuth.initializeAuth;
export const refreshAccessToken = webAuth.refreshAccessToken;
export const setAccessToken = session.setAccessToken;
export const storeTokens = webAuth.storeTokens;
export const subscribeAuth = session.subscribe;

export { parseApiError, readableApiError };
