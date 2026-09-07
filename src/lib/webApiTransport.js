import axios from "axios";

import { isAuthInfrastructureRequest } from "./apiCore.js";
import { createAuthSessionChangedError } from "./authSession.js";

export function createWebApiTransport({ baseURL, session, onMutationSuccess } = {}) {
  const options = {
    baseURL: `${String(baseURL || "").replace(/\/$/, "")}/`,
    timeout: 12000,
    withCredentials: true,
  };
  const cookieClient = axios.create(options);
  const api = axios.create(options);
  let unauthorizedHandler = null;

  api.interceptors.request.use((config) => {
    config._authGeneration ??= session.getGeneration();
    if (!session.isCurrent(config._authGeneration)) throw createAuthSessionChangedError();
    const accessToken = session.getAccessToken();
    if (accessToken) config.headers.Authorization = `Bearer ${accessToken}`;
    return config;
  });

  api.interceptors.response.use(
    (response) => {
      if (!session.isCurrent(response.config._authGeneration)) throw createAuthSessionChangedError();
      onMutationSuccess?.(response.config);
      return response;
    },
    async (error) => {
      const request = error.config;
      if (request?._authGeneration !== undefined && !session.isCurrent(request._authGeneration)) {
        return Promise.reject(createAuthSessionChangedError());
      }
      if (
        error.response?.status !== 401
        || request?._retry
        || isAuthInfrastructureRequest(request?.url)
        || !unauthorizedHandler
      ) {
        return Promise.reject(error);
      }
      request._retry = true;
      try {
        const response = await unauthorizedHandler({ request, client: api, error });
        if (!session.isCurrent(request._authGeneration)) throw createAuthSessionChangedError();
        return response;
      } catch (retryError) {
        if (!session.isCurrent(request._authGeneration)) throw createAuthSessionChangedError();
        throw retryError;
      }
    },
  );

  return Object.freeze({
    api,
    cookieClient,
    setUnauthorizedHandler(handler) {
      unauthorizedHandler = typeof handler === "function" ? handler : null;
    },
  });
}
