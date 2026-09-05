import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import gsap from "gsap";
import { Icon } from "../components/Icon.jsx";
import { UniverseOwnerCard } from "../components/UniverseOwnerCard.jsx";
import { UniverseHeroArt } from "../components/UniverseHeroArt.jsx";
import { useSiteSettings } from "../context/SiteSettingsContext.jsx";
import { usePageColorTransition } from "../components/PageColorTransition.jsx";
import { api, getStoredTokens } from "../lib/api.js";
import { demoEnabled, demoUniverseOwners } from "@demo-data";
import { hydrateDemoUniverseOwners } from "../lib/demoMedia.js";
import { ANIMEMO_AVATAR_PATH, ANIMEMO_POSTER_FALLBACK_PATH } from "../lib/mediaAssets.js";

async function loadDemoUniverseOwners(cache) {
  return demoEnabled
    ? hydrateDemoUniverseOwners(demoUniverseOwners, { client: api, cache })
    : [];
}

function apiEntryToPick(entry) {
  return {
    id: entry.id,
    title: entry.title,
    poster: entry.poster || entry.poster_url || ANIMEMO_POSTER_FALLBACK_PATH,
    score: entry.personal_score === null ? null : Number(entry.personal_score),
  };
}

function normalizeOwner(owner, index) {
  return {
    id: owner.public_slug || index,
    nickname: owner.nickname || owner.username || "未命名手账",
    subtitle: owner.subtitle || "把每一次与动画相遇认真收藏。",
    avatar: owner.avatar_url || ANIMEMO_AVATAR_PATH,
    public_slug: owner.public_slug || "",
    stats: owner.stats || {},
    top_picks: (owner.top_picks || []).map(apiEntryToPick),
  };
}

export function ColumnSubmitPage() {
  const navigate = useNavigate();
  const [form, setForm] = useState({ title: "", summary: "", body: "" });
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(false);
  const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));

  const submit = async (event) => {
    event.preventDefault();
    setLoading(true);
    try {
      const { access } = getStoredTokens();
      const demoMode = demoEnabled && localStorage.getItem("anime_journal_demo") === "true";
      if (access && !demoMode) {
        const { data } = await api.post("columns/", form);
        await api.post(`columns/${data.id}/submit/`);
      } else if (demoEnabled) {
        const drafts = JSON.parse(localStorage.getItem("anime_journal_column_drafts") || "[]");
        localStorage.setItem("anime_journal_column_drafts", JSON.stringify([{ ...form, id: Date.now(), status: "pending" }, ...drafts]));
      } else {
        setMessage("请先登录后再提交专栏。");
        navigate("/login", { state: { from: "/featured/submit" } });
        return;
      }
      setMessage("投稿已进入审核队列，我们会在手账内同步状态。");
      setForm({ title: "", summary: "", body: "" });
    } catch {
      setMessage("提交失败，请登录后重试。");
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="community-page column-submit-page">
      <div className="dot-texture" aria-hidden="true" />
      <header className="column-submit-header">
        <Link to="/featured" className="back-showcase"><Icon name="arrow-left" /> 返回精选专栏</Link>
        <span className="micro-label">PITCH YOUR STORY / 专栏投稿</span>
        <h1>把你的动画观点<br /><b>认真写下来</b></h1>
      </header>
      <form className="column-submit-form" onSubmit={submit}>
        <label><span>专栏标题</span><input value={form.title} onChange={(event) => update("title", event.target.value)} minLength="5" maxLength="200" required placeholder="例：百合动画入门，从轻松日常到浓烈情感" /></label>
        <label><span>一句话摘要</span><textarea value={form.summary} onChange={(event) => update("summary", event.target.value)} maxLength="400" required placeholder="告诉编辑与读者，这篇专栏为什么值得读。" /></label>
        <label><span>正文</span><textarea className="column-body-input" value={form.body} onChange={(event) => update("body", event.target.value)} minLength="100" required placeholder="从你的手账、评分和观看感受出发开始写作……" /></label>
        {message && <p className="form-message success">{message}</p>}
        <footer><span>提交后进入待审核状态，发布或驳回都会保留在你的账号中。</span><button className="brutal-button coral" disabled={loading}><Icon name="award" /> {loading ? "正在提交…" : "提交审核"}</button></footer>
      </form>
    </main>
  );
}

