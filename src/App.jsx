import { useEffect, useMemo, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { AdminLoginPage } from "./pages/AdminLoginPage.jsx";
import { AdminDashboardPage } from "./pages/AdminDashboardPage.jsx";
import { UserAuthPage } from "./pages/UserAuthPage.jsx";
import { DashboardPage } from "./pages/DashboardPage.jsx";
import { ColumnSubmitPage, UniversePage } from "./pages/CommunityPages.jsx";
import { FeaturedPage } from "./pages/FeaturedPage.jsx";
import { ShowcasePage } from "./pages/ShowcasePage.jsx";
import { PluginPlatformPage } from "./pages/PluginPlatformPage.jsx";
import { PluginDraftPreviewPage } from "./pages/PluginDraftPreviewPage.jsx";
import { PageColorTransition } from "./components/PageColorTransition.jsx";
import { SiteSettingsProvider } from "./context/SiteSettingsContext.jsx";
import { api, getAuthUser, initializeAuth, subscribeAuth } from "./lib/api.js";
import { PluginErrorBoundary } from "./plugins/sdk/PluginErrorBoundary.jsx";
import { PluginRuntimeProvider, usePluginRuntime } from "./plugins/sdk/PluginRuntimeContext.jsx";
import { validatePluginRoute } from "./plugins/sdk/host.js";

// SDK v2 loads only server-declared frontend entries and explicit route access
// policies. Plugin code is never eagerly executed before the host authorizes it.

function AppRoutes({ authUser }) {
  const navigate = useNavigate();
  const location = useLocation();
  const { loading: pluginsLoading, plugins, reloadPlugin } = usePluginRuntime();
  const pluginRoutes = useMemo(() => plugins.flatMap(({ slug, manifest, plugin, status }) => {
    if (status !== "loaded" || !plugin) return [];
    const routes = Array.isArray(plugin.routes) ? plugin.routes : [];
    return routes.flatMap((route) => {
      const validation = validatePluginRoute(route, slug, manifest);
      if (!validation.valid) {
        console.warn(`[plugin:${slug}] ignored route`, validation.reason);
        return [];
      }
      return [{ ...route, pluginSlug: slug, pluginArea: validation.area }];
    });
  }), [plugins]);

  return (
    <Routes>
      <Route path="/" element={<ShowcasePage />} />
      <Route path="/login" element={<UserAuthPage />} />
      <Route path="/register" element={<UserAuthPage />} />
      <Route path="/register/verify" element={<UserAuthPage />} />
      <Route path="/register/complete" element={<UserAuthPage />} />
      <Route path="/dashboard" element={<DashboardPage />} />
      <Route path="/plugins" element={<PluginPlatformPage authUser={authUser} />} />
      <Route path="/plugins/preview/:previewSession" element={<PluginDraftPreviewPage authUser={authUser} />} />
      <Route path="/featured" element={<FeaturedPage />} />
      <Route path="/featured/submit" element={<ColumnSubmitPage />} />
      <Route path="/featured/:columnId" element={<Navigate to="/featured" replace />} />
      <Route path="/universe" element={<UniversePage />} />
      <Route path="/shared/:publicSlug" element={<ShowcasePage sharedMode />} />
      <Route path="/admin-login" element={<AdminLoginPage />} />
      <Route path="/admin-control" element={<AdminDashboardPage />} />
      {pluginRoutes.map(({ path, Component, pluginSlug, access = "public", permission }) => {
        const hasPermission = !permission
          || Boolean(authUser?.pluginPermissions?.includes?.(permission));
        const allowed = hasPermission && (access === "public"
          || (access === "auth" && Boolean(authUser))
          || (access === "staff" && Boolean(authUser?.is_staff)));
        return (
        <Route
          key={`${pluginSlug}:${path}`}
          path={path}
          element={(
            <PluginErrorBoundary pluginSlug={pluginSlug} onReload={() => reloadPlugin(pluginSlug)} onHome={() => navigate("/", { replace: true })}>
              {!allowed ? <Navigate to={access === "staff" ? "/admin-login" : access === "auth" ? "/login" : "/"} replace state={{ from: path }} /> : null}
              {allowed && <Component />}
            </PluginErrorBoundary>
          )}
        />
        );
      })}
      <Route
        path="*"
        element={pluginsLoading && location.pathname.startsWith("/plugins/")
          ? <main className="app-auth-bootstrap" aria-label="正在加载插件" />
          : <Navigate to="/" replace />}
      />
    </Routes>
  );
}

export function App() {
  const [authReady, setAuthReady] = useState(false);
  const [authUser, setAuthUser] = useState(() => getAuthUser());

  useEffect(() => subscribeAuth(({ user }) => setAuthUser(user)), []);

  useEffect(() => {
    let active = true;
    initializeAuth().then(() => {
      if (!active) return;
      setAuthReady(true);
    });
    return () => { active = false; };
  }, []);

  if (!authReady) {
    return <main className="app-auth-bootstrap" aria-label="正在恢复登录状态" />;
  }

  return (
      <SiteSettingsProvider>
      <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <PageColorTransition>
          <PluginRuntimeProvider authUser={authUser}>
            <AppRoutes authUser={authUser} />
          </PluginRuntimeProvider>
        </PageColorTransition>
      </BrowserRouter>
    </SiteSettingsProvider>
  );
}
