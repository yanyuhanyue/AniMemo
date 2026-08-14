/** Public frontend contract for AniMemo Plugin SDK v2. */
export const PLUGIN_SDK_VERSION = "2.0.0";

export const HOST_SDK_CAPABILITIES = Object.freeze([
  "api.v2",
  "auth.readonly.v2",
  "navigation.v2",
  "ui.notify",
  "ui.confirm",
  "events.v2",
  "site.readonly.v1",
  "plugin.permissions.v2",
]);

const SLUG_PATTERN = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/;
const CORE_EVENTS = new Set([
  "auth.login",
  "auth.logout",
  "auth.userChanged",
  "journal.created",
  "journal.updated",
  "journal.deleted",
  "column.created",
  "column.updated",
  "column.deleted",
  "site.settingsUpdated",
  "plugin.enabled",
  "plugin.disabled",
]);

function normalizePath(path) {
  return String(path || "").replace(/^\/+/, "");
}

function createApiFacade(client, prefix = "") {
  const requestPath = (path) => `${prefix}${normalizePath(path)}`;
  const facade = {
    get: (path, config) => client.get(requestPath(path), config),
    post: (path, data, config) => client.post(requestPath(path), data, config),
    put: (path, data, config) => client.put(requestPath(path), data, config),
    patch: (path, data, config) => client.patch(requestPath(path), data, config),
    delete: (path, config) => client.delete(requestPath(path), config),
    plugin: (slug) => {
      if (!SLUG_PATTERN.test(String(slug || ""))) throw new Error("Invalid plugin slug");
      return createApiFacade(client, `plugins/${slug}/`);
    },
  };
  return Object.freeze(facade);
}

export function createEventBus() {
  const listeners = new Map();
  const subscribe = (eventName, listener, once = false) => {
    if (typeof listener !== "function") return () => {};
    const bucket = listeners.get(eventName) || new Set();
    const wrapped = once
      ? (payload) => { off(eventName, wrapped); listener(payload); }
      : listener;
    bucket.add(wrapped);
    listeners.set(eventName, bucket);
    return () => off(eventName, wrapped);
  };
  const on = (eventName, listener) => subscribe(String(eventName || ""), listener);
  const once = (eventName, listener) => subscribe(String(eventName || ""), listener, true);
  const off = (eventName, listener) => {
    const bucket = listeners.get(eventName);
    if (!bucket) return;
    bucket.delete(listener);
    if (!bucket.size) listeners.delete(eventName);
  };
  const emit = (eventName, payload) => {
    (listeners.get(eventName) || new Set()).forEach((listener) => listener(payload));
  };
  return Object.freeze({ on, once, off, emit, clear: () => listeners.clear(), isCoreEvent: (name) => CORE_EVENTS.has(name) });
}

function safeSiteSettings(settings) {
  const value = settings && typeof settings === "object" ? settings : {};
  const blocked = /secret|token|password|cookie|resend|api[_-]?key|csrf|totp|recovery/i;
  return Object.freeze(Object.fromEntries(Object.entries(value).filter(([key]) => !blocked.test(key))));
}

