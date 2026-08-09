import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { AnimeModal } from "../components/AnimeModal.jsx";
import { AddAnimeModal } from "../components/dashboard/AddAnimeModal.jsx";
import { ImportJournalModal } from "../components/dashboard/ImportJournalModal.jsx";
import { BangumiImportDialog } from "../components/dashboard/BangumiImportDialog.jsx";
import { DashboardReturnHomeControl, DashboardShareControl } from "../components/dashboard/DashboardJournalControls.jsx";
import { BulkManagementToolbar } from "../components/dashboard/BulkManagementToolbar.jsx";
import { ContinueWatchingSection, DashboardAnalyticsSection, SmartReminderSection } from "../components/dashboard/JournalDashboardSections.jsx";
import { AnimeCatalog } from "../components/catalog/AnimeCatalog.jsx";
import { CatalogFilterLab } from "../components/catalog/CatalogFilterLab.jsx";
import { CatalogMeta } from "../components/catalog/CatalogMeta.jsx";
import { Icon } from "../components/Icon.jsx";
import { api, authApi, clearTokens, readableApiError } from "../lib/api.js";
import { resolveTagColors } from "../lib/tagPresets.js";
import { pressBeforeOpen } from "../lib/modalMotion.js";
import { matchesActivityFilter, runBounded } from "../lib/journalExperience.js";
import { useSiteSettings } from "../context/SiteSettingsContext.jsx";
import { usePluginRuntime } from "../plugins/sdk/PluginRuntimeContext.jsx";
import {
  SORT_OPTIONS,
  STATUS_OPTIONS,
  apiToRecord,
  blankRecord,
  comparePeriod,
  matchesQuickFilter,
  recordToApi,
} from "./dashboardData.js";
import {
  DangerZoneDialog,
  DashboardAvatar,
  ProfileMenu,
  ProfilePanel,
  QuickFilterEditor,
} from "./DashboardDialogs.jsx";
import { useDashboardData } from "./useDashboardData.js";
import { useDashboardEntrance } from "./useDashboardEntrance.js";
import { useDashboardImport } from "./useDashboardImport.js";


