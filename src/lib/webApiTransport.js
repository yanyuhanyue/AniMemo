import axios from "axios";

import { isAuthInfrastructureRequest } from "./apiCore.js";

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
    const accessToken = session.getAccessToken();
    if (accessToken) config.headers.Authorization = `Bearer ${accessToken}`;
    return config;
  });

  api.interceptors.response.use(
    (response) => {
      onMutationSuccess?.(response.config);
      return response;
    },
    async (error) => {
      const request = error.config;
      if (
        error.response?.status !== 401
        || request?._retry
        || isAuthInfrastructureRequest(request?.url)
        || !unauthorizedHandler
      ) {
        return Promise.reject(error);
      }
      request._retry = true;
      return unauthorizedHandler({ request, client: api, error });
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
