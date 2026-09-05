import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import gsap from "gsap";
import { Icon } from "../Icon.jsx";
import { readableApiError, api } from "../../lib/api.js";
import { validateTrustedPosterUrl } from "../../lib/posterSources.js";
import { bangumiIdentityFromResult, externalMediaResultFromApi } from "../../lib/externalMedia.js";

const CATALOG_PAGE_SIZE = 10;
const BANGUMI_SPINNER_MIN_MS = 420;

const EMPTY_DRAFT = {
  title: "",
  japaneseTitle: "",
  period: "",
  studio: "",
  episodes: "",
  poster: "",
  posterFile: null,
  posterSource: "trusted_url",
  baikeUrl: "",
  tagsText: "",
  description: "",
  status: "planned",
  score: "",
  review: "",
  externalIdentity: null,
};

function normalizeCatalogRecord(item) {
  return {
    title: item.title || "",
    japaneseTitle: item.japanese_title || item.japaneseTitle || "",
    period: item.airing_period || item.period || "",
    studio: item.studio || "",
    episodes: item.episodes || "",
    poster: item.poster || item.poster_url || "",
    baikeUrl: item.canonical_url || item.canonicalUrl || item.baike_url || item.baikeUrl || "",
    tagsText: Array.isArray(item.tags) ? item.tags.join("，") : "",
    description: item.description || "",
    status: "planned",
    score: "",
    review: "",
    posterFile: null,
    posterSource: "default_url",
    externalIdentity: null,
  };
}

function normalizeBangumi(item) {
  const [year, rawMonth] = String(item.airDate || "").split("-");
  const month = Number.parseInt(rawMonth, 10);
  const yearMonth = year && Number.isFinite(month) ? `${year}-${month}` : "";
  const rawTitle = String(item.title || "").trim();
  return {
    title: rawTitle ? formatAnimeTitle(rawTitle) : "",
    japaneseTitle: item.japaneseTitle || "",
    period: yearMonth || "",
    studio: item.studio || "",
    episodes: item.episodes || "",
    poster: item.posterUrl || "",
    baikeUrl: item.canonicalUrl || item.canonical_url || "",
    tagsText: Array.isArray(item.tags) ? item.tags.slice(0, 8).join(",") : "",
    description: item.summary || "",
    status: "planned",
    score: "",
    review: "",
    posterFile: null,
    posterSource: "default_url",
    externalIdentity: bangumiIdentityFromResult(item),
  };
}

function asCatalogItem(record) {
  return {
    id: record.id,
    title: record.title,
    japanese_title: record.japaneseTitle || record.japanese_title || "",
    airing_period: record.period || record.airing_period || "",
    studio: record.studio || "",
    episodes: record.episodes || "",
    poster: record.poster || record.poster_url || "",
    baike_url: record.baikeUrl || record.baike_url || "",
    tags: record.tags || [],
    description: record.description || "",
  };
}

function formatAnimeTitle(title) {
  const value = String(title || "").trim();
  return value.startsWith("《") && value.endsWith("》") ? value : `《${value}》`;
}

function normalizeAnimeIdentity(value) {
  return String(value || "")
    .normalize("NFKC")
    .toLocaleLowerCase("zh-CN")
    .replace(/[\s《》「」『』【】〔〕〈〉·・:：,，。.!！?？'"“”‘’\-—_]/g, "");
}

function animeIdentityValues(record) {
  const title = normalizeAnimeIdentity(record.title);
  const japaneseTitle = normalizeAnimeIdentity(record.japaneseTitle || record.japanese_title);
  return [title, japaneseTitle].filter(Boolean);
}

function SearchSpinner({ pink = false }) {
  return (
    <span className={`dashboard-add-search-spinner${pink ? " dashboard-add-search-spinner--pink" : ""}`} aria-hidden="true">
      {Array.from({ length: 8 }, (_, index) => <i key={index} />)}
    </span>
  );
}

function ResultMarquee({ children }) {
  const viewportRef = useRef(null);
  const trackRef = useRef(null);

  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    const track = trackRef.current;
    if (!viewport || !track) return undefined;

    let frameId = 0;
    let active = true;
    const measure = () => {
      frameId = 0;
      if (!active) return;
      const distance = Math.max(0, Math.ceil(track.scrollWidth - viewport.clientWidth));
      track.style.setProperty("--magic-result-distance", `${distance}px`);
      track.classList.toggle("is-overflowing", distance > 0);
    };
    const scheduleMeasure = () => {
      if (frameId) cancelAnimationFrame(frameId);
      frameId = requestAnimationFrame(measure);
    };
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(scheduleMeasure);
    observer?.observe(viewport);
    observer?.observe(track);
    document.fonts?.ready.then(scheduleMeasure);
    scheduleMeasure();

    return () => {
      active = false;
      observer?.disconnect();
      if (frameId) cancelAnimationFrame(frameId);
      track.classList.remove("is-overflowing");
      track.style.removeProperty("--magic-result-distance");
    };
  }, [children]);

  return <span className="magic-result-marquee" ref={viewportRef}><span className="magic-result-marquee__track" ref={trackRef}>{children}</span></span>;
}