export function DashboardPage() {
  const { settings: siteSettings } = useSiteSettings();
  const rootRef = useRef(null);
  const navigate = useNavigate();
  const { navigation } = usePluginRuntime();
  const dashboardItems = navigation.filter((item) => item.area === "dashboard");
  const location = useLocation();
  const modeTransition = location.state?.dashboardModeTransition === true;
  const {
    dashboardReady,
    demoCatalogRecords,
    analytics,
    analyticsError,
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
  } = useDashboardData({ navigate });
  const [query, setQuery] = useState("");
  const [tag, setTag] = useState("all");
  const [status, setStatus] = useState("all");
  const [year, setYear] = useState("all");
  const [sort, setSort] = useState("date-desc");
  const [pageSize, setPageSize] = useState("all");
  const [priority, setPriority] = useState(true);
  const [activeQuick, setActiveQuick] = useState("all");
  const [activity, setActivity] = useState("all");
  const [view, setView] = useState("list");
  const [selected, setSelected] = useState(null);
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState(() => new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const [profileInitialTab, setProfileInitialTab] = useState("profile");
  const [bangumiImportOpen, setBangumiImportOpen] = useState(false);
  const [dangerZoneOpen, setDangerZoneOpen] = useState(false);
  const [profileMenuOpen, setProfileMenuOpen] = useState(false);
  const profileMenuAnchorRef = useRef(null);
  const profileAvatarButtonRef = useRef(null);
  const [filterEditorOpen, setFilterEditorOpen] = useState(false);
  const [notice, setNotice] = useState("");
  const [noticeKind, setNoticeKind] = useState("");
  const deepLinkRequestRef = useRef("");
  useEffect(() => { if (loadError) setNotice(loadError); }, [loadError]);
  useEffect(() => {
    if (!selected) return;
    const refreshed = records.find((record) => record.id === selected.record.id);
    if (refreshed && refreshed !== selected.record) {
      setSelected((current) => current ? { ...current, record: refreshed } : current);
    }
  }, [records, selected]);
  useDashboardEntrance({ dashboardReady, modeTransition, rootRef });

  const clearEntryQuery = () => {
    const params = new URLSearchParams(location.search);
    if (!params.has("entry")) return;
    params.delete("entry");
    navigate({ pathname: location.pathname, search: params.toString() ? `?${params}` : "" }, { replace: true });
  };

  const closeSelected = () => {
    setSelected(null);
    clearEntryQuery();
  };

  useEffect(() => {
    if (!profileMenuOpen) return undefined;
    const firstItem = profileMenuAnchorRef.current?.querySelector('[role="menuitem"]');
    const focusFrame = window.requestAnimationFrame(() => firstItem?.focus());
    const closeOnOutside = (event) => {
      if (!profileMenuAnchorRef.current?.contains(event.target)) setProfileMenuOpen(false);
    };
    const closeOnEscape = (event) => {
      if (event.key !== "Escape") return;
      setProfileMenuOpen(false);
      profileAvatarButtonRef.current?.focus();
    };
    document.addEventListener("pointerdown", closeOnOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.removeEventListener("pointerdown", closeOnOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [profileMenuOpen]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const outcome = params.get("external_account_status");
    if (!outcome) return;
    const providerSlug = params.get("external_account_provider");
    const provider = providerSlug === "bangumi" ? "Bangumi" : (providerSlug || "外部账号");
    setProfileInitialTab("external");
    setProfileOpen(true);
    flash(outcome === "connected" ? `${provider} 账号已安全连接。` : `${provider} 授权未完成，请重新尝试。`, "profile");
    window.history.replaceState(window.history.state, "", `${window.location.pathname}${window.location.hash}`);
  }, []);

  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const entryId = params.get("entry");
    if (!entryId || !dashboardReady || selected?.record?.id === Number(entryId) || selected?.record?.id === entryId) return;
    const localRecord = records.find((record) => String(record.id) === String(entryId));
    if (localRecord) {
      deepLinkRequestRef.current = entryId;
      openEditor(localRecord, null, false);
      return;
    }
    if (isDemo || deepLinkRequestRef.current === entryId || !/^\d+$/.test(entryId)) {
      if (!/^\d+$/.test(entryId)) {
        flash("作品链接无效，请从手账列表重新打开。", "error");
        clearEntryQuery();
      }
      return;
    }
    deepLinkRequestRef.current = entryId;
    api.get(`entries/${entryId}/`).then((response) => {
      const record = apiToRecord(response.data, presetColors);
      setRecords((current) => current.some((item) => String(item.id) === String(record.id)) ? current : [record, ...current]);
      openEditor(record, null, false);
    }).catch(() => {
      flash("没有找到这部作品，或它不属于当前账号。", "error");
      clearEntryQuery();
    });
  }, [dashboardReady, isDemo, location.pathname, location.search, presetColors, records, selected]);

  useEffect(() => {
    if (location.search.includes("entry=")) return;
    if (selected) setSelected(null);
  }, [location.search, selected]);

  const allTags = useMemo(() => [...new Set(records.flatMap((record) => record.tags || []))].sort((a, b) => a.localeCompare(b, "zh-CN")), [records]);
  const allYears = useMemo(() => [...new Set(records.map((record) => String(record.period || "").slice(0, 4)).filter((item) => /^\d{4}$/.test(item)))].sort((a, b) => Number(b) - Number(a)), [records]);
  const activeFilter = quickFilters.find((item) => String(item.id) === activeQuick);
  const filteredRecords = useMemo(() => {
    const filtered = records.filter((record) => {
      const haystack = `${record.title} ${record.japaneseTitle} ${record.studio}`.toLowerCase();
      return (!query.trim() || haystack.includes(query.trim().toLowerCase()))
        && (status === "all" || record.status === status)
        && (tag === "all" || record.tags?.includes(tag))
        && (year === "all" || String(record.period || "").startsWith(year))
        && matchesActivityFilter(record, activity)
        && matchesQuickFilter(record, activeFilter);
    });
    const sorted = [...filtered].sort((a, b) => {
      if (priority) {
        const aPriority = Number(a.score) > 0 || a.status === "completed";
        const bPriority = Number(b.score) > 0 || b.status === "completed";
        if (aPriority !== bPriority) return Number(bPriority) - Number(aPriority);
      }
      if (sort === "score-desc") return (Number(b.score) || -1) - (Number(a.score) || -1);
      if (sort === "score-asc") return (Number(a.score) || 99) - (Number(b.score) || 99);
      if (sort === "date-asc") return comparePeriod(a, b);
      if (sort === "updated-desc") return String(b.updatedAt || "").localeCompare(String(a.updatedAt || ""));
      if (sort === "updated-asc") return String(a.updatedAt || "").localeCompare(String(b.updatedAt || ""));
      return comparePeriod(b, a);
    });
    return sorted;
  }, [activeFilter, activity, priority, query, records, sort, status, tag, year]);
  const visible = pageSize === "all" ? filteredRecords : filteredRecords.slice(0, Number(pageSize));

  const flash = (message, kind = "") => {
    setNotice(message);
    setNoticeKind(kind);
    window.clearTimeout(flash.timer);
    flash.timer = window.setTimeout(() => { setNotice(""); setNoticeKind(""); }, 2400);
  };

  const {
    closeImport,
    confirmImport,
    fileRef,
    importBusy,
    importData,
    importError,
    importOpen,
    importPreview,
  } = useDashboardImport({ isDemo, presetColors, records, setRecords, flash });

  const saveRecord = async (record) => {
    const { posterFile, ...savedRecord } = record;
    const existing = records.some((item) => item.id === savedRecord.id);
    const previousRecord = existing ? records.find((item) => item.id === savedRecord.id) : null;
    setRecords((current) => existing ? current.map((item) => item.id === savedRecord.id ? savedRecord : item) : [savedRecord, ...current]);
    if (!isDemo) {
      try {
        const payload = recordToApi(savedRecord);
        if (existing) delete payload.poster_url;
        let response;
        if (posterFile) {
          const formData = new FormData();
          Object.entries(payload).forEach(([key, value]) => formData.append(key, Array.isArray(value) || typeof value === "object" ? JSON.stringify(value) : value ?? ""));
          formData.append("poster_file", posterFile);
          response = existing && Number.isFinite(Number(savedRecord.id))
            ? await api.patch(`entries/${savedRecord.id}/`, formData, { headers: { "Content-Type": "multipart/form-data" } })
            : await api.post("entries/", formData, { headers: { "Content-Type": "multipart/form-data" } });
        } else {
          response = existing && Number.isFinite(Number(savedRecord.id))
            ? await api.patch(`entries/${savedRecord.id}/`, payload)
            : await api.post("entries/", payload);
        }
        if (response.data) setRecords((current) => current.map((item) => item.id === savedRecord.id ? apiToRecord(response.data, presetColors) : item));
      } catch (requestError) {
        setRecords((current) => previousRecord ? current.map((item) => item.id === savedRecord.id ? previousRecord : item) : current.filter((item) => item.id !== savedRecord.id));
        throw new Error(readableApiError(requestError, "记录保存失败，请检查封面来源后重试。"));
      }
    }
    flash(existing ? "记录已更新" : "新番剧已加入手账");
  };

  const saveNewRecord = async (draft) => {
    const tags = String(draft.tagsText || "").split(/[，,]/).map((item) => item.trim()).filter(Boolean).slice(0, 30);
    const poster = draft.posterFile ? URL.createObjectURL(draft.posterFile) : draft.poster || "/assets/posters/poster-01.webp";
    const statusLabels = { completed: "看过", watching: "在看", planned: "想看", on_hold: "搁置", dropped: "弃番" };
    const record = {
      id: `local-${Date.now()}`,
      title: draft.title,
      japaneseTitle: draft.japaneseTitle || "",
      period: draft.period,
      studio: draft.studio || "待补充",
      episodes: draft.episodes || "待定",
      score: draft.score === "" || draft.score === null ? null : Number(draft.score),
      status: draft.status || "planned",
      statusLabel: statusLabels[draft.status] || "想看",
      tags,
      poster,
      posterUrl: draft.posterSource === "default_url" && /^https:\/\//i.test(String(draft.poster || "")) ? draft.poster : "",
      customPosterUrl: draft.posterSource === "trusted_url" && /^https:\/\//i.test(String(draft.poster || "")) ? draft.poster : "",
      posterSource: draft.posterFile ? "upload" : draft.posterSource || (draft.poster ? "trusted_url" : "none"),
      clearCustomPoster: false,
      description: draft.description || "",
      review: draft.review || "",
      baikeUrl: draft.baikeUrl || "https://mzh.moegirl.org.cn/",
      watchHistory: [],
      watchHistoryCount: 0,
      firstWatchedOn: null,
      lastWatchedOn: null,
      latestEpisodeStart: null,
      latestEpisodeEnd: null,
      tagColors: resolveTagColors(tags, {}, presetColors),
      shared: false,
      visibility: "private",
      externalIdentity: draft.externalIdentity || null,
      externalIdentities: [],
    };
    setRecords((current) => [record, ...current]);
    if (!isDemo) {
      try {
        const payload = recordToApi(record);
        if (draft.posterFile) {
          const formData = new FormData();
          Object.entries(payload).forEach(([key, value]) => formData.append(key, Array.isArray(value) || typeof value === "object" ? JSON.stringify(value) : value ?? ""));
          formData.append("poster_file", draft.posterFile);
          const response = await api.post("entries/", formData, { headers: { "Content-Type": "multipart/form-data" } });
          if (response.data) setRecords((current) => current.map((item) => item.id === record.id ? apiToRecord(response.data, presetColors) : item));
        } else {
          const response = await api.post("entries/", payload);
          if (response.data) setRecords((current) => current.map((item) => item.id === record.id ? apiToRecord(response.data, presetColors) : item));
        }
      } catch (requestError) {
        setRecords((current) => current.filter((item) => item.id !== record.id));
        throw new Error(readableApiError(requestError, "加入手账失败，请检查封面来源后重试。"));
      }
    }
    flash("新番剧已加入手账");
  };

  const deleteRecord = async (id) => {
    setRecords((current) => current.filter((record) => record.id !== id));
    flash("记录已删除");
    if (!isDemo && Number.isFinite(Number(id))) api.delete(`entries/${id}/`).catch(() => {});
  };

  const updateExternalIdentity = (entryId, { externalIdentities, entryPatch }) => {
    const applyUpdate = (record) => record.id === entryId
      ? { ...record, ...entryPatch, externalIdentities }
      : record;
    setRecords((current) => current.map(applyUpdate));
  };

  const openEditor = (record, source, updateUrl = true, initialTab = "overview") => {
    const container = source?.closest?.(".anime-list-row, .anime-poster-card__interaction") || source;
    const origin = container?.querySelector?.("img") || source;
    pressBeforeOpen(container, () => {
      setSelected({ record, initialTab, originRect: origin?.getBoundingClientRect?.() || null, returnFocus: source || container || null });
      if (!updateUrl) return;
      const params = new URLSearchParams(location.search);
      params.set("entry", record.id);
      navigate({ pathname: location.pathname, search: `?${params}` });
    });
  };

  const openEntryById = (entryId, initialTab = "overview") => {
    const record = records.find((item) => String(item.id) === String(entryId));
    if (record) {
      openEditor(record, null, true, initialTab);
      return;
    }
    const params = new URLSearchParams(location.search);
    params.set("entry", entryId);
    navigate({ pathname: location.pathname, search: `?${params}` });
  };

  const quickUpdateStatus = async (record, nextStatus) => {
    if (!record || record.status === nextStatus) return;
    const labels = Object.fromEntries(STATUS_OPTIONS.filter(([value]) => value !== "all"));
    const previous = record;
    const optimistic = { ...record, status: nextStatus, statusLabel: labels[nextStatus] || nextStatus };
    setRecords((current) => current.map((item) => item.id === record.id ? optimistic : item));
    try {
      if (!isDemo) {
        const response = await api.patch(`entries/${record.id}/`, { watch_status: nextStatus });
        setRecords((current) => current.map((item) => item.id === record.id ? apiToRecord(response.data, presetColors) : item));
      }
      flash(`已将《${record.title}》标记为${labels[nextStatus] || nextStatus}`);
    } catch (requestError) {
      setRecords((current) => current.map((item) => item.id === record.id ? previous : item));
      flash(readableApiError(requestError, "观看状态修改失败，请稍后重试。"), "error");
    }
  };

  const toggleSelected = (entryId) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(entryId)) next.delete(entryId);
      else next.add(entryId);
      return next;
    });
  };

  const toggleSelectionMode = () => {
    setSelectionMode((current) => !current);
    setSelectedIds(new Set());
  };

  const applyBulkOperation = async (operation, value) => {
    const targets = records.filter((record) => selectedIds.has(record.id));
    if (!targets.length || bulkBusy) return;
    setBulkBusy(true);
    const results = await runBounded(targets, async (record) => {
      let patch;
      if (operation === "status") patch = { watch_status: value };
      if (operation === "visibility") patch = { visibility: value };
      if (operation === "tag-add") {
        const tags = record.tags.includes(value) ? record.tags : [...record.tags, value];
        patch = { tags, tag_colors: resolveTagColors(tags, record.tagColors || {}, presetColors) };
      }
      if (operation === "tag-remove") {
        const tags = record.tags.filter((tagName) => tagName !== value);
        const tagColors = { ...(record.tagColors || {}) };
        delete tagColors[value];
        patch = { tags, tag_colors: tagColors };
      }
      if (isDemo) {
        const labels = Object.fromEntries(STATUS_OPTIONS.filter(([statusValue]) => statusValue !== "all"));
        return {
          ...record,
          ...(patch.watch_status ? { status: patch.watch_status, statusLabel: labels[patch.watch_status] } : {}),
          ...(patch.visibility ? { visibility: patch.visibility, shared: patch.visibility !== "private" } : {}),
          ...(patch.tags ? { tags: patch.tags, tagColors: patch.tag_colors } : {}),
        };
      }
      const response = await api.patch(`entries/${record.id}/`, patch);
      return apiToRecord(response.data, presetColors);
    }, 4);
    const successful = results.filter((result) => result.status === "fulfilled");
    const failed = results.length - successful.length;
    const updates = new Map(successful.map((result) => [result.value.id, result.value]));
    setRecords((current) => current.map((record) => updates.get(record.id) || record));
    setSelectedIds(new Set());
    setBulkBusy(false);
    flash(`批量操作完成：成功 ${successful.length}，失败 ${failed}${failed ? "。失败项目保持原值" : ""}`, failed ? "error" : "");
  };

  const exportData = async () => {
    try {
      const bundle = isDemo
        ? {
            format: "animemo-data-bundle",
            schema_version: 1,
            exported_at: new Date().toISOString(),
            entries: records.map((record) => ({
              entry: {
                title: record.title || "",
                japanese_title: record.japaneseTitle || "",
                airing_period: record.period || "",
                studio: record.studio || "",
                episodes: record.episodes || "",
                description: record.description || "",
                poster_url: record.posterUrl || "",
                custom_poster_url: record.customPosterUrl || "",
                baike_url: record.baikeUrl || "",
                tags: record.tags || [],
                tag_colors: record.tagColors || {},
                personal_score: record.score ?? null,
                watch_status: record.status || "planned",
                review: record.review || "",
                visibility: record.visibility || (record.shared ? "public" : "private"),
              },
              external_identities: record.externalIdentities || [],
              watch_history: record.watchHistory || [],
            })),
          }
        : (await api.get("export/")).data;
      const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `animemo-data-bundle-${new Date().toISOString().slice(0, 10)}.json`;
      anchor.click();
      URL.revokeObjectURL(url);
      flash("手账数据已导出");
    } catch (requestError) {
      flash(readableApiError(requestError, "手账数据导出失败，请稍后重试。"));
    }
  };

  const saveSettings = async (next) => {
    setSettings(next);
    setProfileOpen(false);
    window.requestAnimationFrame(() => profileAvatarButtonRef.current?.focus({ preventScroll: true }));
    flash("个人资料已更新。", "profile");
    if (!isDemo) {
      try { await api.patch("settings/me/", { nickname: next.nickname, showcase_subtitle: next.subtitle, accent: next.accent }); }
      catch { flash("资料已保存到本地，服务器同步稍后重试"); }
    }
  };

  const resetFilters = () => {
    setQuery(""); setTag("all"); setStatus("all"); setYear("all"); setActivity("all"); setSort("date-desc"); setPageSize("all"); setPriority(true); setActiveQuick("all");
    flash("筛选条件已恢复默认");
  };

  const changeView = (nextView) => {
    if (nextView === view) return;
    if (typeof document.startViewTransition === "function") {
      document.startViewTransition(() => setView(nextView));
    } else {
      setView(nextView);
    }
  };

  const changeCatalogFilter = (key, value) => {
    if (key === "search") setQuery(value);
    if (key === "tag") setTag(value);
    if (key === "status") setStatus(value);
    if (key === "year") setYear(value);
    if (key === "activity") setActivity(value);
    if (key === "sort") setSort(value);
    if (key === "quick") {
      setActiveQuick(value);
      setTag("all");
    }
  };

  const changePublicJournalStatus = async (action) => {
    if (isDemo) {
      const nextStatus = action === "cancel" ? "private" : "pending";
      setSettings((current) => ({ ...current, publicStatus: nextStatus, publicProfile: false }));
      flash(action === "cancel" ? "已取消分享，你的手账已立即恢复私密。" : "分享申请已提交，请等待管理员审核。");
      return;
    }

    try {
      const response = action === "cancel"
        ? await api.patch("public-journal/status/", {})
        : await api.post("public-journal/status/", {});
      const data = response.data || {};
      setSettings((current) => ({
        ...current,
        publicStatus: data.public_status || (action === "cancel" ? "private" : "pending"),
        publicProfile: data.is_public ?? data.allow_sharing ?? false,
        publicSlug: data.public_slug || current.publicSlug,
      }));
      flash(action === "cancel"
        ? "已取消分享，你的手账已立即恢复私密。"
        : data.public_status === "approved"
          ? "管理员手账已直接公开，无需审核。"
          : "分享申请已提交，请等待管理员审核。");
    } catch (requestError) {
      flash(readableApiError(requestError, action === "cancel" ? "取消分享失败，请稍后重试。" : "分享申请提交失败，请稍后重试。"));
      throw requestError;
    }
  };

  const saveQuickFilter = async (filter) => {
    const payload = { name: filter.name, tags: filter.tags || [], title_keywords: filter.title_keywords || [], match_mode: filter.match_mode || "any", color: filter.color || "#ffe66d" };
    if (isDemo) {
      const local = { ...payload, id: filter.id || `local-filter-${Date.now()}` };
      setQuickFilters((current) => filter.id ? current.map((item) => String(item.id) === String(filter.id) ? local : item) : [...current, local]);
      setActiveQuick(String(local.id));
      setFilterEditorOpen(false);
      flash("自定义筛选已保存");
      return;
    }
    try {
      const response = filter.id ? await api.patch(`filters/${filter.id}/`, payload) : await api.post("filters/", payload);
      const saved = response.data;
      setQuickFilters((current) => filter.id ? current.map((item) => String(item.id) === String(filter.id) ? saved : item) : [...current, saved]);
      setActiveQuick(String(saved.id));
      setFilterEditorOpen(false);
      flash("自定义筛选已同步");
    } catch { flash("筛选保存失败，请稍后重试"); }
  };

  const deleteQuickFilter = async (filter) => {
    setQuickFilters((current) => current.filter((item) => String(item.id) !== String(filter.id)));
    if (String(activeQuick) === String(filter.id)) setActiveQuick("all");
    setFilterEditorOpen(false);
    flash("自定义筛选已删除");
    if (!isDemo && Number.isFinite(Number(filter.id))) api.delete(`filters/${filter.id}/`).catch(() => flash("服务器删除稍后重试"));
  };

  const logout = async () => {
    setProfileMenuOpen(false);
    try {
      await authApi.logout();
    } finally {
      clearTokens();
      localStorage.removeItem("anime_journal_demo");
      setAuthSnapshot({});
      navigate("/login");
    }
  };

  const returnHome = async () => {
    setProfileMenuOpen(false);
    try {
      await authApi.logout();
    } finally {
      clearTokens();
      localStorage.removeItem("anime_journal_demo");
      setAuthSnapshot({});
      navigate("/", { replace: true });
    }
  };
  const openProfilePanel = (tab = "profile") => {
    setProfileMenuOpen(false);
    setProfileInitialTab(tab);
    setProfileOpen(true);
  };
  const openDangerZone = () => {
    setProfileMenuOpen(false);
    setDangerZoneOpen(true);
  };
  const handleAccountMenuKeyDown = (event) => {
    const items = [...(profileMenuAnchorRef.current?.querySelectorAll('[role="menuitem"]') || [])];
    const currentIndex = items.indexOf(document.activeElement);
    let nextIndex = currentIndex;
    if (event.key === "ArrowDown") nextIndex = (currentIndex + 1) % items.length;
    else if (event.key === "ArrowUp") nextIndex = (currentIndex - 1 + items.length) % items.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = items.length - 1;
    else if (event.key === "Tab") { setProfileMenuOpen(false); return; }
    else return;
    event.preventDefault();
    items[nextIndex]?.focus();
  };
  const changePassword = async (payload) => {
    if (isDemo) return;
    await authApi.changePassword(payload);
    window.setTimeout(logout, 900);
  };
  const deleteAccount = async (payload) => {
    if (isDemo) {
      Object.keys(localStorage).filter((key) => key.startsWith("anime_journal_")).forEach((key) => localStorage.removeItem(key));
      logout();
      return;
    }
    await authApi.deleteAccount(payload);
    logout();
  };

  const scored = records.filter((record) => Number(record.score) > 0);
  const catalogFilters = { search: query, tag, status, year, activity, sort, quick: activeQuick };
  const displayedAnalytics = analytics || (isDemo ? {
    summary: {
      total: records.length,
      watch_history_count: records.reduce((total, record) => total + Number(record.watchHistoryCount || record.watchHistory?.length || 0), 0),
      active_days: new Set(records.flatMap((record) => (record.watchHistory || []).map((item) => item.watched_on).filter(Boolean))).size,
    },
    status_distribution: Object.fromEntries(STATUS_OPTIONS.filter(([value]) => value !== "all").map(([value]) => [value, records.filter((record) => record.status === value).length])),
    activity_summary: { today: 0, last_7_days: 0, current_month: 0 },
    recent_activity: [],
  } : null);

  return (
    <main className="dashboard-page dashboard-page--target" ref={rootRef} style={{ "--user-accent": settings.accent }}>
      <div className="dashboard-memphis-grid" aria-hidden="true" />
      <div className="dashboard-float-ring" aria-hidden="true" />
      <div className="dashboard-float-triangle" aria-hidden="true" />
      <div className="dashboard-float-zigzag" aria-hidden="true" />

      <header className={`dashboard-memphis-console dashboard-console-piece${modeTransition ? " dashboard-mode-transition" : ""}`}>
        <div className="dashboard-console-pattern" aria-hidden="true" />
        <div className="dashboard-console-shell">
          <div className="dashboard-console-left">
            <span className="dashboard-private-zone"><Icon name="bolt" /> PRIVATE JOURNAL / 私人手账</span>
            <div className="dashboard-profile-identity">
              <div className="dashboard-profile-menu-anchor" ref={profileMenuAnchorRef}>
                <button ref={profileAvatarButtonRef} className="dashboard-profile-avatar" type="button" onClick={() => setProfileMenuOpen((current) => !current)} aria-label="打开账户菜单" aria-haspopup="menu" aria-expanded={profileMenuOpen}><DashboardAvatar src={settings.avatar} alt={`${settings.nickname.trim() || settings.email || "当前账户"}的头像`} /></button>
                {profileMenuOpen && <ProfileMenu settings={settings} onEdit={() => openProfilePanel("profile")} onLogout={logout} onDelete={openDangerZone} onKeyDown={handleAccountMenuKeyDown} />}
              </div>
              <div className="dashboard-profile-copy"><h1>{`${settings.nickname.trim() || settings.email || "当前账户"}的番剧手账房`}</h1><p>{settings.subtitle}<button type="button" onClick={() => openProfilePanel("profile")} aria-label="修改展示副标题"><Icon name="edit" /></button></p></div>
            </div>
          </div>
          <div className="dashboard-console-actions">
            <nav className="dashboard-console-tools" aria-label="手账工具">
              <DashboardReturnHomeControl onConfirm={returnHome} />
              <button className="dashboard-arcade-button is-yellow" type="button" onClick={exportData}><Icon name="export" /><span className="dashboard-arcade-button__label">导出数据</span></button>
              <button className="dashboard-arcade-button is-coral" type="button" onClick={() => fileRef.current?.click()}><Icon name="upload" /><span className="dashboard-arcade-button__label">导入备份</span></button>
              <button className="dashboard-arcade-button" type="button" onClick={() => navigate(`/shared/${settings.publicSlug || "local-preview"}?preview=1`, { state: { dashboardModeTransition: true } })}><Icon name="eye" /><span className="dashboard-arcade-button__label">预览模式</span></button>
              <DashboardShareControl publicStatus={settings.publicStatus} onChange={changePublicJournalStatus} />
              <button className="dashboard-arcade-button is-yellow" type="button" onClick={() => navigate("/plugins")}><Icon name="puzzle" /><span className="dashboard-arcade-button__label">插件中心</span></button>
              {dashboardItems.map((item) => <button key={`${item.pluginSlug}:${item.id}`} className="dashboard-arcade-button is-plugin" type="button" onClick={() => navigate(item.path)}><Icon name={item.icon || "puzzle"} /><span className="dashboard-arcade-button__label">{item.label}</span></button>)}
              <input ref={fileRef} type="file" accept=".json,.csv,application/json,text/csv" onChange={importData} hidden />
            </nav>
            <div className="dashboard-add-anime-wrap">
              <button className="dashboard-add-anime-cta" type="button" onClick={() => setAddOpen(true)}><span className="dashboard-add-icon"><Icon name="plus" /></span><span className="dashboard-add-copy"><span className="dashboard-add-title">添加番剧</span><span className="dashboard-add-caption">EXPAND YOUR JOURNAL</span></span><span className="dashboard-add-sticker">ADD</span></button>
            </div>
          </div>
        </div>
      </header>

      <div className="dashboard-journal-workbench dashboard-entrance-piece">
        <ContinueWatchingSection records={records} onOpen={(record, source) => openEditor(record, source)} onRecord={(record, source) => openEditor(record, source, true, "history")} onComplete={(record) => quickUpdateStatus(record, "completed")} onAdd={() => setAddOpen(true)} />
        <DashboardAnalyticsSection analytics={displayedAnalytics} error={analyticsError} onOpenEntry={openEntryById} />
        <SmartReminderSection records={records} onOpen={(record, source) => openEditor(record, source)} />
      </div>

      <section className="dashboard-main dashboard-entrance-piece">
        <CatalogFilterLab
          filters={catalogFilters}
          onFilterChange={changeCatalogFilter}
          onReset={resetFilters}
          viewMode={view}
          onViewChange={changeView}
          resultCount={filteredRecords.length}
          tags={allTags}
          years={allYears}
          quickFilters={quickFilters}
          onEditQuickFilters={() => setFilterEditorOpen(true)}
          statusOptions={STATUS_OPTIONS}
          sortOptions={SORT_OPTIONS}
        />

        <BulkManagementToolbar active={selectionMode} selectedCount={selectedIds.size} tags={allTags} busy={bulkBusy} onToggle={toggleSelectionMode} onClear={() => setSelectedIds(new Set())} onApply={applyBulkOperation} />

        <CatalogMeta resultCount={filteredRecords.length} pageSize={pageSize} onPageSizeChange={setPageSize} unscoredCount={records.length - scored.length} pageSizeOptions={["12", "24", "48", "96"]} />
        <div className="hazard-line" aria-hidden="true" />

        <section className={`dashboard-results dashboard-results--${view} dashboard-entrance-piece`}>
          {visible.length > 0 && <AnimeCatalog records={visible} viewMode={view} onOpenDetail={openEditor} sort={sort} onSortChange={setSort} ready={dashboardReady} variant="editable" onAddRecord={() => setAddOpen(true)} onQuickStatus={quickUpdateStatus} selectionMode={selectionMode} selectedIds={selectedIds} onToggleSelection={toggleSelected} />}
          {!visible.length && <button className="dashboard-empty-state" type="button" onClick={() => records.length ? resetFilters() : setAddOpen(true)}><span className="dashboard-empty-plus"><Icon name="plus" /></span><h2>{records.length ? "没有匹配的番剧" : "手账还是空的"}</h2><p>{records.length ? "换一个筛选条件，或者恢复默认筛选。" : "点击这里，添加第一部属于你的番剧。"}</p><strong>{records.length ? "恢复默认" : "开始添加"} <Icon name="arrow-right" /></strong></button>}
        </section>
      </section>

      {notice && noticeKind === "profile" ? <div className="dashboard-profile-toast" role="status"><span className="dashboard-profile-toast__icon" aria-hidden="true"><Icon name="circle-check" /></span><span className="dashboard-profile-toast__message">{notice}</span><button className="dashboard-profile-toast__close" type="button" onClick={() => { setNotice(""); setNoticeKind(""); }} aria-label="关闭提示"><Icon name="close" /></button></div> : notice ? <div className="brutal-toast dashboard-toast" role="status"><Icon name="check" /> {notice}</div> : null}
      {selected && <AnimeModal record={selected.record} originRect={selected.originRect} returnFocus={selected.returnFocus} editable isDemo={isDemo} initialTab={selected.initialTab} onClose={closeSelected} onSave={saveRecord} onDelete={records.some((record) => record.id === selected.record.id) ? deleteRecord : null} onIdentityChange={(update) => updateExternalIdentity(selected.record.id, update)} onOpenExternalAccount={() => { closeSelected(); openProfilePanel("external"); }} tagPresets={tagPresets} trustedPosterHosts={siteSettings.trusted_poster_hosts} />}
      {addOpen && <AddAnimeModal isDemo={isDemo} catalogRecords={demoCatalogRecords} existingRecords={records} onClose={() => setAddOpen(false)} onSubmit={saveNewRecord} trustedPosterHosts={siteSettings.trusted_poster_hosts} />}
      {profileOpen && <ProfilePanel settings={settings} initialTab={profileInitialTab} isDemo={isDemo} onClose={() => { setProfileOpen(false); window.requestAnimationFrame(() => profileAvatarButtonRef.current?.focus({ preventScroll: true })); }} onSave={saveSettings} onChangePassword={changePassword} onOpenBangumiImport={() => { setProfileOpen(false); setBangumiImportOpen(true); }} />}
      {bangumiImportOpen && <BangumiImportDialog onClose={() => setBangumiImportOpen(false)} />}
      {dangerZoneOpen && <DangerZoneDialog settings={settings} isDemo={isDemo} onClose={() => { setDangerZoneOpen(false); window.requestAnimationFrame(() => profileAvatarButtonRef.current?.focus({ preventScroll: true })); }} onDeleteAccount={deleteAccount} />}
      {filterEditorOpen && <QuickFilterEditor filters={quickFilters} onClose={() => setFilterEditorOpen(false)} onSave={saveQuickFilter} onDelete={deleteQuickFilter} />}
      {importOpen && <ImportJournalModal fileName={importFile?.name} preview={importPreview} busy={importBusy} error={importError} onClose={closeImport} onConfirm={confirmImport} />}
    </main>
  );
}
