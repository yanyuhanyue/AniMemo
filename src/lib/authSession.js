export function createAuthSession() {
  let accessToken = null;
  let authUser = null;
  const listeners = new Set();

  const notify = () => {
    const snapshot = Object.freeze({ access: accessToken, user: authUser });
    listeners.forEach((listener) => listener(snapshot));
  };

  return Object.freeze({
    setAccessToken(value) {
      accessToken = value || null;
      notify();
    },
    getAccessToken: () => accessToken,
    getUser: () => authUser,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    store({ access, user } = {}) {
      accessToken = access || null;
      if (user !== undefined) authUser = user || null;
      notify();
    },
    mergeUser(user) {
      authUser = { ...(authUser || {}), ...(user || {}) };
      notify();
      return authUser;
    },
    clear() {
      accessToken = null;
      authUser = null;
      notify();
    },
  });
}