export function createPluginHost({
  slug,
  client,
  authStore,
  navigation,
  ui,
  site,
  eventBus,
  manifest = {},
}) {
  if (!SLUG_PATTERN.test(String(slug || ""))) throw new Error("Invalid plugin slug");
  const getReadonlyUser = () => {
    const user = authStore?.getUser?.();
    if (!user || typeof user !== "object") return null;
    return Object.freeze({ ...user, capabilities: Object.freeze([...(user.capabilities || [])]) });
  };
  const auth = Object.freeze({
    getUser: getReadonlyUser,
    isAuthenticated: () => Boolean(authStore?.getUser?.()),
    isStaff: () => Boolean(authStore?.getUser?.()?.is_staff),
    getRole: () => authStore?.getUser?.()?.role || authStore?.getUser?.()?.staff_role || null,
    getCapabilities: () => Object.freeze([...(authStore?.getUser?.()?.capabilities || [])]),
    getPluginPermissions: () => Object.freeze([...(authStore?.getUser?.()?.pluginPermissions || [])]),
    subscribe: (listener) => authStore?.subscribe?.((snapshot = {}) => {
      const user = snapshot.user && typeof snapshot.user === "object"
        ? Object.freeze({ ...snapshot.user, capabilities: Object.freeze([...(snapshot.user.capabilities || [])]) })
        : null;
      listener?.(Object.freeze({ user, authenticated: Boolean(user) }));
    }) || (() => {}),
  });

  const events = Object.freeze({
    on: eventBus.on,
    once: eventBus.once,
    off: eventBus.off,
    emit: (eventName, payload) => {
      const name = String(eventName || "");
      if (!name.startsWith(`plugin:${slug}:`)) {
        throw new Error(`Plugins may only emit their own namespaced events: plugin:${slug}:*`);
      }
      eventBus.emit(name, payload);
    },
  });

  const capabilities = Object.freeze({
    has: (capability) => HOST_SDK_CAPABILITIES.includes(capability),
    list: () => Object.freeze([...HOST_SDK_CAPABILITIES]),
  });

  return Object.freeze({
    sdkVersion: PLUGIN_SDK_VERSION,
    api: createApiFacade(client),
    auth,
    navigation: Object.freeze({
      navigate: navigation.navigate,
      replace: navigation.replace,
      back: navigation.back,
    }),
    ui: Object.freeze({
      notify: ui.notify,
      confirm: ui.confirm,
    }),
    events,
    site: Object.freeze({
      getSettings: () => safeSiteSettings(site.getSettings?.()),
      getName: () => String(site.getName?.() || "AniMemo"),
      getBaseUrl: () => String(site.getBaseUrl?.() || (typeof window !== "undefined" ? window.location.origin : "")),
      subscribeSettings: site.subscribeSettings,
    }),
    capabilities,
    manifest: Object.freeze({
      extensions: Object.freeze([...(manifest.extensions || [])]),
      permissions: Object.freeze([...(manifest.permissions || [])]),
    }),
  });
}

export function validatePluginRoute(route, slug, manifest = {}) {
  const path = String(route?.path || "");
  const prefix = `/plugins/${slug}`;
  if (typeof route?.Component !== "function" || !path.startsWith(prefix) || (path !== prefix && !path.startsWith(`${prefix}/`))) {
    return { valid: false, reason: "插件路由必须位于自身 /plugins/<slug>/ 命名空间。" };
  }
  if (path.includes("..") || /^\/+(login|register|admin|settings)(?:\/|$)/.test(path)) {
    return { valid: false, reason: "插件不能覆盖核心路由。" };
  }
  const extensions = new Set(manifest.extensions || []);
  const area = route.area || "dashboard";
  if (!extensions.has("frontend.page")) return { valid: false, reason: "Manifest 未声明 frontend.page。" };
  const access = route.access || "public";
  if (!["public", "auth", "staff"].includes(access)) return { valid: false, reason: "插件路由 access 无效。" };
  return { valid: true, area, access };
}

export function collectPluginNavigation(plugins, area) {
  const seen = new Set();
  const items = [];
  (plugins || []).forEach(({ slug, plugin, manifest, status }) => {
    if (status !== "loaded" || !plugin) return;
    const declaredAreas = new Set(manifest.extensions || []);
    const navigation = Array.isArray(plugin.navigation)
      ? plugin.navigation.filter((item) => {
        const itemArea = String(item?.area || "dashboard");
        return (!area || itemArea === area) && declaredAreas.has("frontend.navigation");
      })
      : [];
    navigation.forEach((item, index) => {
      const id = String(item?.id || `${slug}.${index}`);
      const path = String(item?.path || "");
      const prefix = `/plugins/${slug}`;
      if (!path.startsWith(prefix) || (path !== prefix && !path.startsWith(`${prefix}/`))) {
        console.warn(`[plugin:${slug}] ignored unsafe ${area} navigation path`, path);
        return;
      }
      if (seen.has(id)) {
        console.warn(`[plugin:${slug}] ignored duplicate ${area} navigation id`, id);
        return;
      }
      seen.add(id);
      items.push({ ...item, id, pluginSlug: slug, order: Number.isFinite(Number(item.order)) ? Number(item.order) : 1000 });
    });
  });
  return items.sort((a, b) => a.order - b.order || a.pluginSlug.localeCompare(b.pluginSlug) || a.id.localeCompare(b.id));
}
