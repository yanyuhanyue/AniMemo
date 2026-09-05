import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import gsap from "gsap";
import {
  calculateShowcaseStats,
  quickFilters,
} from "../data/catalogData.js";
import { FeaturedAnimeModal } from "../components/featured/FeaturedAnimeModal.jsx";
import { Icon } from "../components/Icon.jsx";
import { AppBootLoader } from "../components/AppBootLoader.jsx";
import { AnimeCatalog } from "../components/catalog/AnimeCatalog.jsx";
import { CatalogFilterLab } from "../components/catalog/CatalogFilterLab.jsx";
import { CatalogMeta } from "../components/catalog/CatalogMeta.jsx";
import { api, authApi, clearTokens, getStoredTokens } from "../lib/api.js";
import { buildPresetColorMap, normalizeTagPresets, resolveTagColors } from "../lib/tagPresets.js";
import { pressBeforeOpen } from "../lib/modalMotion.js";
import { usePageColorTransition } from "../components/PageColorTransition.jsx";
import { SharedShowcaseHeader } from "../components/SharedShowcaseHeader.jsx";
import { useSiteSettings } from "../context/SiteSettingsContext.jsx";
import { demoAnimeRecords, demoEnabled, getDemoUniverseOwner } from "@demo-data";
import { hydrateDemoAnimeRecords, hydrateDemoUniverseOwner, reconcileDemoRecords } from "../lib/demoMedia.js";
import {
  ANIMEMO_AVATAR_PATH,
  ANIMEMO_POSTER_FALLBACK_PATH,
  normalizeBundledPosterPath,
} from "../lib/mediaAssets.js";

const RECORDS_STORAGE_KEY = "anime_journal_records_v1";
const SETTINGS_STORAGE_KEY = "anime_journal_settings_v1";
const RECORDS_UPDATED_EVENT = "anime-journal:records-updated";
const DEFAULT_FILTERS = { search: "", tag: "all", status: "all", year: "all", sort: "date-desc", quick: "all" };

function apiToRecord(item, presetColors) {
  const externalIdentities = Array.isArray(item.external_identities) ? item.external_identities : [];
  const bangumiIdentity = externalIdentities.find((identity) => (
    String(identity?.provider || "").toLowerCase() === "bangumi"
    && String(identity?.external_id || "").trim()
  ));
  return {
    id: item.id,
    title: item.title,
    japaneseTitle: item.japanese_title || "",
    period: item.airing_period || "未定档",
    studio: item.studio || "待补充",
    episodes: item.episodes || "待定",
    score: item.personal_score === null || item.personal_score === "" ? null : Number(item.personal_score),
    status: item.watch_status,
    statusLabel: item.watch_status_display || { completed: "看过", watching: "在看", planned: "想看", on_hold: "搁置" }[item.watch_status] || "想看",
    tags: item.tags || [],
    tagColors: resolveTagColors(item.tags || [], item.tag_colors || {}, presetColors),
    poster: normalizeBundledPosterPath(item.poster || item.poster_url),
    posterOriginal: item.poster_original || item.poster || item.poster_url || "",
    description: item.description || "",
    review: item.review || "",
    baikeUrl: item.baike_url || "",
    externalIdentities,
    resourceIdentity: bangumiIdentity ? {
      provider: "bangumi",
      externalId: String(bangumiIdentity.external_id).trim(),
    } : null,
    watchHistory: [],
  };
}

function recordToFeaturedColumn(record) {
  const bangumiIdentity = record.resourceIdentity || (record.externalIdentities || []).find((identity) => (
    String(identity?.provider || "").toLowerCase() === "bangumi"
    && String(identity?.external_id || "").trim()
  ));
  const canonicalUrl = bangumiIdentity?.canonicalUrl
    || bangumiIdentity?.canonical_url
    || (bangumiIdentity?.externalId ? `https://bgm.tv/subject/${bangumiIdentity.externalId}` : "")
    || (bangumiIdentity?.external_id ? `https://bgm.tv/subject/${bangumiIdentity.external_id}` : "");
  return {
    id: `showcase-${record.id}`,
    anime: {
      title: record.title,
      japaneseTitle: record.japaneseTitle,
      poster: normalizeBundledPosterPath(record.poster),
      posterOriginal: record.posterOriginal || "",
      externalUrl: canonicalUrl || record.baikeUrl || "",
      externalSource: canonicalUrl ? "Bangumi" : "外部资料",
      resourceIdentity: record.resourceIdentity || null,
      period: record.period,
      score: record.score,
      studio: record.studio,
      episodeCount: record.episodes,
      tags: record.tags || [],
      tagColors: record.tagColors || {},
      summary: record.description,
      personalReview: record.review,
      watchHistory: record.watchHistory || [],
    },
  };
}