export function AddAnimeModal({ onClose, onSubmit, isDemo = false, catalogRecords = [], existingRecords = [], trustedPosterHosts = [] }) {
  const rootRef = useRef(null);
  const panelRef = useRef(null);
  const timelineRef = useRef(null);
  const closeRef = useRef(false);
  const titleInputRef = useRef(null);
  const smartPopoverRef = useRef(null);
  const smartTimelineRef = useRef(null);
  const smartFillTimelineRef = useRef(null);
  const smartToastRef = useRef(null);
  const smartToastTimerRef = useRef(0);
  const bangumiSelectionRef = useRef(false);
  const bangumiRequestRef = useRef(0);
  const catalogRequestRef = useRef(0);
  const catalogAddingRef = useRef(false);
  const [tab, setTab] = useState("smart");
  const [draft, setDraft] = useState(EMPTY_DRAFT);
  const [smartQuery, setSmartQuery] = useState("");
  const [smartResults, setSmartResults] = useState([]);
  const [smartSearching, setSmartSearching] = useState(false);
  const [smartError, setSmartError] = useState("");
  const [selectedBangumiId, setSelectedBangumiId] = useState(null);
  const [smartFillPulse, setSmartFillPulse] = useState(0);
  const [smartFillNotice, setSmartFillNotice] = useState("");
  const [catalogQuery, setCatalogQuery] = useState("");
  const [catalogResults, setCatalogResults] = useState([]);
  const [catalogMeta, setCatalogMeta] = useState({ count: 0, page: 1, pages: 1, pageSize: CATALOG_PAGE_SIZE });
  const [catalogPage, setCatalogPage] = useState(1);
  const [catalogSearching, setCatalogSearching] = useState(false);
  const [catalogError, setCatalogError] = useState("");
  const [catalogAddingId, setCatalogAddingId] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState("");
  const existingAnimeKeys = useMemo(() => new Set(existingRecords.flatMap(animeIdentityValues)), [existingRecords]);
  const boundBangumiIds = useMemo(() => new Set(
    existingRecords.flatMap((record) => (record.externalIdentities || [])
      .filter((identity) => identity?.provider === "bangumi" && identity.external_id != null)
      .map((identity) => String(identity.external_id))),
  ), [existingRecords]);
  const isCatalogItemAdded = useCallback((item) => animeIdentityValues(item).some((key) => existingAnimeKeys.has(key)), [existingAnimeKeys]);
  const isBangumiAlreadyBound = useCallback((item) => {
    const externalId = item?.externalId ?? item?.external_id;
    return externalId != null && boundBangumiIds.has(String(externalId));
  }, [boundBangumiIds]);
  const smartDropdownOpen = smartSearching || smartResults.length > 0 || Boolean(smartError);

  const closeWithMotion = useCallback(() => {
    if (closeRef.current) return;
    closeRef.current = true;
    const finish = () => onClose?.();
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches || !panelRef.current) {
      finish();
      return;
    }
    timelineRef.current?.kill();
    timelineRef.current = gsap.timeline({ onComplete: finish })
      .to(panelRef.current, { y: 50, rotation: -2, scale: .82, autoAlpha: 0, duration: .24, ease: "power3.in", overwrite: "auto" })
      .to(rootRef.current, { autoAlpha: 0, duration: .12, ease: "power2.in", overwrite: "auto" }, "-=.08");
  }, [onClose]);

  const searchBangumi = useCallback(async (term) => {
    const normalized = term.trim();
    if (normalized.length < 2) {
      bangumiRequestRef.current += 1;
      setSmartResults([]);
      setSmartSearching(false);
      setSmartError(normalized ? "请至少输入 2 个字符再进行搜索。" : "请先输入需要查找的番剧名称。");
      return;
    }
    const requestId = ++bangumiRequestRef.current;
    const searchStartedAt = performance.now();
    setSmartSearching(true);
    setSmartError("");
    try {
      const response = await api.get("external-media/providers/bangumi/search/", { params: { q: normalized } });
      if (requestId === bangumiRequestRef.current) {
        setSmartResults((response.data?.results || []).map(externalMediaResultFromApi));
      }
    } catch (requestError) {
      if (requestId === bangumiRequestRef.current) {
        setSmartResults([]);
        setSmartError(readableApiError(requestError, "Bangumi 搜索暂时不可用。"));
      }
    } finally {
      const remainingSpinnerTime = BANGUMI_SPINNER_MIN_MS - (performance.now() - searchStartedAt);
      if (remainingSpinnerTime > 0) {
        await new Promise((resolve) => window.setTimeout(resolve, remainingSpinnerTime));
      }
      if (requestId === bangumiRequestRef.current) setSmartSearching(false);
    }
  }, []);

  useEffect(() => {
    const root = rootRef.current;
    const panel = panelRef.current;
    if (!root || !panel) return undefined;
    const context = gsap.context(() => {
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        gsap.set([root, panel], { autoAlpha: 1, clearProps: "transform" });
        return;
      }
      timelineRef.current = gsap.timeline()
        .fromTo(root, { autoAlpha: 0 }, { autoAlpha: 1, duration: .14, ease: "power2.out" })
        .fromTo(panel, { autoAlpha: 0, y: -50, rotation: 2, scale: .7 }, { autoAlpha: 1, y: 0, rotation: 0, scale: 1, duration: .62, ease: "back.out(1.5)", overwrite: "auto", clearProps: "transform,opacity,visibility" }, 0)
        .fromTo(panel.querySelectorAll("[data-add-piece]"), { autoAlpha: 0, y: -16, scale: .96 }, { autoAlpha: 1, y: 0, scale: 1, duration: .3, stagger: .04, ease: "back.out(1.5)", overwrite: "auto", clearProps: "transform,opacity,visibility" }, .18);
    }, root);
    return () => {
      timelineRef.current?.kill();
      context.revert();
    };
  }, []);

  useLayoutEffect(() => {
    const popover = smartPopoverRef.current;
    if (!smartDropdownOpen || !popover || bangumiSelectionRef.current) return undefined;
    smartTimelineRef.current?.kill();
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      gsap.set(popover, { autoAlpha: 1, scaleY: 1, clearProps: "transform" });
      return undefined;
    }
    smartTimelineRef.current = gsap.fromTo(
      popover,
      { autoAlpha: .28, scaleY: .28, transformOrigin: "50% 0%" },
      { autoAlpha: 1, scaleY: 1, duration: .32, ease: "back.out(1.18)", overwrite: "auto", clearProps: "transform,opacity,visibility" },
    );
    return () => {
      if (!bangumiSelectionRef.current) smartTimelineRef.current?.kill();
    };
  }, [smartDropdownOpen]);

  useLayoutEffect(() => {
    const popover = smartPopoverRef.current;
    if (!popover || !smartResults.length || smartSearching || smartError || bangumiSelectionRef.current) return undefined;
    const rows = popover.querySelectorAll(".dashboard-add-search-result");
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      gsap.set(rows, { autoAlpha: 1, clearProps: "transform" });
      return undefined;
    }
    gsap.fromTo(rows, { autoAlpha: 0, y: -5 }, { autoAlpha: 1, y: 0, duration: .18, stagger: .025, ease: "power2.out", overwrite: "auto", clearProps: "transform,opacity,visibility" });
    return undefined;
  }, [smartError, smartResults, smartSearching]);

  useLayoutEffect(() => {
    if (!smartFillPulse || !rootRef.current) return undefined;
    const pieces = [...rootRef.current.querySelectorAll("[data-smart-fill-piece]")]
      .map((piece) => piece.matches("input, textarea") ? piece : piece.querySelector("input:not([type='file']), textarea"))
      .filter(Boolean);
    const poster = rootRef.current.querySelector("[data-smart-fill-poster] img");
    smartFillTimelineRef.current?.kill();
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      smartFillTimelineRef.current = gsap.fromTo([poster, ...pieces].filter(Boolean), { autoAlpha: .7 }, { autoAlpha: 1, duration: .12, stagger: .015, overwrite: "auto", clearProps: "opacity,visibility" });
      return () => smartFillTimelineRef.current?.kill();
    }
    smartFillTimelineRef.current = gsap.timeline();
    if (poster) {
      smartFillTimelineRef.current.fromTo(poster, { autoAlpha: .72, scale: .97 }, { autoAlpha: 1, scale: 1, duration: .18, ease: "back.out(1.15)", overwrite: "auto", clearProps: "transform,opacity,visibility" }, 0);
    }
    smartFillTimelineRef.current
      .to(pieces, { x: 2, y: 2, backgroundColor: "#ffe66d", duration: .055, stagger: .025, ease: "power2.out", overwrite: "auto" }, 0)
      .to(pieces, { x: 0, y: 0, backgroundColor: "#ffffff", duration: .075, stagger: .025, ease: "power2.inOut", clearProps: "transform,backgroundColor" }, .055);
    return () => smartFillTimelineRef.current?.kill();
  }, [smartFillPulse]);

  useLayoutEffect(() => {
    const toast = smartToastRef.current;
    if (!smartFillNotice || !toast) return undefined;
    window.clearTimeout(smartToastTimerRef.current);
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    gsap.fromTo(
      toast,
      { autoAlpha: 0, y: reducedMotion ? 6 : 24, scale: reducedMotion ? 1 : .9 },
      { autoAlpha: 1, y: 0, scale: 1, duration: reducedMotion ? .12 : .34, ease: reducedMotion ? "power1.out" : "back.out(1.35)", overwrite: "auto", clearProps: "transform,opacity,visibility" },
    );
    smartToastTimerRef.current = window.setTimeout(() => setSmartFillNotice(""), 3600);
    return () => window.clearTimeout(smartToastTimerRef.current);
  }, [smartFillNotice]);

  useEffect(() => () => {
    smartTimelineRef.current?.kill();
    smartFillTimelineRef.current?.kill();
    window.clearTimeout(smartToastTimerRef.current);
  }, []);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    const previousActive = document.activeElement;
    document.body.style.overflow = "hidden";
    const focusTimer = window.setTimeout(() => titleInputRef.current?.focus(), 80);
    const handleKeyDown = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeWithMotion();
        return;
      }
      if (event.key !== "Tab" || !panelRef.current) return;
      const focusable = [...panelRef.current.querySelectorAll('button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])')];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.clearTimeout(focusTimer);
      document.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = previousOverflow;
      previousActive?.focus?.();
    };
  }, [closeWithMotion]);

  useEffect(() => {
    if (tab !== "smart" || smartQuery.trim().length < 2) {
      bangumiRequestRef.current += 1;
      setSmartResults([]);
      setSmartSearching(false);
      setSmartError("");
      return undefined;
    }
    const timer = window.setTimeout(() => searchBangumi(smartQuery), 420);
    return () => window.clearTimeout(timer);
  }, [searchBangumi, smartQuery, tab]);

  useEffect(() => {
    if (tab !== "catalog") return undefined;
    const requestId = ++catalogRequestRef.current;
    const timer = window.setTimeout(async () => {
      setCatalogSearching(true);
      setCatalogError("");
      try {
        if (isDemo) {
          const needle = catalogQuery.trim().toLowerCase();
          const matches = catalogRecords.map(asCatalogItem).filter((item) => {
            if (!needle) return true;
            return `${item.title} ${item.japanese_title} ${item.studio} ${item.airing_period}`.toLowerCase().includes(needle);
          });
          const pages = Math.max(1, Math.ceil(matches.length / CATALOG_PAGE_SIZE));
          const page = Math.min(catalogPage, pages);
          const start = (page - 1) * CATALOG_PAGE_SIZE;
          if (requestId === catalogRequestRef.current) {
            setCatalogResults(matches.slice(start, start + CATALOG_PAGE_SIZE));
            setCatalogMeta({ count: matches.length, page, pages, pageSize: CATALOG_PAGE_SIZE });
          }
          return;
        }
        const response = await api.get("catalog/public-search/", {
          params: { q: catalogQuery.trim(), page: catalogPage, page_size: CATALOG_PAGE_SIZE },
        });
        if (requestId === catalogRequestRef.current) {
          setCatalogResults(response.data?.results || []);
          setCatalogMeta({
            count: response.data?.count || 0,
            page: response.data?.page || 1,
            pages: response.data?.pages || 1,
            pageSize: response.data?.page_size || CATALOG_PAGE_SIZE,
          });
        }
      } catch (requestError) {
        if (requestId === catalogRequestRef.current) {
          setCatalogResults([]);
          setCatalogError(readableApiError(requestError, "公共番剧池暂时不可用。"));
        }
      } finally {
        if (requestId === catalogRequestRef.current) setCatalogSearching(false);
      }
    }, catalogQuery ? 260 : 40);
    return () => window.clearTimeout(timer);
  }, [catalogPage, catalogQuery, catalogRecords, isDemo, tab]);

  const update = (key, value) => setDraft((current) => ({ ...current, [key]: value }));
  const selectFile = (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    if (!/^image\/(png|jpe?g|webp)$/i.test(file.type) || file.size > 5 * 1024 * 1024) {
      setError("请选择 5MB 以内的 JPG、PNG 或 WebP 图片。");
      return;
    }
    setError("");
    setDraft((current) => ({ ...current, posterFile: file, posterSource: "upload" }));
    const reader = new FileReader();
    reader.onload = () => setPreview(String(reader.result));
    reader.readAsDataURL(file);
  };
  const chooseBangumi = async (item, row) => {
    if (isBangumiAlreadyBound(item)) {
      setSmartError("这个 Bangumi 条目已经绑定到你的另一部手账，请选择其他条目。");
      return;
    }
    if (bangumiSelectionRef.current) return;
    bangumiSelectionRef.current = true;
    setSelectedBangumiId(item.externalId);
    const completeSelection = (selectedItem, notice) => {
      const next = normalizeBangumi(selectedItem);
      setDraft(next);
      setPreview(next.poster);
      setSmartResults([]);
      setSmartQuery("");
      setSmartError("");
      setSelectedBangumiId(null);
      setSmartFillPulse((value) => value + 1);
      setSmartFillNotice(notice);
      bangumiSelectionRef.current = false;
    };
    const popover = smartPopoverRef.current;
    const collapse = new Promise((resolve) => {
      if (!popover || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        resolve();
        return;
      }
      smartTimelineRef.current?.kill();
      smartTimelineRef.current = gsap.timeline({ onComplete: resolve })
        .to(row, { x: 3, y: 3, duration: .07, ease: "power2.out", overwrite: "auto" })
        .to(row, { x: 0, y: 0, duration: .06, ease: "power2.inOut", overwrite: "auto" })
        .to(popover, { autoAlpha: 0, scaleY: 0, transformOrigin: "50% 0%", duration: .25, ease: "power3.in", overwrite: "auto" }, .07);
    });
    let selectedItem = item;
    let notice = "番剧资料已自动填充，请检查后再创建。";
    try {
      const [response] = await Promise.all([
        item.externalId
          ? api.get(`external-media/providers/bangumi/subjects/${encodeURIComponent(item.externalId)}/`)
          : Promise.resolve({ data: null }),
        collapse,
      ]);
      selectedItem = response.data ? externalMediaResultFromApi(response.data) : item;
    } catch (requestError) {
      await collapse;
      notice = readableApiError(requestError, "Bangumi 详情暂时无法读取，已使用搜索结果填充。");
    }
    completeSelection(selectedItem, notice);
  };
  const closeSmartSearch = () => {
    if (bangumiSelectionRef.current) return;
    bangumiRequestRef.current += 1;
    setSmartSearching(false);
    setSmartResults([]);
    setSmartError("");
    titleInputRef.current?.focus();
  };
  const addCatalogItem = async (item) => {
    if (catalogAddingRef.current || isCatalogItemAdded(item)) return;
    catalogAddingRef.current = true;
    setCatalogAddingId(item.id);
    setCatalogError("");
    try {
      await onSubmit?.(normalizeCatalogRecord(item));
      closeWithMotion();
    } catch (requestError) {
      catalogAddingRef.current = false;
      setCatalogError(readableApiError(requestError, "加入手账失败，请稍后重试。"));
      setCatalogAddingId(null);
    }
  };
  const submit = async (event) => {
    event.preventDefault();
    if (!draft.title.trim() || !draft.period.trim()) {
      setError("请先填写番剧中文名和放送季度。");
      return;
    }
    const posterError = draft.posterFile ? "" : validateTrustedPosterUrl(draft.poster, trustedPosterHosts);
    if (posterError) {
      setError(posterError);
      return;
    }
    setLoading(true);
    setError("");
    try {
      await onSubmit?.({ ...draft, title: draft.title.trim(), period: draft.period.trim() });
      closeWithMotion();
    } catch (requestError) {
      setError(readableApiError(requestError, "创建番剧失败，请稍后重试。"));
      setLoading(false);
    }
  };
  const changeTab = (nextTab) => {
    if (nextTab === tab) return;
    setTab(nextTab);
    setError("");
    setSmartError("");
    setCatalogError("");
    if (nextTab === "catalog") setCatalogPage(1);
  };

  return (
    <div className="dashboard-add-modal" ref={rootRef} role="dialog" aria-modal="true" aria-labelledby="dashboard-add-title">
      <button className="dashboard-add-modal__backdrop" type="button" aria-label="关闭添加番剧" onClick={closeWithMotion} />
      <section className="dashboard-add-modal__panel" ref={panelRef}>
        <header data-add-piece>
          <div><span className="dashboard-modal-kicker">ADD ANIME</span><h2 id="dashboard-add-title">添加番剧</h2><p>亲手创建一部全新番剧，或者快速添加。</p></div>
          <button className="dashboard-square-button" type="button" onClick={closeWithMotion} aria-label="关闭"><Icon name="close" /></button>
        </header>
        <div className="dashboard-add-modal__tabs" role="tablist" aria-label="添加方式" data-add-piece>
          <button type="button" className={tab === "smart" ? "is-active" : ""} onClick={() => changeTab("smart")} role="tab" aria-selected={tab === "smart"}><Icon name="wand" /> 智能录入新番</button>
          <button type="button" className={tab === "catalog" ? "is-active" : ""} onClick={() => changeTab("catalog")} role="tab" aria-selected={tab === "catalog"}><Icon name="search" /> 搜索番剧库</button>
        </div>

        <div className="dashboard-add-modal__content">
          {tab === "smart" ? <form className="dashboard-add-form" onSubmit={submit}>
            <aside className="dashboard-add-form__poster" data-add-piece>
              <label className="dashboard-add-poster-preview" data-smart-fill-poster>{preview || draft.poster ? <img src={preview || draft.poster} alt="海报预览" /> : <span className="dashboard-add-poster-copy"><Icon name="file-upload" /><strong>选择本地海报</strong><small>JPG / PNG / WebP · 最大 5MB</small></span>}<input type="file" accept="image/png,image/jpeg,image/webp" onChange={selectFile} hidden /></label>
              <label data-smart-fill-piece><span>或填写受信任图片 URL</span><input value={draft.poster} onChange={(event) => { setDraft((current) => ({ ...current, poster: event.target.value, posterFile: null, posterSource: "trusted_url" })); setPreview(""); }} placeholder="https://..." /><small className="dashboard-add-poster-hint">仅支持管理员白名单中的 HTTPS 图片域名</small></label>
            </aside>
            <div className="dashboard-add-form__fields" data-add-piece>
              <div className="dashboard-add-field-grid">
                <div className="dashboard-add-title-cell">
                  <label className="dashboard-add-smart-field" data-smart-fill-piece><span>番剧中文名 <b>*</b></span><input ref={titleInputRef} value={draft.title} onChange={(event) => { update("title", event.target.value); setSmartQuery(event.target.value); }} placeholder="输入名称，停顿后智能搜索" required /></label>
                    {smartDropdownOpen && <div className="dashboard-add-search-popover" id="dashboard-smart-search-results" role="region" aria-label="Bangumi 搜索结果" ref={smartPopoverRef}>
                      <div className="dashboard-add-search-popover__head"><span><Icon name="satellite-dish" /> MAGIC METADATA SEARCH</span><button className="dashboard-add-search-popover__close" type="button" onClick={closeSmartSearch} aria-label="关闭智能搜索提示"><Icon name="close" /></button></div>
                      {smartSearching && <p className="is-status"><SearchSpinner pink /> <span>正在搜索 Bangumi…</span></p>}
                      {smartError && <p className="is-error"><Icon name="warning" /> <span>{smartError}</span></p>}
                      {!smartSearching && !smartError && smartResults.map((item) => {
                        const alreadyBound = isBangumiAlreadyBound(item);
                        return <button className={`dashboard-add-search-result${selectedBangumiId === item.externalId ? " is-selecting" : ""}`} type="button" key={item.externalId} onClick={(event) => chooseBangumi(item, event.currentTarget)} disabled={selectedBangumiId !== null || alreadyBound} aria-label={alreadyBound ? `${item.title} 已绑定到其他手账` : `选择 ${item.title}`}><span className="dashboard-add-search-result__poster"><img src={item.thumbnailUrl || item.posterUrl || "/assets/posters/poster-01.webp"} alt={`${item.title} 海报`} decoding="async" onError={(event) => { event.currentTarget.src = "/assets/posters/poster-01.webp"; }} /></span><span className="dashboard-add-search-result__copy"><strong><ResultMarquee>{item.title}</ResultMarquee></strong><small><ResultMarquee>{alreadyBound ? "已绑定到其他手账" : item.japaneseTitle || "未填写日文名"}</ResultMarquee></small></span><Icon className="dashboard-add-search-result__arrow" name="arrow-right" /></button>;
                      })}
                    </div>}
                  <button className={`dashboard-add-search-trigger${smartSearching ? " is-searching" : ""}`} type="button" onClick={() => searchBangumi(draft.title)} disabled={smartSearching} aria-label={smartSearching ? "正在从 Bangumi 搜索番剧资料" : "从 Bangumi 自动搜索番剧资料"} aria-expanded={smartDropdownOpen} aria-controls="dashboard-smart-search-results">{smartSearching ? <SearchSpinner /> : <Icon name="search" />}</button>
                </div>
                <label data-smart-fill-piece><span>番剧日文名</span><input value={draft.japaneseTitle} onChange={(event) => update("japaneseTitle", event.target.value)} /></label>
                <label data-smart-fill-piece><span>放送季度 <b>*</b></span><input value={draft.period} onChange={(event) => update("period", event.target.value)} placeholder="例如 2026-4" required /></label>
                <label data-smart-fill-piece><span>制作公司</span><input value={draft.studio} onChange={(event) => update("studio", event.target.value)} /></label>
                <label data-smart-fill-piece><span>话数情况</span><input value={draft.episodes} onChange={(event) => update("episodes", event.target.value)} placeholder="例如 12、12+1" /></label>
                <label data-smart-fill-piece><span>外部资料 URL</span><input value={draft.baikeUrl} onChange={(event) => update("baikeUrl", event.target.value)} placeholder="https://bgm.tv/subject/..." /></label>
              </div>
              <label data-smart-fill-piece><span>公共标签</span><input value={draft.tagsText} onChange={(event) => update("tagsText", event.target.value)} placeholder="使用逗号分隔，例如：日常，治愈，原创" /></label>
              <div className="dashboard-add-bottom-grid">
                <label data-smart-fill-piece><span>剧情简介</span><textarea value={draft.description} onChange={(event) => update("description", event.target.value)} rows="6" /></label>
                <fieldset className="dashboard-add-private"><h3><Icon name="bookmark" /> 初始私人手账信息</h3><label><span>观看状态</span><select value={draft.status} onChange={(event) => update("status", event.target.value)}><option value="planned">想看</option><option value="watching">在看</option><option value="completed">看过</option><option value="on_hold">搁置</option><option value="dropped">弃番</option></select></label><label><span>主观评分</span><input type="number" min="0" max="10" step="0.1" value={draft.score} onChange={(event) => update("score", event.target.value)} placeholder="0.0 - 10.0" /></label><label className="wide"><span>个人评价</span><textarea value={draft.review} onChange={(event) => update("review", event.target.value)} rows="3" /></label></fieldset>
              </div>
            </div>
            {error && <p className="dashboard-add-error" role="alert">{error}</p>}
            <footer data-add-piece><button type="button" className="brutal-button white compact" onClick={closeWithMotion}>取消</button><button className="brutal-button yellow compact" type="submit" disabled={loading}>{loading ? "正在创建..." : "创建并加入手账"} <Icon name="arrow-right" /></button></footer>
          </form> : <div className="dashboard-catalog-search" data-add-piece>
            <label className="dashboard-catalog-search__input"><Icon name="search" /><span>搜索中文名、日文名、制作公司或放送季度</span><input value={catalogQuery} onChange={(event) => { setCatalogQuery(event.target.value); setCatalogPage(1); }} placeholder="例如：摇曳百合、京都动画、2022-10...（不能保证搜索到你想要的番剧）" /></label>
            <div className="dashboard-catalog-search__heading"><span><Icon name="history" /> 公共番剧池 · 最新收录</span><small>共 {catalogMeta.count} 条 · {catalogMeta.pageSize} 条/页</small></div>
            {catalogSearching && <p className="dashboard-add-search-status">正在检索番剧库……</p>}
            {catalogError && <p className="dashboard-add-error" role="alert">{catalogError}</p>}
            {!catalogSearching && !catalogError && !catalogResults.length && <p className="dashboard-add-search-status">没有找到匹配记录。</p>}
            <div className="dashboard-catalog-results">{catalogResults.map((item) => {
              const isAdded = isCatalogItemAdded(item);
              const isAdding = catalogAddingId === item.id;
              return <article key={item.id}><img src={item.poster || item.poster_url || "/assets/posters/poster-01.webp"} alt={`${item.title} 海报`} /><div><strong>{formatAnimeTitle(item.title)}</strong><small>{item.japanese_title || "未填写日文名"}</small><em>{item.airing_period || "未定档"}</em></div><button className={isAdded ? "is-added" : ""} type="button" onClick={() => addCatalogItem(item)} disabled={isAdded || catalogAddingId !== null}><Icon name={isAdded ? "circle-check" : "plus"} /> {isAdded ? "已加入" : isAdding ? "正在加入" : "加入手账"}</button></article>;
            })}</div>
            {catalogMeta.count > 0 && <nav className="dashboard-catalog-pagination" aria-label="番剧库分页"><button className="is-edge is-first" type="button" onClick={() => setCatalogPage(1)} disabled={catalogMeta.page <= 1} aria-label="转到第一页">«</button><button className="is-prev" type="button" onClick={() => setCatalogPage((page) => Math.max(1, page - 1))} disabled={catalogMeta.page <= 1}><Icon name="arrow-left" /> 上一页</button><span>第 {catalogMeta.page} / {catalogMeta.pages} 页</span><button className="is-next" type="button" onClick={() => setCatalogPage((page) => Math.min(catalogMeta.pages, page + 1))} disabled={catalogMeta.page >= catalogMeta.pages}>下一页 <Icon name="arrow-right" /></button><button className="is-edge is-last" type="button" onClick={() => setCatalogPage(catalogMeta.pages)} disabled={catalogMeta.page >= catalogMeta.pages} aria-label="转到最后一页">»</button></nav>}
          </div>}
        </div>
      </section>
      {smartFillNotice && <div className="dashboard-smart-fill-toast" role="status" ref={smartToastRef}><span className="dashboard-smart-fill-toast__icon" aria-hidden="true"><Icon name="circle-check" /></span><span className="dashboard-smart-fill-toast__message">{smartFillNotice}</span><button className="dashboard-smart-fill-toast__close" type="button" onClick={() => setSmartFillNotice("")} aria-label="关闭自动填充提示"><Icon name="close" /></button></div>}
    </div>
  );
}
