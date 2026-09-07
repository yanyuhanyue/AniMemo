export function createAuthSessionChangedError() {
  const error = new Error("登录状态已变化，请重试。");
  error.code = "AUTH_SESSION_CHANGED";
  return error;
}

export function createAuthSession() {
  let generation = 0;
  let accessToken = null;
  let authUser = null;
  let changingIdentity = false;
  const listeners = new Set();
  const generationListeners = new Set();

  const advanceGeneration = (identityChange = false) => {
    generation += 1;
    changingIdentity = identityChange;
    generationListeners.forEach((listener) => listener(generation));
    return generation;
  };

  const notify = () => {
    const snapshot = Object.freeze({ access: accessToken, user: authUser, generation });
    listeners.forEach((listener) => listener(snapshot));
  };

  return Object.freeze({
    getGeneration: () => generation,
    isCurrent: (expectedGeneration) => expectedGeneration === generation,
    advanceGeneration: () => advanceGeneration(),
    beginIdentityChange: () => advanceGeneration(true),
    isChangingIdentity: () => changingIdentity,
    subscribeGeneration(listener) {
      generationListeners.add(listener);
      return () => generationListeners.delete(listener);
    },
    setAccessToken(value, expectedGeneration = generation) {
      if (expectedGeneration !== generation) return false;
      accessToken = value || null;
      notify();
      return true;
    },
    getAccessToken: () => accessToken,
    getUser: () => authUser,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    store({ access, user } = {}, expectedGeneration) {
      if (expectedGeneration !== undefined && expectedGeneration !== generation) return false;
      if (expectedGeneration === undefined) advanceGeneration();
      accessToken = access || null;
      if (user !== undefined) authUser = user || null;
      notify();
      return true;
    },
    mergeUser(user, expectedGeneration = generation) {
      if (expectedGeneration !== generation) return null;
      authUser = { ...(authUser || {}), ...(user || {}) };
      notify();
      return authUser;
    },
    clear(expectedGeneration = generation) {
      if (expectedGeneration !== generation) return false;
      advanceGeneration();
      accessToken = null;
      authUser = null;
      notify();
      return true;
    },
  });
}