function normalizeLocalRecordColors(record, presetColors) {
  return {
    ...record,
    tagColors: resolveTagColors(record.tags || [], record.tagColors || record.tag_colors || {}, presetColors),
  };
}

function readStoredRecords() {
  try {
    const records = JSON.parse(localStorage.getItem(RECORDS_STORAGE_KEY) || "null");
    if (!Array.isArray(records)) return [];
    return records.map((record) => (
      record?.title === "《超时空辉夜姬！》" && record?.studio === "Studio Colorido"
        ? { ...record, studio: "Studio Colorido、STUDIO CHROMATO" }
        : record
    ));
  } catch {
    return [];
  }
}

function isExplicitDemoMode() {
  return demoEnabled
    && localStorage.getItem("anime_journal_demo") === "true";
}

function readStoredPreviewSettings() {
  try {
    return JSON.parse(localStorage.getItem(SETTINGS_STORAGE_KEY) || "null") || {};
  } catch {
    return {};
  }
}

function isOva(record) {
  return record.tags?.some((tag) => tag.toUpperCase() === "OVA")
    || /(^|\s|《)OVA($|\s|》)/i.test(`${record.title || ""} ${record.japaneseTitle || ""}`);
}

function periodSortValue(period) {
  const match = String(period || "").match(/^(\d{4})-(\d{1,2})$/);
  return match ? Number(match[1]) * 100 + Number(match[2]) : 0;
}

function Header({ profile, sharedMode, siteSettings }) {
  const { isTransitioning, navigateWithTransition } = usePageColorTransition();
  const nickname = profile?.nickname || "AniMemo";
  const title = sharedMode ? `${nickname} 的番剧汇总` : siteSettings.homepage_title;
  const subtitle = sharedMode ? (profile?.subtitle || "把每一次与动画相遇认真收藏。") : siteSettings.homepage_description;
  const avatar = sharedMode ? (profile?.avatar || ANIMEMO_AVATAR_PATH) : siteSettings.site_avatar_url;
  return (
    <header className="showcase-hero">
      <div className="hero-grid" aria-hidden="true" />
      <div className="hero-circle" aria-hidden="true"><Icon name="star" /></div>
      <div className="showcase-hero__inner">
        <div className="showcase-hero__copy hero-piece">
          <span className="journal-kicker"><Icon name="heart" /> 私人手账 / 追番记录</span>
          <div className="showcase-title-row">
            <div className="avatar-frame"><img src={avatar} alt={sharedMode ? nickname : siteSettings.site_name} /></div>
            <h1><span>{title}</span></h1>
          </div>
          <p>{subtitle}</p>
        </div>

        <div className="showcase-hero__actions hero-piece">
          <button className="hero-action hero-action--universe" type="button" disabled={isTransitioning} onClick={() => navigateWithTransition("/universe")}>
            <span className="hero-action__visual teal"><Icon name="earth" /> 番剧共创宇宙</span>
          </button>
          <button className="hero-action hero-action--featured" type="button" disabled={isTransitioning} onClick={() => navigateWithTransition("/featured")}>
            <span className="hero-action__visual pink"><Icon name="award" /> 浏览精选专栏</span>
          </button>
          <button id="startJournal" className="journal-cta" type="button" disabled={isTransitioning} onClick={() => navigateWithTransition("/login") }>
            <span className="journal-cta__visual">
              <span className="journal-cta__icon"><Icon name="wand" /></span>
              <span className="journal-cta__copy">
                <small>START YOUR JOURNAL <b>FREE</b></small>
                <strong>设计自己的个人手账！</strong>
                <em>注册 · 登录 · 跨设备同步</em>
              </span>
              <span className="journal-cta__arrow"><Icon name="arrow-right" /></span>
            </span>
          </button>
        </div>
      </div>
    </header>
  );
}

