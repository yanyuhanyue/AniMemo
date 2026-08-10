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
  DASHBOARD_PAGE_SIZE,
  appendUniqueDashboardRecords,
  buildDashboardQueryParams,
  buildDashboardQueryKey,
  getDashboardNextPage,
} from "./dashboardQuery.js";
import { reconcileDashboardMutations, sortDashboardRecords } from "./dashboardMutation.js";

function settingsFromApi(current, data = {}) {
  return {
    ...current,
    email: data.email || current.email,
    nickname: Object.hasOwn(data, "nickname") ? data.nickname : (data.username || current.nickname),
    subtitle: Object.hasOwn(data, "showcase_subtitle") ? data.showcase_subtitle : current.subtitle,
    avatar: Object.hasOwn(data, "avatar_url") ? data.avatar_url : current.avatar,
    accent: data.accent || current.accent,
    publicProfile: data.is_public ?? data.allow_sharing ?? current.publicProfile,
    publicSlug: data.public_slug || current.publicSlug,
    publicStatus: data.public_status || current.publicStatus,
    isStaff: data.is_staff ?? current.isStaff,
    isSuperuser: data.is_superuser ?? current.isSuperuser,
    twoFactorEnabled: data.two_factor_enabled ?? current.twoFactorEnabled,
  };
}

