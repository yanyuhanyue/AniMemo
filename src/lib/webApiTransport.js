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
  const pendingRequests = new Map();
  let unauthorizedHandler = null;

  function releaseRequest(config) {
    const controller = config?._authController;
    pendingRequests.get(controller)?.removeCallerListener();
    pendingRequests.delete(controller);
  }

  session.subscribeGeneration((generation) => {
    for (const [controller, pending] of pendingRequests) {
      if (pending.generation === generation) continue;
      pending.removeCallerListener();
      pendingRequests.delete(controller);
      // Stop the browser from applying old Set-Cookie headers, before any new login.
      controller.abort();
    }
  });

  function bindRequest(config, includeAccess) {
    config._authGeneration ??= session.getGeneration();
    if (!session.isCurrent(config._authGeneration)) throw createAuthSessionChangedError();
    if (includeAccess && session.isChangingIdentity()) throw createAuthSessionChangedError();
    const controller = new AbortController();
    const callerSignal = Object.hasOwn(config, "_authCallerSignal") ? config._authCallerSignal : config.signal;
    const abortFromCaller = () => controller.abort(callerSignal.reason);
    callerSignal?.addEventListener("abort", abortFromCaller, { once: true });
    if (callerSignal?.aborted) abortFromCaller();
    config._authCallerSignal = callerSignal || null;
    config._authController = controller;
    config.signal = controller.signal;
    pendingRequests.set(controller, {
      generation: config._authGeneration,
      removeCallerListener: () => callerSignal?.removeEventListener("abort", abortFromCaller),
    });
    if (includeAccess) {
      const accessToken = session.getAccessToken();
      if (accessToken) config.headers.Authorization = `Bearer ${accessToken}`;
    }
    return config;
  }

  function staleRequestError(error) {
    const generation = error.config?._authGeneration;
    return generation !== undefined && !session.isCurrent(generation) ? createAuthSessionChangedError() : error;
  }

  // Capture identity during the API call, before a same-turn login can replace it.
  api.interceptors.request.use((config) => bindRequest(config, true), undefined, { synchronous: true });
  cookieClient.interceptors.request.use((config) => bindRequest(config, false), undefined, { synchronous: true });
  cookieClient.interceptors.response.use((response) => {
    releaseRequest(response.config);
    if (!session.isCurrent(response.config._authGeneration)) throw createAuthSessionChangedError();
    return response;
  }, (error) => {
    releaseRequest(error.config);
    return Promise.reject(staleRequestError(error));
  });

  api.interceptors.response.use(
    (response) => {
      releaseRequest(response.config);
      if (!session.isCurrent(response.config._authGeneration)) throw createAuthSessionChangedError();
      onMutationSuccess?.(response.config);
      return response;
    },
    async (error) => {
      releaseRequest(error.config);
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