function Stats({ stats, interactive = true }) {
  return (
    <section className="stat-grid" aria-label="番剧统计">
      {stats.map((stat) => {
        const Tag = interactive ? "button" : "article";
        return (
          <Tag
            {...(interactive ? { type: "button" } : {})}
            className={`stat-card stat-card--${stat.key} stat-piece`}
            key={stat.key}
            onClick={interactive ? (event) => { if (event.detail > 0) event.currentTarget.blur(); } : undefined}
            aria-label={`${stat.label}：${stat.value}，统计展示`}
          >
            <span className={`stat-card__visual ${stat.color}`}>
              <span className="stat-card__label"><span>{stat.label}</span><i><Icon name={stat.icon} /></i></span>
              <strong className={`${["completed", "average", "masterpiece"].includes(stat.key) ? "boxed" : ""}${["average", "ova"].includes(stat.key) ? " accent" : ""}`}>{stat.value}</strong>
              <small>{stat.note}</small>
            </span>
          </Tag>
        );
      })}
    </section>
  );
}

export function ShowcasePage({ sharedMode = false }) {
  const { settings: siteSettings } = useSiteSettings();
  const rootRef = useRef(null);
  const initialDataSettledRef = useRef(false);
  const navigate = useNavigate();
  const location = useLocation();
  const { publicSlug: routePublicSlug = "" } = useParams();
  const [searchParams] = useSearchParams();
  const ownerPreview = sharedMode && searchParams.get("preview") === "1";
  const modeTransition = ownerPreview && location.state?.dashboardModeTransition === true;
  const publicSlug = routePublicSlug.trim()
    || searchParams.get("showcase")?.trim()
    || import.meta.env.VITE_PUBLIC_SHOWCASE_SLUG?.trim();
  const explicitDemo = isExplicitDemoMode();
  const [records, setRecords] = useState(() => explicitDemo ? readStoredRecords() : []);
  const [remoteStats, setRemoteStats] = useState(null);
  const [profile, setProfile] = useState(null);
  const [ownerPublicStatus, setOwnerPublicStatus] = useState(() => demoEnabled ? (readStoredPreviewSettings().publicStatus || "private") : "private");
  const [dataError, setDataError] = useState("");
  const [view, setView] = useState("list");
  const [selected, setSelected] = useState(null);
  const [modalSourceElement, setModalSourceElement] = useState(null);
  const [showBackTop, setShowBackTop] = useState(false);
  const [pageSize, setPageSize] = useState("all");
  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  const [dataReady, setDataReady] = useState(false);
  const [bootComplete, setBootComplete] = useState(sharedMode);
  const [loaderVisible, setLoaderVisible] = useState(!sharedMode);
  const demoMediaCacheRef = useRef(new Map());
  const changeCatalogView = useCallback((nextView) => {
    if (nextView === view) return;

    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reducedMotion || typeof document.startViewTransition !== "function") {
      setView(nextView);
      return;
    }

    document.startViewTransition(() => {
      flushSync(() => setView(nextView));
    }).finished.catch(() => {});
  }, [view]);
  const handleBootComplete = useCallback(() => {
    setBootComplete(true);
    setLoaderVisible(false);
  }, []);

  useEffect(() => {
    if (dataReady) setBootComplete(true);
  }, [dataReady]);

  useEffect(() => {
    if (!sharedMode) return;
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  }, [publicSlug, sharedMode]);

  useEffect(() => {
    if (!ownerPreview) return undefined;
    const { access } = getStoredTokens();
    if (demoEnabled && (!access || localStorage.getItem("anime_journal_demo") === "true")) {
      setOwnerPublicStatus(readStoredPreviewSettings().publicStatus || "private");
      return undefined;
    }
    if (!access) return undefined;

    let cancelled = false;
    api.get("settings/me/").then(({ data }) => {
      if (cancelled) return;
      setOwnerPublicStatus(data.public_status || "private");
      setProfile((current) => ({
        ...current,
        nickname: data.nickname || data.username || current?.nickname,
        subtitle: data.showcase_subtitle || current?.subtitle,
        avatar: Object.hasOwn(data, "avatar_url") ? data.avatar_url : current?.avatar,
        public_slug: data.public_slug || current?.public_slug,
      }));
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [ownerPreview]);

  useEffect(() => {
    let cancelled = false;
    let refreshTimer;
    const markInitialDataReady = () => {
      if (cancelled || initialDataSettledRef.current) return;
      initialDataSettledRef.current = true;
      setDataReady(true);
    };

    const loadDemoOwner = async () => {
      if (!demoEnabled || !publicSlug) return null;
      return hydrateDemoUniverseOwner(getDemoUniverseOwner(publicSlug), { client: api, cache: demoMediaCacheRef.current });
    };

    const applyLocalRecords = async (providedDemo = null) => {
      if (cancelled) return;
      const demo = providedDemo || await loadDemoOwner();
      if (cancelled) return;
      if (demo) {
        setRecords(await hydrateDemoAnimeRecords(demo.records, { client: api, cache: demoMediaCacheRef.current }));
        setRemoteStats(demo.stats);
        setProfile({
          nickname: demo.nickname,
          subtitle: demo.subtitle,
          avatar: demo.avatar,
          public_slug: demo.public_slug,
        });
        return;
      }
      let localRecords = readStoredRecords();
      if (!localRecords.length && demoEnabled) localRecords = demoAnimeRecords;
      if (demoEnabled) {
        localRecords = reconcileDemoRecords(localRecords, demoAnimeRecords);
        localRecords = await hydrateDemoAnimeRecords(localRecords, {
          client: api,
          cache: demoMediaCacheRef.current,
        });
      }
      localRecords = localRecords.map((record) => ({
        ...normalizeLocalRecordColors(record),
        poster: normalizeBundledPosterPath(record.poster),
      }));
      if (cancelled) return;
      if (ownerPreview) {
        const settings = readStoredPreviewSettings();
        setRecords(localRecords);
        setRemoteStats(null);
        setProfile({
          nickname: settings.nickname || settings.email || "AniMemo",
          subtitle: settings.subtitle || "把每一次与动画相遇认真收藏。",
          avatar: settings.avatar || ANIMEMO_AVATAR_PATH,
          public_slug: publicSlug || "local-preview",
        });
        setOwnerPublicStatus(settings.publicStatus || "private");
        return;
      }
      setRecords(localRecords);
      setRemoteStats(null);
      setProfile(sharedMode ? {
        nickname: "公开同好",
        subtitle: "这位同好正在公开分享自己的观看轨道。",
        avatar: ANIMEMO_AVATAR_PATH,
        public_slug: publicSlug || "",
      } : null);
    };

    const refresh = async () => {
      if (!publicSlug) {
        if (ownerPreview || isExplicitDemoMode()) {
          await applyLocalRecords();
          setDataError("");
          markInitialDataReady();
          return;
        }
        try {
          const [{ data }, tagPresetsResponse] = await Promise.all([
            api.get("homepage/"),
            api.get("tag-presets/").catch(() => null),
          ]);
          if (cancelled) return;
          const presetColors = buildPresetColorMap(normalizeTagPresets(tagPresetsResponse?.data, []));
          setRecords(Array.isArray(data.results) ? data.results.map((item) => apiToRecord(item, presetColors)) : []);
          setRemoteStats(data.stats || null);
          setProfile(null);
          setDataError("");
        } catch {
          if (demoEnabled) await applyLocalRecords();
          else {
            setRecords([]);
            setRemoteStats(null);
            setProfile(null);
            setDataError("首页数据加载失败，请检查服务器连接。");
          }
        } finally {
          markInitialDataReady();
        }
        return;
      }
      const demoOwner = await loadDemoOwner();
      if (demoOwner) {
        await applyLocalRecords(demoOwner);
        setDataError("");
        markInitialDataReady();
        return;
      }
      try {
        const [{ data }, tagPresetsResponse] = await Promise.all([
          api.get(`showcase/${encodeURIComponent(publicSlug)}/`),
          api.get("tag-presets/").catch(() => null),
        ]);
        if (cancelled) return;
        const presetColors = buildPresetColorMap(normalizeTagPresets(tagPresetsResponse?.data, []));
        setRecords(Array.isArray(data.results) ? data.results.map((item) => apiToRecord(item, presetColors)) : []);
        setRemoteStats(data.stats || null);
        setProfile(data.profile ? {
          ...data.profile,
          avatar: data.profile.avatar || data.profile.avatar_url || "/assets/avatar.png",
          public_slug: data.profile.public_slug || publicSlug,
        } : null);
        setDataError("");
      } catch {
        if (demoEnabled) await applyLocalRecords();
        else {
          setRecords([]);
          setRemoteStats(null);
          setProfile(null);
          setDataError("公开手账加载失败或不存在。");
        }
      } finally {
        markInitialDataReady();
      }
    };

    const handleStorage = (event) => {
      if (!publicSlug && isExplicitDemoMode() && (!event.key || event.key === RECORDS_STORAGE_KEY)) void applyLocalRecords();
    };
    const handleRecordsUpdated = (event) => {
      if (publicSlug) return;
      if (isExplicitDemoMode()) {
        if (Array.isArray(event.detail)) setRecords(event.detail);
        else void applyLocalRecords();
        setRemoteStats(null);
      } else {
        void refresh();
      }
    };
    const handleFocus = () => refresh();

    refresh();
    if (publicSlug) refreshTimer = window.setInterval(refresh, 60000);
    window.addEventListener("focus", handleFocus);
    window.addEventListener("storage", handleStorage);
    window.addEventListener(RECORDS_UPDATED_EVENT, handleRecordsUpdated);
    return () => {
      cancelled = true;
      window.clearInterval(refreshTimer);
      window.removeEventListener("focus", handleFocus);
      window.removeEventListener("storage", handleStorage);
      window.removeEventListener(RECORDS_UPDATED_EVENT, handleRecordsUpdated);
    };
  }, [ownerPreview, publicSlug, sharedMode]);

  const changeOwnerPublicStatus = useCallback(async (action) => {
    const { access } = getStoredTokens();
    const isDemo = demoEnabled
      && (!access || localStorage.getItem("anime_journal_demo") === "true");
    if (isDemo) {
      const nextStatus = action === "cancel" ? "private" : "pending";
      const settings = readStoredPreviewSettings();
      localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify({
        ...settings,
        publicStatus: nextStatus,
        publicProfile: false,
      }));
      setOwnerPublicStatus(nextStatus);
      return;
    }

    const response = action === "cancel"
      ? await api.patch("public-journal/status/", {})
      : await api.post("public-journal/status/", {});
    setOwnerPublicStatus(response.data?.public_status || (action === "cancel" ? "private" : "pending"));
  }, []);

  const returnOwnerPreviewHome = useCallback(async () => {
    try {
      await authApi.logout();
    } finally {
      clearTokens();
      localStorage.removeItem("anime_journal_demo");
      navigate("/", { replace: true });
    }
  }, [navigate]);

  useEffect(() => {
    if (!bootComplete) return undefined;
    const context = gsap.context(() => {
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        return;
      }

      if (!modeTransition) {
        const entrance = gsap.timeline();
        if (sharedMode) {
          entrance
            .from(".shared-hero-piece", { autoAlpha: 0, y: 34, rotation: 1.1, duration: 0.38, stagger: 0.065, ease: "back.out(1.16)", clearProps: "transform,opacity,visibility", immediateRender: false })
            .from(".stat-piece", { autoAlpha: 0, y: 34, scale: 0.97, duration: 0.34, stagger: 0.045, ease: "back.out(1.2)", clearProps: "transform,opacity,visibility", immediateRender: false }, 0.13)
            .from(".control-piece", { autoAlpha: 0, y: 24, duration: 0.32, stagger: 0.055, ease: "power3.out", clearProps: "transform,opacity,visibility", immediateRender: false }, 0.34)
            .from(".filter-grid .filter-field", { autoAlpha: 0, y: 14, duration: 0.24, stagger: 0.035, ease: "power2.out", clearProps: "transform,opacity,visibility", immediateRender: false }, 0.48)
            .from(".quick-filter-row", { autoAlpha: 0, y: 12, duration: 0.24, ease: "power2.out", clearProps: "transform,opacity,visibility", immediateRender: false }, 0.62);
        } else {
          entrance
            .from(".hero-piece", { y: -36, duration: 0.42, stagger: 0.06, ease: "back.out(1.2)", clearProps: "transform", immediateRender: false })
            .from(".stat-piece", { y: -38, scale: 0.97, duration: 0.36, stagger: 0.045, ease: "back.out(1.24)", clearProps: "transform", immediateRender: false }, 0.12)
            .from(".control-piece", { y: -22, duration: 0.34, stagger: 0.06, ease: "power3.out", clearProps: "transform", immediateRender: false }, 0.34)
            .from(".filter-grid .filter-field", { y: -10, duration: 0.22, stagger: 0.035, ease: "power2.out", clearProps: "transform", immediateRender: false }, 0.48)
            .from(".quick-filter-row", { y: -8, duration: 0.22, ease: "power2.out", clearProps: "transform", immediateRender: false }, 0.62);
        }
      }

      gsap.to(".hero-circle", { y: 13, rotate: 19, duration: 3.2, repeat: -1, yoyo: true, ease: "sine.inOut" });
      gsap.to(".floating-ring", { rotate: 360, duration: 14, repeat: -1, ease: "none" });
      gsap.to(".floating-star", { y: 18, rotate: -7, duration: 3.1, repeat: -1, yoyo: true, ease: "sine.inOut" });
      gsap.to(".filter-lab__flag", { rotate: -2, y: 3, duration: 1.8, repeat: -1, yoyo: true, ease: "sine.inOut" });
      gsap.ticker.wake();
    }, rootRef);
    const fallbackTimer = window.setTimeout(() => {
      const elements = rootRef.current?.querySelectorAll(".hero-piece, .stat-piece, .control-piece, .filter-grid .filter-field, .quick-filter-row");
      if (elements?.length) gsap.set(elements, { clearProps: "transform,opacity,clipPath" });
    }, 1450);
    return () => {
      window.clearTimeout(fallbackTimer);
      context.revert();
    };
  }, [bootComplete, modeTransition, sharedMode]);

  useEffect(() => {
    const onScroll = () => setShowBackTop(window.scrollY > 480);
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return undefined;
    let focusFrame;

    if (selected) {
      root.setAttribute("inert", "");
      root.setAttribute("aria-hidden", "true");
    } else {
      root.removeAttribute("inert");
      root.removeAttribute("aria-hidden");
      if (modalSourceElement?.isConnected) {
        focusFrame = window.requestAnimationFrame(() => {
          modalSourceElement.focus({ preventScroll: true });
          setModalSourceElement(null);
        });
      }
    }

    return () => {
      window.cancelAnimationFrame(focusFrame);
      root.removeAttribute("inert");
      root.removeAttribute("aria-hidden");
    };
  }, [modalSourceElement, selected]);

  const availableTags = useMemo(() => [...new Set(records.flatMap((record) => record.tags || []))].sort(
    (a, b) => a.localeCompare(b, "zh-CN"),
  ), [records]);
  const availableYears = useMemo(() => [...new Set(records.map((record) => record.period?.split("-")[0]).filter(Boolean))].sort(
    (a, b) => Number(b) - Number(a),
  ), [records]);
  const stats = useMemo(() => calculateShowcaseStats(records, remoteStats), [records, remoteStats]);
  const criticalImages = useMemo(() => [
    profile?.avatar || "/assets/avatar.png",
    ...records.slice(0, 4).map((record) => record.poster),
  ], [profile?.avatar, records]);

  const filtered = useMemo(() => {
    const selectedQuick = quickFilters.find((quick) => quick.id === filters.quick);
    const query = filters.search.trim().toLocaleLowerCase("zh-CN");
    const result = records.filter((record) => {
      if (query && !`${record.title} ${record.japaneseTitle}`.toLocaleLowerCase("zh-CN").includes(query)) return false;
      if (filters.tag !== "all" && !record.tags.includes(filters.tag)) return false;
      if (filters.status !== "all" && record.status !== filters.status) return false;
      if (filters.year !== "all" && !record.period.startsWith(filters.year)) return false;
      if (selectedQuick?.tags?.length && !selectedQuick.tags.some((tag) => tag === "OVA" ? isOva(record) : record.tags.includes(tag))) return false;
      return true;
    });
    return result.sort((a, b) => {
      if (filters.sort === "date-asc") return periodSortValue(a.period) - periodSortValue(b.period) || a.title.localeCompare(b.title, "zh-CN");
      if (filters.sort === "score-desc") return (Number(b.score) || -1) - (Number(a.score) || -1);
      if (filters.sort === "score-asc") return (Number(a.score) || Number.MAX_SAFE_INTEGER) - (Number(b.score) || Number.MAX_SAFE_INTEGER);
      return periodSortValue(b.period) - periodSortValue(a.period) || a.title.localeCompare(b.title, "zh-CN");
    });
  }, [filters, records]);

  const visible = pageSize === "all" ? filtered : filtered.slice(0, Number(pageSize));
  const unscoredCount = records.filter((record) => !Number.isFinite(Number(record.score)) || Number(record.score) <= 0).length;

  const changeSort = (sort) => setFilters((current) => ({ ...current, sort }));
  const openRecord = (record, source) => {
    pressBeforeOpen(source, () => {
      setModalSourceElement(source || null);
      setSelected(recordToFeaturedColumn(record));
    });
  };

  return (
    <main className={`showcase-page${sharedMode ? " shared-showcase-page" : ""}${bootComplete ? " is-ready" : " is-booting"}`} ref={rootRef} aria-busy={!bootComplete}>
      {loaderVisible && <AppBootLoader dataReady={dataReady} criticalImages={criticalImages} onComplete={handleBootComplete} />}
      {sharedMode ? (
        <SharedShowcaseHeader
          profile={profile}
          records={records}
          ownerPreview={ownerPreview}
          modeTransition={modeTransition}
          publicStatus={ownerPublicStatus}
          onShareChange={changeOwnerPublicStatus}
          onEdit={() => navigate("/dashboard", { state: { dashboardModeTransition: true } })}
          onReturnHome={returnOwnerPreviewHome}
        />
      ) : <Header profile={profile} sharedMode={sharedMode} siteSettings={siteSettings} />}
      <div className="dot-texture" aria-hidden="true" />
      <div className="floating-shape floating-ring" aria-hidden="true"><Icon name="earth" /></div>
      <div className="floating-shape floating-star" aria-hidden="true"><Icon name="star" /></div>
      <div className="showcase-content">
        <Stats stats={stats} interactive={!sharedMode} />
        <CatalogFilterLab
          filters={filters}
          onFilterChange={(key, value) => setFilters((current) => ({ ...current, [key]: value, quick: key === "quick" ? value : current.quick }))}
          onReset={() => setFilters(DEFAULT_FILTERS)}
          viewMode={view}
          onViewChange={changeCatalogView}
          resultCount={filtered.length}
          tags={availableTags}
          years={availableYears}
          quickFilters={quickFilters}
        />
        <CatalogMeta resultCount={filtered.length} pageSize={pageSize} onPageSizeChange={setPageSize} unscoredCount={unscoredCount} />
        <div className="hazard-line" aria-hidden="true" />
        <div className={`view-content view-content--${view}`} id="anime-results">
          {visible.length ? (
            <AnimeCatalog
              records={visible}
              viewMode={view}
              onOpenDetail={openRecord}
              sort={filters.sort}
              onSortChange={changeSort}
              ready={bootComplete}
            />
          ) : <div className="empty-state"><Icon name={dataError ? "warning" : "search"} /><h2>{dataError || "没有找到匹配的番剧"}</h2><p>{dataError ? "当前页面不会使用演示数据代替服务器结果。" : "换个关键词或恢复默认筛选试试。"}</p></div>}
        </div>
      </div>
      <button className={`back-to-top${showBackTop ? " is-visible" : ""}`} onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })} aria-label="返回页面顶部"><span className="back-to-top__visual"><Icon name="arrow-up" /></span></button>
      {selected && <FeaturedAnimeModal column={selected} onClosed={() => setSelected(null)} />}
    </main>
  );
}
