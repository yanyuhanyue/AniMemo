import { Suspense, useEffect, useState } from "react";
import { Navigate, useNavigate, useParams } from "react-router-dom";

import { Icon } from "../components/Icon.jsx";
import { api, readableApiError } from "../lib/api.js";
import { PluginErrorBoundary } from "../plugins/sdk/PluginErrorBoundary.jsx";
import { createEventBus, createPluginHost, validatePluginRoute } from "../plugins/sdk/host.js";

export function PluginDraftPreviewPage({ authUser }) {
  const navigate = useNavigate();
  const { previewSession = "" } = useParams();
  const [preview, setPreview] = useState(null);
  const [PreviewComponent, setPreviewComponent] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    let plugin = null;
    let styleNode = null;
    const eventBus = createEventBus();

    const load = async () => {
      setError("");
      try {
        const { data } = await api.get(`plugins/previews/${encodeURIComponent(previewSession)}/`);
        if (!active) return;
        setPreview(data);
        if (data.styleEntry) {
          styleNode = document.createElement("link");
          styleNode.rel = "stylesheet";
          styleNode.href = new URL(data.styleEntry, window.location.origin).href;
          styleNode.dataset.pluginPreview = data.slug;
          document.head.appendChild(styleNode);
        }
        const module = await import(/* @vite-ignore */ new URL(data.frontendEntry, window.location.origin).href);
        if (typeof module.default !== "function") throw new Error("插件入口没有导出 createPlugin(host)。");
        const host = createPluginHost({
          slug: data.slug,
          client: api,
          authStore: { getUser: () => authUser, subscribe: () => () => {} },
          navigation: { navigate, replace: (path, options) => navigate(path, { ...options, replace: true }), back: () => navigate(-1) },
          ui: {
            notify: (payload) => window.alert(typeof payload === "string" ? payload : payload?.message || "插件提示"),
            confirm: (payload) => Promise.resolve(window.confirm(payload?.message || "确认继续？")),
          },
          site: { getSettings: () => ({}), getName: () => "AniMemo", getBaseUrl: () => window.location.origin, subscribeSettings: () => () => {} },
          eventBus,
          manifest: data.manifest,
        });
        plugin = module.default(host);
        const route = (Array.isArray(plugin?.routes) ? plugin.routes : []).find((item) => validatePluginRoute(item, data.slug, data.manifest).valid);
        if (!route) throw new Error("插件没有可预览的 frontend.page 路由。");
        if (active) setPreviewComponent(() => route.Component);
      } catch (requestError) {
        if (active) setError(readableApiError(requestError, "私人预览已过期或无法加载。"));
      }
    };
    void load();
    return () => {
      active = false;
      plugin?.dispose?.();
      styleNode?.remove?.();
      eventBus.clear();
    };
  }, [authUser, navigate, previewSession]);

  if (!authUser) return <Navigate to="/login" replace />;

  return (
    <main className="plugin-draft-preview">
      <header className="plugin-draft-preview__bar">
        <button type="button" onClick={() => navigate("/plugins")} title="返回我的插件"><Icon name="arrow-left" /></button>
        <div><span>PRIVATE DRAFT PREVIEW</span><strong>{preview ? `${preview.slug} v${preview.version}` : "正在验证预览会话"}</strong></div>
        <b>仅作者可见</b>
      </header>
      {error && <section className="plugin-platform-error" role="alert"><Icon name="warning" />{error}</section>}
      {!error && !PreviewComponent && <section className="plugin-draft-preview__loading">正在加载前端草稿...</section>}
      {PreviewComponent && (
        <PluginErrorBoundary pluginSlug={preview?.slug} onReload={() => window.location.reload()} onHome={() => navigate("/plugins") }>
          <Suspense fallback={<section className="plugin-draft-preview__loading">正在加载预览页面...</section>}>
            <PreviewComponent />
          </Suspense>
        </PluginErrorBoundary>
      )}
    </main>
  );
}
