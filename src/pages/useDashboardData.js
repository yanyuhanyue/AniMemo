import { useCallback, useEffect, useMemo, useRef, useState } from "react";

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
import {
  appendUniqueDashboardRecords,
  buildDashboardQueryParams,
  buildDashboardQueryKey,
  getDashboardNextPage,
} from "./dashboardQuery.js";

export function useDashboardData({ navigate, entryQuery = {} }) {
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
  const [isInitialLoading, setIsInitialLoading] = useState(!isDemo);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const [nextPage, setNextPage] = useState(null);
  const [totalCount, setTotalCount] = useState(isDemo ? records.length : 0);
  const [facets, setFacets] = useState({ tags: [], years: [] });
  const [debouncedSearch, setDebouncedSearch] = useState(String(entryQuery.search || ""));
  const [entriesReloadKey, setEntriesReloadKey] = useState(0);
  const requestGenerationRef = useRef(0);
  const requestControllerRef = useRef(null);
  const loadingMoreRef = useRef(false);
  const [analytics, setAnalytics] = useState(null);
  const [analyticsError, setAnalyticsError] = useState("");
  const entriesRevision = useServerStateRevision(["journal_entries"]);
  const metadataRevision = useServerStateRevision(["settings", "filters", "showcase", "analytics"]);
  const presetColors = useMemo(() => buildPresetColorMap(tagPresets), [tagPresets]);
  const requestQuery = useMemo(() => ({
    search: debouncedSearch,
    tag: entryQuery.tag,
    status: entryQuery.status,
    visibility: entryQuery.visibility,
    year: entryQuery.year,
    activity: entryQuery.activity,
    sort: entryQuery.sort,
    priority: entryQuery.priority,
    quickFilterId: entryQuery.quickFilterId,
    quickFilter: entryQuery.quickFilter || quickFilters.find((item) => String(item.id) === String(entryQuery.quickFilterId)),
  }), [
    debouncedSearch,
    entryQuery.activity,
    entryQuery.priority,
    entryQuery.quickFilter,
    entryQuery.quickFilterId,
    entryQuery.sort,
    entryQuery.status,
    entryQuery.tag,
    entryQuery.visibility,
    entryQuery.year,
    quickFilters,
  ]);
  const requestQueryRef = useRef(requestQuery);
  requestQueryRef.current = requestQuery;
  const presetColorsRef = useRef(presetColors);
  presetColorsRef.current = presetColors;
  const requestQueryKey = useMemo(() => buildDashboardQueryKey(requestQuery), [requestQuery]);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(String(entryQuery.search || "")), 300);
    return () => window.clearTimeout(timer);
  }, [entryQuery.search]);

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
      api.get("settings/me/"),
      api.get("filters/"),
      api.get("tag-presets/"),
      api.get("stats/me/"),
    ]).then(([settingsResult, filtersResult, tagPresetsResult, analyticsResult]) => {
      if (cancelled) return;
      const loadedTagPresets = tagPresetsResult.status === "fulfilled"
        ? normalizeTagPresets(tagPresetsResult.value.data, [])
        : FALLBACK_TAG_PRESETS;
      setTagPresets(loadedTagPresets);
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
      if (analyticsResult.status === "fulfilled") {
        setAnalytics(analyticsResult.value.data || null);
        setAnalyticsError("");
      } else {
        setAnalytics(null);
        setAnalyticsError(readableApiError(analyticsResult.reason, "手账统计读取失败，请稍后重试。"));
      }
      setDashboardReady(true);
    });
    return () => { cancelled = true; };
  }, [access, isDemo, metadataRevision]);

  const fetchEntriesPage = useCallback(async ({ page, append, generation }) => {
    const controller = new AbortController();
    requestControllerRef.current?.abort();
    requestControllerRef.current = controller;
    const params = buildDashboardQueryParams(requestQueryRef.current, { page, includeFacets: page === 1 });
    try {
      const response = await api.get("entries/", { params, signal: controller.signal });
      if (generation !== requestGenerationRef.current) return;
      const payload = response.data || {};
      const items = Array.isArray(payload) ? payload : (payload.results || []);
      const mapped = items.map((item) => apiToRecord(item, presetColorsRef.current));
      setRecords((current) => append ? appendUniqueDashboardRecords(current, mapped) : mapped);
      setTotalCount(Number.isFinite(Number(payload.count)) ? Number(payload.count) : mapped.length);
      setNextPage(getDashboardNextPage(payload));
      setHasMore(Boolean(payload.next));
      if (page === 1 && payload.facets) {
        setFacets({ tags: payload.facets.tags || [], years: payload.facets.years || [] });
      }
      setLoadError("");
      setLoadMoreError("");
    } catch (error) {
      if (controller.signal.aborted || generation !== requestGenerationRef.current) return;
      if (append) setLoadMoreError(readableApiError(error, "加载更多记录失败，请重试。"));
      else {
        setRecords([]);
        setTotalCount(0);
        setNextPage(null);
        setHasMore(false);
        setLoadError(readableApiError(error, "番剧数据加载失败，请检查服务器连接。"));
      }
    } finally {
      if (generation === requestGenerationRef.current) {
        if (append) {
          loadingMoreRef.current = false;
          setIsLoadingMore(false);
        }
        else setIsInitialLoading(false);
      }
    }
  }, [requestQueryKey]);

  useEffect(() => {
    if (isDemo) {
      setIsInitialLoading(false);
      setHasMore(false);
      setNextPage(null);
      setTotalCount(records.length);
      return undefined;
    }
    if (!access) return undefined;
    const generation = requestGenerationRef.current + 1;
    requestGenerationRef.current = generation;
    requestControllerRef.current?.abort();
    setIsInitialLoading(true);
    loadingMoreRef.current = false;
    setIsLoadingMore(false);
    setLoadMoreError("");
    setLoadError("");
    setRecords([]);
    setNextPage(null);
    setHasMore(false);
    fetchEntriesPage({ page: 1, append: false, generation });
    return () => {
      requestControllerRef.current?.abort();
      requestGenerationRef.current += 1;
    };
  }, [access, entriesRevision, fetchEntriesPage, isDemo, requestQueryKey, entriesReloadKey]);

  const loadMore = useCallback(() => {
    if (isDemo || !hasMore || !nextPage || isInitialLoading || isLoadingMore || loadingMoreRef.current) return;
    const generation = requestGenerationRef.current;
    loadingMoreRef.current = true;
    setIsLoadingMore(true);
    setLoadMoreError("");
    fetchEntriesPage({ page: nextPage, append: true, generation });
  }, [fetchEntriesPage, hasMore, isDemo, isInitialLoading, isLoadingMore, nextPage]);

  const refreshEntries = useCallback(() => {
    setEntriesReloadKey((current) => current + 1);
  }, []);

  useEffect(() => {
    if (!isDemo) return;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(records));
    window.dispatchEvent(new CustomEvent("anime-journal:records-updated", { detail: records }));
  }, [isDemo, records]);

  useEffect(() => { if (isDemo) localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings)); }, [isDemo, settings]);
  useEffect(() => { if (isDemo) localStorage.setItem(FILTERS_KEY, JSON.stringify(quickFilters)); }, [isDemo, quickFilters]);

  return {
    analytics,
    analyticsError,
    dashboardReady,
    demoCatalogRecords,
    isDemo,
    isInitialLoading,
    isLoadingMore,
    loadMore,
    loadMoreError,
    loadError,
    hasMore,
    loadedCount: records.length,
    nextPage,
    presetColors,
    quickFilters,
    records,
    setAuthSnapshot,
    setQuickFilters,
    setRecords,
    setSettings,
    settings,
    tagPresets,
    totalCount,
    facets,
    refreshEntries,
  };
}
