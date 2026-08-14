import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, getAuthUser, subscribeAuth } from "../../lib/api.js";
import { useSiteSettings } from "../../context/SiteSettingsContext.jsx";
import { collectPluginNavigation, createEventBus, createPluginHost, PLUGIN_SDK_VERSION } from "./host.js";

async function loadPluginModule(metadata) {
  const entry = metadata?.frontendEntry;
  if (!entry) throw new Error("插件未提供 frontend/plugin.js 入口。");
  if (!globalThis.__ANIMEMO_REACT_RUNTIME__?.React) {
    throw new Error("宿主 Shared React Runtime 尚未就绪。");
  }
  const url = new URL(entry, window.location.origin);
  const module = await import(/* @vite-ignore */ url.href);
  return { module };
}

async function loadPluginStyle(metadata) {
  if (!metadata?.styleEntry || typeof document === "undefined") return null;
  const styleNode = document.createElement("link");
  styleNode.rel = "stylesheet";
  styleNode.href = new URL(metadata.styleEntry, window.location.origin).href;
  styleNode.dataset.pluginSlug = metadata.slug;
  document.head.appendChild(styleNode);
  return { styleNode };
}

const PluginRuntimeContext = createContext({
  sdkVersion: PLUGIN_SDK_VERSION,
  plugins: [],
  navigation: [],
  loading: true,
  reloadPlugin: async () => {},
});

export function usePluginRuntime() {
  return useContext(PluginRuntimeContext);
}

function PluginRuntimeUi({ notice, confirmRequest, onConfirm }) {
  return (
    <>
      {notice && <div className={`plugin-runtime-notice is-${notice.type || "info"}`} role="status"><strong>{notice.title || "插件提示"}</strong><span>{notice.message || ""}</span></div>}
      {confirmRequest && (
        <div className="plugin-runtime-confirm-backdrop" role="presentation">
          <section className="plugin-runtime-confirm" role="dialog" aria-modal="true" aria-labelledby="plugin-confirm-title">
            <span className="plugin-runtime-confirm__kicker">PLUGIN CONFIRM</span>
            <h2 id="plugin-confirm-title">{confirmRequest.title || "确认操作"}</h2>
            <p>{confirmRequest.message || "请确认是否继续。"}</p>
            <div className="plugin-runtime-confirm__actions">
              <button type="button" onClick={() => onConfirm(false)}>{confirmRequest.cancelText || "取消"}</button>
              <button type="button" className={confirmRequest.danger ? "is-danger" : ""} onClick={() => onConfirm(true)}>{confirmRequest.confirmText || "确认"}</button>
            </div>
          </section>
        </div>
      )}
    </>
  );
}