function filtersFromApi(data) {
  const items = data?.results || data || [];
  return Array.isArray(items) ? [{ id: "all", name: "全部", tags: [] }, ...items.filter((item) => String(item.id) !== "all")] : null;
}

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
  const entryViewRevisionRef = useRef(0);
  const requestControllerRef = useRef(null);
  const loadingMoreRef = useRef(false);
  const mountedRef = useRef(false);
  const metadataHydratedRef = useRef(false);
  const [analytics, setAnalytics] = useState(null);
  const [analyticsError, setAnalyticsError] = useState("");
  const entriesRevision = useServerStateRevision(["journal_entries"]);
  const settingsRevision = useServerStateRevision(["settings"]);
  const filtersRevision = useServerStateRevision(["filters"]);
  const analyticsRevision = useServerStateRevision(["analytics"]);
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
  const requestQueryKeyRef = useRef(requestQueryKey);
  requestQueryKeyRef.current = requestQueryKey;
  const recordsRef = useRef(records);
  recordsRef.current = records;
  const hasMoreRef = useRef(hasMore);
  hasMoreRef.current = hasMore;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      requestControllerRef.current?.abort();
    };
  }, []);

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
    if (isDemo) return;
    setRecords((current) => {
      let changed = false;
      const decorated = current.map((record) => {
        const tagColors = resolveTagColors(record.tags, record.savedTagColors ?? record.tagColors, presetColors);
        const keys = Object.keys(tagColors);
        const matches = keys.length === Object.keys(record.tagColors || {}).length
          && keys.every((tag) => tagColors[tag] === record.tagColors?.[tag]);
        if (matches) return record;
        changed = true;
        return { ...record, tagColors };
      });
      return changed ? decorated : current;
    });
  }, [isDemo, presetColors]);

  useEffect(() => {
    let cancelled = false;
    metadataHydratedRef.current = false;
    if (isDemo) {
      metadataHydratedRef.current = true;
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
        setSettings((current) => settingsFromApi(current, settingsResult.value.data));
      }
      if (filtersResult.status === "fulfilled") {
        const filters = filtersFromApi(filtersResult.value.data);
        if (filters) setQuickFilters(filters);
      }
      if (analyticsResult.status === "fulfilled") {
        setAnalytics(analyticsResult.value.data || null);
        setAnalyticsError("");
      } else {
        setAnalytics(null);
        setAnalyticsError(readableApiError(analyticsResult.reason, "手账统计读取失败，请稍后重试。"));
      }
      metadataHydratedRef.current = true;
      setDashboardReady(true);
    });
    return () => { cancelled = true; };
  }, [access, isDemo]);

  useEffect(() => {
    if (isDemo || !access || !metadataHydratedRef.current) return undefined;
    let cancelled = false;
    api.get("settings/me/").then((response) => {
      if (!cancelled) setSettings((current) => settingsFromApi(current, response.data));
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [access, isDemo, settingsRevision]);

  useEffect(() => {
    if (isDemo || !access || !metadataHydratedRef.current) return undefined;
    let cancelled = false;
    api.get("filters/").then((response) => {
      const filters = filtersFromApi(response.data);
      if (!cancelled && filters) setQuickFilters(filters);
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [access, filtersRevision, isDemo]);

  useEffect(() => {
    if (isDemo || !access || !metadataHydratedRef.current) return undefined;
    let cancelled = false;
    api.get("stats/me/").then((response) => {
      if (cancelled) return;
      setAnalytics(response.data || null);
      setAnalyticsError("");
    }).catch((error) => {
      if (cancelled) return;
      setAnalytics(null);
      setAnalyticsError(readableApiError(error, "手账统计读取失败，请稍后重试。"));
    });
    return () => { cancelled = true; };
  }, [access, analyticsRevision, isDemo]);

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
      setRecords((current) => append
        ? sortDashboardRecords(appendUniqueDashboardRecords(current, mapped), requestQueryRef.current)
        : mapped);
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
    entryViewRevisionRef.current += 1;
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
    entryViewRevisionRef.current += 1;
    setEntriesReloadKey((current) => current + 1);
  }, []);

  const getEntryMutationSnapshot = useCallback(() => ({
    queryKey: requestQueryKeyRef.current,
    revision: entryViewRevisionRef.current,
    loadedCount: recordsRef.current.length,
    hadMore: hasMoreRef.current,
  }), []);

  const commitEntryMutations = useCallback((mutations, snapshot) => {
    if (!mountedRef.current) return { status: "unmounted" };
    if (!snapshot || snapshot.queryKey !== requestQueryKeyRef.current || snapshot.revision !== entryViewRevisionRef.current) {
      refreshEntries();
      return { status: "refreshed" };
    }

    requestControllerRef.current?.abort();
    requestGenerationRef.current += 1;
    entryViewRevisionRef.current += 1;
    loadingMoreRef.current = false;
    setIsLoadingMore(false);
    setLoadMoreError("");

    const reconciliation = reconcileDashboardMutations(recordsRef.current, mutations, requestQueryRef.current);
    recordsRef.current = reconciliation.records;
    setRecords(reconciliation.records);
    setTotalCount((current) => Math.max(0, current + reconciliation.countDelta));
    setFacets((current) => {
      const tags = new Set(current.tags);
      const years = new Set(current.years);
      mutations.forEach(({ nextRecord }) => {
        (nextRecord?.tags || []).forEach((tag) => tags.add(tag));
        const year = String(nextRecord?.period || "").slice(0, 4);
        if (/^\d{4}$/.test(year)) years.add(year);
      });
      return {
        tags: [...tags].sort((a, b) => a.localeCompare(b, "zh-CN")),
        years: [...years].sort((a, b) => Number(b) - Number(a)),
      };
    });

    if (reconciliation.removedVisibleRecord && snapshot.hadMore && snapshot.loadedCount > 0) {
      const page = Math.max(1, Math.ceil(snapshot.loadedCount / DASHBOARD_PAGE_SIZE));
      const generation = requestGenerationRef.current;
      loadingMoreRef.current = true;
      setIsLoadingMore(true);
      fetchEntriesPage({ page, append: true, generation });
      return { status: "reconciled", backfillPage: page };
    }
    return { status: "reconciled", backfillPage: null };
  }, [fetchEntriesPage, refreshEntries]);

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
    commitEntryMutations,
    getEntryMutationSnapshot,
    refreshEntries,
  };
}