export function UniversePage() {
  const { settings: siteSettings } = useSiteSettings();
  const rootRef = useRef(null);
  const { isTransitioning, navigateWithTransition } = usePageColorTransition();
  const [owners, setOwners] = useState([]);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const demoMediaCacheRef = useRef(new Map());

  useEffect(() => {
    let cancelled = false;
    let refreshTimer;
    const refresh = async () => {
      try {
        const { data } = await api.get("showcases/");
        if (cancelled) return;
        const results = Array.isArray(data.results) ? data.results.map(normalizeOwner) : [];
        if (results.length || !demoEnabled) setOwners(results);
        else setOwners(await loadDemoUniverseOwners(demoMediaCacheRef.current));
        setLoadError("");
      } catch {
        if (!cancelled) {
          setOwners(await loadDemoUniverseOwners(demoMediaCacheRef.current));
          setLoadError(demoEnabled ? "" : "公开手账信号加载失败，请检查服务器连接。");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    refresh();
    refreshTimer = window.setInterval(refresh, 60000);
    window.addEventListener("focus", refresh);
    return () => {
      cancelled = true;
      window.clearInterval(refreshTimer);
      window.removeEventListener("focus", refresh);
    };
  }, []);

  useLayoutEffect(() => {
    const introElements = gsap.utils.toArray(".universe-header-reveal", rootRef.current);
    const signalStage = rootRef.current?.querySelector(".universe-signal-stage");
    const crashLeft = rootRef.current?.querySelector(".universe-crash--left");
    const crashRight = rootRef.current?.querySelector(".universe-crash--right");
    const crashChip = rootRef.current?.querySelector(".universe-crash--chip");
    const heroArt = rootRef.current?.querySelector(".universe-hero__art");
    const context = gsap.context(() => {
      gsap.set([introElements, signalStage, crashLeft, crashRight, crashChip, heroArt], { autoAlpha: 1 });
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

      const intro = gsap.timeline({ defaults: { overwrite: "auto" } });
      intro
        .fromTo(crashLeft, {
          clipPath: "polygon(0 0, 2% 0, 2% 100%, 0 100%)",
        }, {
          clipPath: "polygon(0 0, 100% 0, 100% 100%, 0 100%)",
          duration: 0.24,
          ease: "expo.out",
        }, 0)
        .fromTo(crashRight, {
          xPercent: 112,
        }, {
          xPercent: 0,
          duration: 0.3,
          ease: "power4.out",
          clearProps: "transform",
        }, 0.02)
        .fromTo(crashChip, {
          autoAlpha: 0,
          x: 72,
          y: -34,
          rotation: 24,
        }, {
          autoAlpha: 1,
          x: 0,
          y: 0,
          rotation: 0,
          duration: 0.24,
          ease: "back.out(1.25)",
          clearProps: "transform,opacity,visibility",
        }, 0.08)
        .fromTo(heroArt, {
          autoAlpha: 0,
          x: 84,
          y: 22,
          scale: 0.96,
          rotation: 2,
        }, {
          autoAlpha: 1,
          x: 0,
          y: 0,
          scale: 1,
          rotation: 0,
          duration: 0.34,
          ease: "back.out(1.12)",
          clearProps: "transform,opacity,visibility",
        }, 0.08)
        .fromTo(introElements, {
          autoAlpha: 0,
          y: 36,
          rotation: 1.2,
        }, {
          autoAlpha: 1,
          y: 0,
          rotation: 0,
          duration: 0.34,
          stagger: 0.055,
          ease: "back.out(1.18)",
          clearProps: "transform,opacity,visibility",
        }, 0.13)
        .fromTo(signalStage, {
          autoAlpha: 0,
          y: 28,
          rotation: 0.5,
        }, {
          autoAlpha: 1,
          y: 0,
          rotation: 0,
          duration: 0.3,
          ease: "power3.out",
          clearProps: "transform,opacity,visibility",
        }, 0.38);
      gsap.to(".universe-art-spark", { rotate: 360, duration: 11, repeat: -1, ease: "none" });
      gsap.fromTo(
        ".universe-deco--ring",
        { y: 0, rotate: 0 },
        { y: 18, rotate: 12, duration: 3.7, repeat: -1, yoyo: true, ease: "sine.inOut" },
      );
      gsap.fromTo(
        ".universe-deco--tile",
        { x: 0, rotate: 15 },
        { x: 12, rotate: -8, duration: 3.1, repeat: -1, yoyo: true, ease: "sine.inOut" },
      );
      gsap.ticker.wake();
    }, rootRef);
    const fallbackTimer = window.setTimeout(() => {
      gsap.set([introElements, signalStage, crashLeft, crashRight, crashChip, heroArt], { clearProps: "transform,opacity,visibility,clipPath" });
    }, 1100);
    return () => {
      window.clearTimeout(fallbackTimer);
      context.revert();
      gsap.set([introElements, signalStage, crashLeft, crashRight, crashChip, heroArt], { clearProps: "transform,opacity,visibility,clipPath" });
    };
  }, []);

  const visibleOwners = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase("zh-CN");
    if (!normalized) return owners;
    return owners.filter((owner) => `${owner.nickname} ${owner.subtitle}`.toLocaleLowerCase("zh-CN").includes(normalized));
  }, [owners, query]);

  const openOwner = (owner) => {
    if (isTransitioning) return;
    const slug = owner.public_slug || owner.id;
    void navigateWithTransition(`/shared/${encodeURIComponent(slug)}`);
  };

  return (
    <main className="community-page universe-page" ref={rootRef}>
      <div className="dot-texture" aria-hidden="true" />
      <div className="universe-deco universe-deco--tile" aria-hidden="true" />
      <div className="universe-deco universe-deco--ring" aria-hidden="true" />
      <header className="universe-hero">
        <div className="universe-crash universe-crash--left" aria-hidden="true" />
        <div className="universe-crash universe-crash--right" aria-hidden="true" />
        <div className="universe-crash universe-crash--chip" aria-hidden="true" />
        <div className="universe-hero__inner">
          <div className="universe-hero__copy">
            <Link to="/" className="universe-back universe-header-reveal"><Icon name="arrow-left" /> 返回展示主界面</Link>
            <span className="micro-label universe-header-reveal">PUBLIC JOURNAL / LIVE UNIVERSE</span>
            <h1 className="universe-header-reveal"><span>番剧共创</span> <b>宇宙</b></h1>
            <p className="universe-header-reveal">{siteSettings.universe_description}</p>
            <label className="universe-search universe-header-reveal">
              <span><Icon className="universe-search__signal-icon" name="satellite-dish" /> 搜索昵称或账号</span>
              <div className="universe-search__field">
                <span className="universe-search__icon" aria-hidden="true"><Icon name="search" /></span>
                <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索用户昵称，例如：兔子" />
              </div>
            </label>
          </div>
          <UniverseHeroArt socialHandle={siteSettings.social_handle} />
        </div>
      </header>

      <section className="universe-signals" aria-labelledby="signals-title">
        <header className="universe-signals__header universe-signal-stage">
          <div><span className="micro-label">LIVE SIGNALS</span><h2 id="signals-title">公开手账信号</h2></div>
          <span className="universe-owner-count"><Icon name="users-viewfinder" /> {visibleOwners.length} 位同好</span>
        </header>
        <div className="journal-owner-grid" aria-live="polite">
          {visibleOwners.map((owner) => <UniverseOwnerCard owner={owner} onOpen={openOwner} key={owner.id} />)}
        </div>
        {!visibleOwners.length && <div className="universe-empty"><Icon name={loadError ? "warning" : "search"} /><strong>{loadError || "没有捕捉到匹配信号"}</strong><span>{loadError ? "当前页面不会显示演示账号。" : "换一个昵称再试试。"}</span></div>}
        {loading && <span className="universe-refreshing">正在同步公开手账信号...</span>}
      </section>
    </main>
  );
}
