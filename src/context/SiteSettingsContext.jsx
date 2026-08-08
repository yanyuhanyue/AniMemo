import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api } from "../lib/api.js";
import { DEFAULT_TRUSTED_POSTER_HOSTS, normalizeTrustedPosterHosts } from "../lib/posterSources.js";

export const DEFAULT_SITE_SETTINGS = {
  site_name: "Anime Journal",
  homepage_title: "XuanHuang 的番剧汇总",
  site_avatar_url: "/assets/avatar.png",
  homepage_description: "精心收录 2007 年至今的优质动漫作品，包含详细的题材分类、季度划分与主观评价等。",
  universe_description: "穿过各位同好们的观看轨道，发现真实同步、持续生长的私人番剧宇宙。",
  social_handle: "X: @ANIME_JOURNAL",
  registration_enabled: true,
  trusted_poster_hosts: DEFAULT_TRUSTED_POSTER_HOSTS,
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
    window.addEventListener("anime-journal:site-settings-updated", handleUpdate);
    return () => window.removeEventListener("anime-journal:site-settings-updated", handleUpdate);
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
