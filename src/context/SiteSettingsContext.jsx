import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api } from "../lib/api.js";
import { DEFAULT_TRUSTED_POSTER_HOSTS, normalizeTrustedPosterHosts } from "../lib/posterSources.js";

export const DEFAULT_SITE_SETTINGS = {
  site_name: "AniMemo",
  homepage_title: "AniMemo · 我的动漫记忆库",
  site_avatar_url: "/assets/avatar.png",
  homepage_description: "把想看、在看与看完的作品收进同一条记忆轨迹，随时回望每一次与动画相遇的时刻。",
  universe_description: "穿过各位同好们的观看轨道，发现真实同步、持续生长的私人番剧宇宙。",
  social_handle: "X: @ANIMEMO",
  registration_enabled: true,
  trusted_poster_hosts: DEFAULT_TRUSTED_POSTER_HOSTS,
  turnstile: { enabled: false, site_key: "" },
};

const SiteSettingsContext = createContext({
  settings: DEFAULT_SITE_SETTINGS,
  loading: true,
  refresh: async () => {},
});

function normalizeSettings(value) {
  const next = { ...DEFAULT_SITE_SETTINGS, ...(value || {}) };
  next.site_avatar_url = value?.site_avatar_url || DEFAULT_SITE_SETTINGS.site_avatar_url;
  next.trusted_poster_hosts = normalizeTrustedPosterHosts(value?.trusted_poster_hosts);
  const turnstile = value?.turnstile && typeof value.turnstile === "object" ? value.turnstile : {};
  next.turnstile = {
    enabled: Boolean(turnstile.enabled),
    site_key: typeof turnstile.site_key === "string" ? turnstile.site_key.trim() : "",
  };
  return next;
}

export function SiteSettingsProvider({ children }) {
  const [settings, setSettings] = useState(DEFAULT_SITE_SETTINGS);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const { data } = await api.get("site-settings/");
      setSettings(normalizeSettings(data));
    } catch {
      setSettings((current) => normalizeSettings(current));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const handleUpdate = () => void refresh();
    window.addEventListener("animemo:site-settings-updated", handleUpdate);
    return () => window.removeEventListener("animemo:site-settings-updated", handleUpdate);
  }, [refresh]);

  useEffect(() => {
    document.title = settings.site_name || DEFAULT_SITE_SETTINGS.site_name;
  }, [settings.site_name]);

  const value = useMemo(() => ({ settings, loading, refresh }), [loading, refresh, settings]);
  return <SiteSettingsContext.Provider value={value}>{children}</SiteSettingsContext.Provider>;
}

export function useSiteSettings() {
  return useContext(SiteSettingsContext);
}
