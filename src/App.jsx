import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { ShowcasePage } from "./pages/ShowcasePage.jsx";
import { PageColorTransition } from "./components/PageColorTransition.jsx";
import { SiteSettingsProvider } from "./context/SiteSettingsContext.jsx";
import { api, getAuthUser, initializeAuth, subscribeAuth } from "./lib/api.js";
import { PluginErrorBoundary } from "./plugins/sdk/PluginErrorBoundary.jsx";
import { PluginRuntimeProvider, usePluginRuntime } from "./plugins/sdk/PluginRuntimeContext.jsx";
import { validatePluginRoute } from "./plugins/sdk/host.js";

const UserAuthPage = lazy(() => import("./pages/UserAuthPage.jsx").then(({ UserAuthPage: Component }) => ({ default: Component })));
const DashboardPage = lazy(() => import("./pages/DashboardPage.jsx").then(({ DashboardPage: Component }) => ({ default: Component })));
const AdminLoginPage = lazy(() => import("./pages/AdminLoginPage.jsx").then(({ AdminLoginPage: Component }) => ({ default: Component })));
const AdminDashboardPage = lazy(() => import("./pages/AdminDashboardPage.jsx").then(({ AdminDashboardPage: Component }) => ({ default: Component })));
const PluginPlatformPage = lazy(() => import("./pages/PluginPlatformPage.jsx").then(({ PluginPlatformPage: Component }) => ({ default: Component })));
const PluginDraftPreviewPage = lazy(() => import("./pages/PluginDraftPreviewPage.jsx").then(({ PluginDraftPreviewPage: Component }) => ({ default: Component })));
const FeaturedPage = lazy(() => import("./pages/FeaturedPage.jsx").then(({ FeaturedPage: Component }) => ({ default: Component })));
const ColumnSubmitPage = lazy(() => import("./pages/CommunityPages.jsx").then(({ ColumnSubmitPage: Component }) => ({ default: Component })));
const UniversePage = lazy(() => import("./pages/CommunityPages.jsx").then(({ UniversePage: Component }) => ({ default: Component })));

function RouteLoading() {
  return <main className="app-auth-bootstrap" aria-label="正在加载页面"><span>正在加载页面...</span></main>;
}

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
    <Suspense fallback={<RouteLoading />}>
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
    </Suspense>
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