export function PluginRuntimeProvider({ children, authUser }) {
  const navigate = useNavigate();
  const { settings } = useSiteSettings();
  const authRef = useRef(authUser || getAuthUser());
  const settingsRef = useRef(settings);
  const eventBusRef = useRef(null);
  const pluginsRef = useRef([]);
  const confirmResolverRef = useRef(null);
  const [plugins, setPlugins] = useState([]);
  const [loading, setLoading] = useState(true);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [notice, setNotice] = useState(null);
  const [confirmRequest, setConfirmRequest] = useState(null);

  authRef.current = authUser || null;
  settingsRef.current = settings;

  if (!eventBusRef.current) eventBusRef.current = createEventBus();

  const authStore = useMemo(() => ({
    getUser: () => authRef.current,
    subscribe: (listener) => subscribeAuth(listener),
  }), []);

  const notify = useCallback((payload = {}) => {
    const next = typeof payload === "string" ? { message: payload } : payload;
    setNotice({ type: next.type || "info", title: next.title || "插件提示", message: next.message || "" });
    window.clearTimeout(notify.timeout);
    notify.timeout = window.setTimeout(() => setNotice(null), Number(next.duration || 4200));
    return next;
  }, []);

  const confirm = useCallback((payload = {}) => new Promise((resolve) => {
    confirmResolverRef.current = resolve;
    setConfirmRequest(payload);
  }), []);

  const resolveConfirm = useCallback((value) => {
    const resolver = confirmResolverRef.current;
    confirmResolverRef.current = null;
    setConfirmRequest(null);
    resolver?.(Boolean(value));
  }, []);

  useEffect(() => () => {
    window.clearTimeout(notify.timeout);
    confirmResolverRef.current?.(false);
    confirmResolverRef.current = null;
  }, [notify]);

  useEffect(() => {
    const reload = () => setReloadNonce((value) => value + 1);
    window.addEventListener("animemo:plugins-changed", reload);
    return () => window.removeEventListener("animemo:plugins-changed", reload);
  }, []);

  const reloadPlugin = useCallback(async (slug) => {
    const existing = pluginsRef.current.find((item) => item.slug === slug);
    existing?.plugin?.dispose?.();
    existing?.styleNode?.remove?.();
    setPlugins((items) => items.filter((item) => item.slug !== slug));
    setReloadNonce((value) => value + 1);
  }, []);

  useEffect(() => {
    let active = true;
    const load = async () => {
      setLoading(true);
      try {
        const { data } = await api.get("plugins/enabled/");
        const enabled = (Array.isArray(data?.plugins) ? data.plugins : []).map((item) => ({
          ...(data?.manifests?.[item.slug] || {}),
          ...item,
        }));
        const loaded = await Promise.all(enabled.map(async (metadata) => {
          const slug = metadata.slug;
          try {
            const style = await loadPluginStyle(metadata);
            const loadedModule = await loadPluginModule(metadata);
            const module = loadedModule.module;
            if (typeof module.default !== "function") throw new Error("插件入口没有导出 createPlugin(host)。");
            const host = createPluginHost({
              slug,
              client: api,
              authStore,
              navigation: { navigate, replace: (path, options) => navigate(path, { ...options, replace: true }), back: () => navigate(-1) },
              ui: { notify, confirm },
              site: { getSettings: () => settingsRef.current, getName: () => settingsRef.current?.site_name, getBaseUrl: () => window.location.origin, subscribeSettings: (listener) => { const handler = () => listener(settingsRef.current); window.addEventListener("animemo:site-settings-updated", handler); return () => window.removeEventListener("animemo:site-settings-updated", handler); } },
              eventBus: eventBusRef.current,
              manifest: metadata,
            });
            const plugin = module.default(host);
            return { ...metadata, slug, manifest: metadata, host, plugin, styleNode: style?.styleNode || null, status: "loaded" };
          } catch (error) {
            console.error(`[plugin:${slug}] frontend entry failed to load`, error);
            if (typeof document !== "undefined") document.querySelector(`[data-plugin-slug="${slug}"]`)?.remove?.();
            return { ...metadata, slug, manifest: metadata, plugin: null, status: "failed", error: String(error?.message || error) };
          }
        }));
        if (!active) {
          loaded.forEach((item) => { item.plugin?.dispose?.(); item.styleNode?.remove?.(); });
          return;
        }
        pluginsRef.current = loaded;
        setPlugins(loaded);
      } catch (error) {
        console.warn("Enabled plugin metadata could not be loaded.", error?.message || error);
        if (active) setPlugins([]);
      } finally {
        if (active) setLoading(false);
      }
    };
    void load();
    return () => {
      active = false;
      pluginsRef.current.forEach((item) => { item.plugin?.dispose?.(); item.styleNode?.remove?.(); });
      pluginsRef.current = [];
      eventBusRef.current?.clear();
    };
  }, [authStore, confirm, navigate, notify, reloadNonce]);

  const value = useMemo(() => ({
    sdkVersion: PLUGIN_SDK_VERSION,
    plugins,
    navigation: collectPluginNavigation(plugins),
    loading,
    reloadPlugin,
  }), [loading, plugins, reloadPlugin]);

  return (
    <PluginRuntimeContext.Provider value={value}>
      {children}
      <PluginRuntimeUi notice={notice} confirmRequest={confirmRequest} onConfirm={resolveConfirm} />
    </PluginRuntimeContext.Provider>
  );
}
