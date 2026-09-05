import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import gsap from "gsap";
import { FeaturedAnimeModal } from "../components/featured/FeaturedAnimeModal.jsx";
import { FeaturedCard } from "../components/featured/FeaturedCard.jsx";
import { FeaturedFilterLab } from "../components/featured/FeaturedFilterLab.jsx";
import { FeaturedHero } from "../components/featured/FeaturedHero.jsx";
import { Icon } from "../components/Icon.jsx";
import { api } from "../lib/api.js";
import { demoEnabled, demoFeaturedColumns } from "@demo-data";
import { hydrateDemoFeaturedColumns } from "../lib/demoMedia.js";
import {
  ANIMEMO_AVATAR_PATH,
  normalizeBundledPosterPath,
} from "../lib/mediaAssets.js";

const defaults = { q: "", tag: "all", status: "all", year: "all", sort: "date-desc", quick: "all" };

function normalizeFeaturedApiColumn(column) {
  const published = column.published_at || column.updated_at || column.created_at;
  const year = published ? String(new Date(published).getFullYear()) : "";
  const month = published ? String(new Date(published).getMonth() + 1).padStart(2, "0") : "";
  const anime = column.anime || {};
  const resourceIdentity = anime.resourceIdentity || anime.resource_identity || null;
  return {
    id: column.slug || column.id,
    slug: column.slug || String(column.id),
    label: column.label || "FEATURED STORY",
    title: column.title || "未命名专栏",
    japaneseTitle: column.japanese_title || anime.japaneseTitle || anime.japanese_title || "",
    summary: column.summary || "",
    author: column.author_name || "未署名作者",
    authorAvatar: column.author_avatar || ANIMEMO_AVATAR_PATH,
    cover: normalizeBundledPosterPath(anime.poster || anime.poster_url || column.cover),
    period: published ? `${year}-${month}` : (anime.period || anime.airing_period || "未定档"),
    year: year || String(anime.period || anime.airing_period || "").slice(0, 4),
    status: anime.status || anime.watch_status || "completed",
    statusLabel: anime.statusLabel || anime.watch_status_display || "看过",
    score: anime.score ?? anime.personal_score ?? null,
    tags: Array.isArray(column.tags) ? column.tags : (anime.tags || []),
    body: column.body ? column.body.split(/\n{2,}/).filter(Boolean) : [],
    relatedAnime: column.related_anime || [],
    anime: {
      title: anime.title || column.title || "未命名番剧",
      japaneseTitle: anime.japaneseTitle || anime.japanese_title || "",
      poster: normalizeBundledPosterPath(anime.poster || anime.poster_url || column.cover),
      posterOriginal: anime.posterOriginal || anime.poster_original || anime.poster_url || "",
      externalUrl: anime.externalUrl || anime.external_url || anime.baike_url || "",
      externalSource: anime.externalSource || (resourceIdentity?.provider === "bangumi" ? "Bangumi" : "外部资料"),
      resourceIdentity,
      bangumiTitle: anime.bangumiTitle || anime.bangumi_title || "",
      bangumiJapaneseTitle: anime.bangumiJapaneseTitle || anime.bangumi_japanese_title || anime.japanese_title || "",
      period: anime.period || anime.airing_period || "未定档",
      score: anime.score ?? anime.personal_score ?? null,
      studio: anime.studio || "待补充",
      episodeCount: anime.episodeCount || anime.episodes || "待定",
      tags: anime.tags || [],
      summary: anime.summary || anime.description || column.summary || "暂无剧情简介。",
      personalReview: anime.personalReview || anime.review || "暂未记录个人评价。",
    },
    apiBacked: true,
  };
}

function readFilters(params) {
  return Object.fromEntries(Object.keys(defaults).map((key) => [key, params.get(key) || defaults[key]]));
}

