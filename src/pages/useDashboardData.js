import { useEffect, useMemo, useState } from "react";

import { api, getStoredTokens, readableApiError } from "../lib/api.js";
import { useServerStateRevision } from "../lib/serverState.js";
import { buildPresetColorMap, FALLBACK_TAG_PRESETS, normalizeTagPresets, resolveTagColors } from "../lib/tagPresets.js";
import { demoAnimeRecords, demoEnabled } from "@demo-data";

import {
  DEFAULT_QUICK_FILTERS,
  DEFAULT_SETTINGS,
  FILTERS_KEY,
  SETTINGS_KEY,
  STORAGE_KEY,
  apiToRecord,
} from "./dashboardData.js";

export function useDashboardData({ navigate }) {
  const [{ access }, setAuthSnapshot] = useState(() => getStoredTokens());
  const isDemo = demoEnabled
    && (!access || localStorage.getItem("anime_journal_demo") === "true");
  const [records, setRecords] = useState(() => {
    if (!isDemo) return [];
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      return saved ? JSON.parse(saved) : [];
    } catch {
      return [];
    }
  });
  const [demoCatalogRecords, setDemoCatalogRecords] = useState([]);
  const [settings, setSettings] = useState(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(SETTINGS_KEY));
      return saved ? { ...DEFAULT_SETTINGS, ...saved, publicStatus: saved.publicStatus || "private" } : DEFAULT_SETTINGS;
    } catch {
      return DEFAULT_SETTINGS;
    }
  });
  const [quickFilters, setQuickFilters] = useState(() => {
    try { return JSON.parse(localStorage.getItem(FILTERS_KEY)) || DEFAULT_QUICK_FILTERS; }
    catch { return DEFAULT_QUICK_FILTERS; }
  });
  const [tagPresets, setTagPresets] = useState(FALLBACK_TAG_PRESETS);
  const [dashboardReady, setDashboardReady] = useState(isDemo);
  const [loadError, setLoadError] = useState("");
  const serverStateRevision = useServerStateRevision(["journal_entries", "settings", "filters", "showcase"]);
  const presetColors = useMemo(() => buildPresetColorMap(tagPresets), [tagPresets]);

  useEffect(() => {
    if (!demoEnabled && !access) navigate("/login", { replace: true });
  }, [access, navigate]);

  useEffect(() => {
    if (!isDemo) {
      setDemoCatalogRecords([]);
      return undefined;
    }
    setDemoCatalogRecords(demoAnimeRecords);
    if (!localStorage.getItem(STORAGE_KEY)) {
      setRecords(demoAnimeRecords.map((record) => ({
        ...record,
        tagColors: resolveTagColors(record.tags, record.tagColors, presetColors),
        shared: record.score >= 9.5,
      })));
    }
    return undefined;
  }, [isDemo, presetColors]);

  useEffect(() => {
    let cancelled = false;
    if (isDemo) {
      setDashboardReady(true);
      return undefined;
    }
    if (!access) return undefined;
    Promise.allSettled([
      api.get("entries/?page_size=100"),
      api.get("settings/me/"),
      api.get("filters/"),
      api.get("tag-presets/"),
    ]).then(([entriesResult, settingsResult, filtersResult, tagPresetsResult]) => {
      if (cancelled) return;
      const loadedTagPresets = tagPresetsResult.status === "fulfilled"
        ? normalizeTagPresets(tagPresetsResult.value.data, [])
        : FALLBACK_TAG_PRESETS;
      const loadedPresetColors = buildPresetColorMap(loadedTagPresets);
      setTagPresets(loadedTagPresets);
      if (entriesResult.status === "fulfilled") {
        const items = entriesResult.value.data?.results || entriesResult.value.data || [];
        setRecords(Array.isArray(items) ? items.map((item) => apiToRecord(item, loadedPresetColors)) : []);
      } else {
        setRecords([]);
        setLoadError(readableApiError(entriesResult.reason, "番剧数据加载失败，请检查服务器连接。"));
      }
      if (settingsResult.status === "fulfilled") {
        const data = settingsResult.value.data;
        setSettings((current) => ({
          ...current,
          email: data.email || current.email,
          nickname: data.nickname || data.username || current.nickname,
          subtitle: data.showcase_subtitle || current.subtitle,
          avatar: Object.hasOwn(data, "avatar_url") ? data.avatar_url : current.avatar,
          accent: data.accent || current.accent,
          publicProfile: data.is_public ?? data.allow_sharing ?? current.publicProfile,
          publicSlug: data.public_slug || current.publicSlug,
          publicStatus: data.public_status || current.publicStatus,
          isStaff: data.is_staff ?? current.isStaff,
          isSuperuser: data.is_superuser ?? current.isSuperuser,
          twoFactorEnabled: data.two_factor_enabled ?? current.twoFactorEnabled,
        }));
      }
      if (filtersResult.status === "fulfilled") {
        const items = filtersResult.value.data?.results || filtersResult.value.data || [];
        if (Array.isArray(items) && items.length) setQuickFilters([{ id: "all", name: "全部", tags: [] }, ...items]);
      }
      setDashboardReady(true);
    });
    return () => { cancelled = true; };
  }, [access, isDemo, serverStateRevision]);

  useEffect(() => {
    if (!isDemo) return;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(records));
    window.dispatchEvent(new CustomEvent("anime-journal:records-updated", { detail: records }));
  }, [isDemo, records]);

  useEffect(() => { if (isDemo) localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings)); }, [isDemo, settings]);
  useEffect(() => { if (isDemo) localStorage.setItem(FILTERS_KEY, JSON.stringify(quickFilters)); }, [isDemo, quickFilters]);

  return {
    dashboardReady,
    demoCatalogRecords,
    isDemo,
    loadError,
    presetColors,
    quickFilters,
    records,
    setAuthSnapshot,
    setQuickFilters,
    setRecords,
    setSettings,
    settings,
    tagPresets,
  };
}