export function FeaturedPage() {
  const rootRef = useRef(null);
  const cardsRef = useRef(null);
  const firstCardRevealRef = useRef(true);
  const [searchParams, setSearchParams] = useSearchParams();
  const [columns, setColumns] = useState([]);
  const [syncing, setSyncing] = useState(true);
  const [syncError, setSyncError] = useState("");
  const [selectedColumn, setSelectedColumn] = useState(null);
  const [modalSourceElement, setModalSourceElement] = useState(null);
  const demoMediaCacheRef = useRef(new Map());
  const filters = readFilters(searchParams);

  const openAnimeFile = useCallback((column, sourceElement) => {
    setModalSourceElement(sourceElement);
    setSelectedColumn(column);
  }, []);

  const finishModalClose = useCallback(() => {
    setSelectedColumn(null);
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer;
    const refresh = async () => {
      try {
        const { data } = await api.get("featured/", { timeout: 1800 });
        const result = Array.isArray(data?.results) ? data.results : data;
        if (cancelled) return;
        if (Array.isArray(result) && result.length) setColumns(result.map(normalizeFeaturedApiColumn));
        else if (demoEnabled) setColumns(await hydrateDemoFeaturedColumns(demoFeaturedColumns, { client: api, cache: demoMediaCacheRef.current }));
        else setColumns([]);
        setSyncError("");
      } catch {
        if (!cancelled && demoEnabled) {
          setColumns(await hydrateDemoFeaturedColumns(demoFeaturedColumns, { client: api, cache: demoMediaCacheRef.current }));
        } else if (!cancelled) {
          setColumns([]);
          setSyncError("精选专栏加载失败，请检查服务器连接。");
        }
      } finally {
        if (!cancelled) setSyncing(false);
      }
    };
    refresh();
    timer = window.setInterval(refresh, 60000);
    window.addEventListener("focus", refresh);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      window.removeEventListener("focus", refresh);
    };
  }, []);

  const update = (key, value) => {
    const next = new URLSearchParams(searchParams);
    if (value === defaults[key]) next.delete(key);
    else next.set(key, value);
    setSearchParams(next, { replace: true });
  };
  const reset = () => setSearchParams({}, { replace: true });

  const tags = useMemo(() => [...new Set(columns.flatMap((column) => column.tags || []))].sort((a, b) => a.localeCompare(b, "zh-CN")), [columns]);
  const years = useMemo(() => [...new Set(columns.map((column) => column.year).filter(Boolean))].sort((a, b) => Number(b) - Number(a)), [columns]);
  const visible = useMemo(() => {
    const query = filters.q.trim().toLocaleLowerCase("zh-CN");
    const result = columns.filter((column) => {
      const animeTitle = column.anime?.title || "";
      const animeJapaneseTitle = column.anime?.japaneseTitle || "";
      if (query && !`${animeTitle} ${animeJapaneseTitle} ${column.title} ${column.author}`.toLocaleLowerCase("zh-CN").includes(query)) return false;
      if (filters.tag !== "all" && !column.tags.includes(filters.tag)) return false;
      if (filters.status !== "all" && column.status !== filters.status) return false;
      if (filters.year !== "all" && column.year !== filters.year) return false;
      if (filters.quick === "yuri" && !column.tags.some((tag) => ["萌系", "治愈", "轻百", "真百"].includes(tag))) return false;
      return true;
    });
    return result.sort((a, b) => {
      if (filters.sort === "score-desc") return (b.score ?? -1) - (a.score ?? -1);
      if (filters.sort === "score-asc") return (a.score ?? 99) - (b.score ?? 99);
      if (filters.sort === "title") return a.title.localeCompare(b.title, "zh-CN");
      return b.period.localeCompare(a.period);
    });
  }, [columns, filters.q, filters.quick, filters.sort, filters.status, filters.tag, filters.year]);
  const visibleKey = visible.map((column) => column.slug).join(":");

  useLayoutEffect(() => {
    const context = gsap.context(() => {
      const headerElements = gsap.utils.toArray(".featured-header-reveal", rootRef.current);
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        gsap.set(headerElements, { clearProps: "transform,opacity" });
        return;
      }
      gsap.from(headerElements, {
        y: 46,
        rotation: -1.5,
        opacity: 0,
        duration: 0.36,
        stagger: 0.055,
        delay: 0.22,
        ease: "back.out(1.18)",
        clearProps: "transform,opacity",
        overwrite: "auto",
      });
      gsap.ticker.wake();
    }, rootRef);
    return () => context.revert();
  }, []);

  useLayoutEffect(() => {
    const container = cardsRef.current;
    const cards = container?.querySelectorAll(".featured-card");
    if (!cards?.length) return undefined;

    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      firstCardRevealRef.current = false;
      gsap.set(cards, { clearProps: "transform,opacity" });
      return undefined;
    }

    const context = gsap.context(() => {
      gsap.killTweensOf(cards);
      if (firstCardRevealRef.current) {
        gsap.from(cards, {
          y: 150,
          scale: 0.92,
          rotation: -1.5,
          opacity: 0,
          duration: 0.46,
          stagger: 0.1,
          delay: 0.66,
          ease: "back.out(1.08)",
          overwrite: "auto",
          clearProps: "transform,opacity",
          onComplete: () => { firstCardRevealRef.current = false; },
        });
      } else {
        gsap.fromTo(cards, {
          y: -10,
          scale: 1.02,
          opacity: 0,
        }, {
          y: 0,
          scale: 1,
          opacity: 1,
          duration: 0.22,
          stagger: 0.035,
          ease: "power2.out",
          overwrite: "auto",
          clearProps: "transform,opacity",
        });
      }
      gsap.ticker.wake();
    }, container);

    return () => context.revert();
  }, [visibleKey]);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return undefined;
    let focusFrame;
    if (selectedColumn) {
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
  }, [modalSourceElement, selectedColumn]);

  return (
    <Fragment>
      <main className="featured-page" ref={rootRef}>
        <FeaturedHero />
        <div className="featured-page__texture" aria-hidden="true" />
        <section className="featured-shell">
          <div className="featured-enter"><FeaturedFilterLab filters={filters} tags={tags} years={years} resultCount={visible.length} onChange={update} onReset={reset} /></div>
          <div className="featured-toolbar featured-enter"><span>{syncing ? "正在同步专栏数据..." : syncError || `当前展示 ${visible.length} 篇公开专栏`}</span><Link to="/featured/submit" className="brutal-button coral compact"><Icon name="edit" /> 投稿我的专栏</Link></div>
          {visible.length ? <div ref={cardsRef} className="featured-grid" id="featured-results">{visible.map((column) => <FeaturedCard column={column} onOpen={openAnimeFile} key={column.slug} />)}</div> : <div className="featured-empty"><Icon name="search" /><h2>没有找到匹配的专栏</h2><button className="brutal-button yellow compact" type="button" onClick={reset}><Icon name="reset" /> 恢复全部内容</button></div>}
        </section>
      </main>
      {selectedColumn && (
        <FeaturedAnimeModal
          column={selectedColumn}
          onClosed={finishModalClose}
        />
      )}
    </Fragment>
  );
}
