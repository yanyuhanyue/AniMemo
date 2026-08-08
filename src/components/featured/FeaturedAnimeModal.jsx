import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import gsap from "gsap";
import { Icon } from "../Icon.jsx";
import { TagChip } from "../TagChip.jsx";
import { FeaturedModalScore } from "./FeaturedModalScore.jsx";
import { useModalViewportSize } from "./useModalViewportSize.js";
import { WatchHistoryList } from "../WatchHistoryList.jsx";

function displayPeriod(period = "") {
  if (!period || period === "未定档") return "未定档";
  const [year, rawMonth] = String(period).split("-");
  const quarter = /^Q([1-4])$/i.exec(rawMonth || "");
  const numericMonth = quarter ? (Number(quarter[1]) - 1) * 3 + 1 : Number(rawMonth);
  return numericMonth ? `${year}年${numericMonth}月` : `${year}年`;
}

function renderParagraphs(value, fallback) {
  const paragraphs = (Array.isArray(value) ? value : String(value || "").split(/\n{2,}/))
    .map((paragraph) => paragraph.trim())
    .filter(Boolean);
  if (!paragraphs.length) return <p>{fallback}</p>;
  return paragraphs.map((paragraph, index) => <p key={`${index}-${paragraph.slice(0, 18)}`}>{paragraph}</p>);
}

function StudioMarquee({ value }) {
  const viewportRef = useRef(null);
  const trackRef = useRef(null);

  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    const track = trackRef.current;
    if (!viewport || !track) return undefined;

    let active = true;
    let frameId = 0;
    const measure = () => {
      frameId = 0;
      if (!active) return;
      const distance = Math.max(0, Math.round(track.scrollWidth - viewport.clientWidth));
      track.style.setProperty("--studio-marquee-distance", `${distance}px`);
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
      track.style.removeProperty("--studio-marquee-distance");
    };
  }, [value]);

  return (
    <strong className="featured-anime-modal__fact-value featured-anime-modal__fact-value--studio" ref={viewportRef} title={value}>
      <span className="featured-anime-modal__studio-track" ref={trackRef}>{value}</span>
    </strong>
  );
}

export function FeaturedAnimeModal({ column, onClosed }) {
  const rootRef = useRef(null);
  const backdropRef = useRef(null);
  const panelRef = useRef(null);
  const closeRef = useRef(null);
  const timelineRef = useRef(null);
  const fallbackTimerRef = useRef(null);
  const closingRef = useRef(false);
  const closedRef = useRef(false);
  const [phase, setPhase] = useState("opening");
  const [activeTab, setActiveTab] = useState("summary");
  const titleId = useId();
  const tabPanelId = useId();
  const anime = column?.anime;
  const modalSize = useModalViewportSize();
  const modalStyle = useMemo(() => {
    const scaled = (ratio, minimum, maximum) => Math.round(
      Math.min(maximum, Math.max(minimum, modalSize.height * ratio)),
    );
    const posterHeight = Math.round(Math.min(
      modalSize.height * 0.549,
      Math.max(220, modalSize.height - 414),
    ));
    const posterWidth = Math.round(Math.min(
      300,
      Math.max(180, modalSize.width * 0.268),
    ));

    return {
      "--featured-modal-width": `${modalSize.width}px`,
      "--featured-modal-height": `${modalSize.height}px`,
      "--featured-poster-height": `${posterHeight}px`,
      "--featured-poster-width": `${posterWidth}px`,
      "--featured-header-height": `${scaled(0.132, 96, 142)}px`,
      "--featured-fact-height": `${scaled(0.0875, 76, 94)}px`,
      "--featured-fact-label-size": `${scaled(0.013, 12, 14)}px`,
      "--featured-fact-value-size": `${scaled(0.0224, 20, 24)}px`,
      "--featured-score-size": `${scaled(0.0233, 21, 25)}px`,
      "--featured-pending-size": `${scaled(0.0205, 19, 22)}px`,
      "--featured-footer-height": `${scaled(0.0596, 50, 64)}px`,
    };
  }, [modalSize.height, modalSize.width]);

  const posterUrl = useMemo(() => {
    const source = anime?.posterOriginal || anime?.poster;
    if (!source) return "";
    try {
      return new URL(source, window.location.origin).href;
    } catch {
      return source;
    }
  }, [anime?.poster, anime?.posterOriginal]);

  const finishClose = useCallback(() => {
    if (closedRef.current) return;
    closedRef.current = true;
    window.clearTimeout(fallbackTimerRef.current);
    document.body.classList.remove("modal-open");
    document.body.style.removeProperty("--modal-scrollbar-compensation");
    onClosed?.();
  }, [onClosed]);

  const requestClose = useCallback(() => {
    if (closingRef.current || !panelRef.current || !backdropRef.current) return;
    closingRef.current = true;
    setPhase("closing");
    timelineRef.current?.kill();
    const pieceTargets = rootRef.current?.querySelectorAll("[data-featured-modal-piece]") || [];
    gsap.killTweensOf([panelRef.current, backdropRef.current, ...pieceTargets]);
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    timelineRef.current = gsap.timeline({ onComplete: finishClose });
    if (reduced) {
      timelineRef.current.to([panelRef.current, backdropRef.current], {
        autoAlpha: 0,
        duration: 0.08,
        ease: "none",
        overwrite: "auto",
      });
    } else {
      timelineRef.current
        .to(panelRef.current, {
          scale: 0.8,
          rotation: 3,
          y: 50,
          autoAlpha: 0,
          duration: 0.24,
          ease: "power3.in",
          force3D: true,
          overwrite: "auto",
        })
        .to(backdropRef.current, {
          autoAlpha: 0,
          duration: 0.14,
          ease: "power2.in",
          overwrite: "auto",
        }, 0.1);
    }
    fallbackTimerRef.current = window.setTimeout(finishClose, reduced ? 180 : 520);
    gsap.ticker.wake();
  }, [finishClose]);

  useEffect(() => {
    setActiveTab("summary");
  }, [column?.id]);

  useLayoutEffect(() => {
    if (!anime || !rootRef.current || !panelRef.current || !backdropRef.current) return undefined;
    closingRef.current = false;
    closedRef.current = false;
    setPhase("opening");
    const scrollbarWidth = Math.max(0, window.innerWidth - document.documentElement.clientWidth);
    document.body.style.setProperty("--modal-scrollbar-compensation", `${scrollbarWidth}px`);
    document.body.classList.add("modal-open");
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const pieceTargets = rootRef.current.querySelectorAll("[data-featured-modal-piece]");
    const context = gsap.context(() => {
      timelineRef.current = gsap.timeline({
        defaults: { force3D: true },
        onComplete: () => {
          if (closingRef.current) return;
          gsap.set([panelRef.current, ...pieceTargets], { clearProps: "willChange" });
          setPhase("opened");
          closeRef.current?.focus({ preventScroll: true });
        },
      });
      if (reduced) {
        timelineRef.current
          .fromTo(backdropRef.current, { autoAlpha: 0 }, { autoAlpha: 1, duration: 0.08, ease: "none" })
          .fromTo([panelRef.current, ...pieceTargets], { autoAlpha: 0 }, { autoAlpha: 1, duration: 0.08, ease: "none" }, 0);
      } else {
        timelineRef.current
          .fromTo(backdropRef.current, { autoAlpha: 0 }, {
            autoAlpha: 1,
            duration: 0.14,
            ease: "power1.out",
          })
          .fromTo(panelRef.current, {
            autoAlpha: 0,
            scale: 0.5,
            rotation: -5,
            y: 24,
            willChange: "transform,opacity",
          }, {
            autoAlpha: 1,
            scale: 1,
            rotation: 0,
            y: 0,
            duration: 0.52,
            ease: "back.out(1.5)",
          }, 0.02)
          .fromTo(pieceTargets, {
            autoAlpha: 0,
            y: -24,
            scale: 0.88,
            willChange: "transform,opacity",
          }, {
            autoAlpha: 1,
            y: 0,
            scale: 1,
            duration: 0.38,
            stagger: 0.045,
            ease: "back.out(1.8)",
          }, 0.14);
      }
      gsap.ticker.wake();
    }, rootRef);

    return () => {
      window.clearTimeout(fallbackTimerRef.current);
      timelineRef.current?.kill();
      context.revert();
      document.body.classList.remove("modal-open");
      document.body.style.removeProperty("--modal-scrollbar-compensation");
    };
  }, [anime, column?.id]);

  useEffect(() => {
    if (!anime) return undefined;
    const handleKeyDown = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        requestClose();
        return;
      }
      if (event.key !== "Tab" || !panelRef.current) return;
      const focusable = [...panelRef.current.querySelectorAll('button:not(:disabled), a[href], [tabindex]:not([tabindex="-1"])')]
        .filter((element) => !element.hasAttribute("hidden"));
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
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [anime, requestClose]);

  if (!anime) return null;

  const modal = (
    <div
      className={`featured-anime-modal is-${phase}`}
      ref={rootRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
    >
      <div
        className="featured-anime-modal__backdrop"
        ref={backdropRef}
        aria-hidden="true"
        onMouseDown={(event) => {
          if (event.target === event.currentTarget) requestClose();
        }}
      >
        <section
          className="featured-anime-modal__panel"
          ref={panelRef}
          style={modalStyle}
          onMouseDown={(event) => event.stopPropagation()}
        >
          <div className="featured-anime-modal__stripe" aria-hidden="true"><span /><span /><span /></div>
          <div className="featured-anime-modal__body">
            <header className="featured-anime-modal__header" data-featured-modal-piece>
              <div>
                <span className="featured-anime-modal__eyebrow">ANIME FILE / 番剧档案</span>
                <h2 id={titleId}>{anime.title}</h2>
                <p>{anime.japaneseTitle}</p>
              </div>
              <button ref={closeRef} className="featured-anime-modal__close-top" type="button" onClick={requestClose} aria-label="关闭番剧档案">
                <Icon name="close" />
              </button>
            </header>

            <div className="featured-anime-modal__main">
              <aside className="featured-anime-modal__poster-column">
                <div className="featured-anime-modal__poster-viewer" data-featured-modal-piece>
                  <img src={anime.poster} alt={`${anime.title} 海报`} />
                  {posterUrl && (
                    <a className="featured-anime-modal__poster-action" href={posterUrl} target="_blank" rel="noreferrer" aria-label={`查看${anime.title}原始海报`}>
                      <Icon name="arrow-up-right" />
                      <span>查看原图</span>
                    </a>
                  )}
                </div>
                <div className="featured-anime-modal__source" data-featured-modal-piece>
                  <p>当前海报来源 · {posterUrl ? "本地原图" : "暂无原图"}</p>
                  <div className="featured-anime-modal__source-url" title={posterUrl || undefined}>{posterUrl || "未提供原图地址"}</div>
                </div>
                {anime.externalUrl && (
                  <a className="featured-anime-modal__external" href={anime.externalUrl} target="_blank" rel="noreferrer" data-featured-modal-piece>
                    <strong><Icon name="book" /> 前往{anime.externalSource || "资料站"}查看完整信息</strong>
                    <span>点击跳转 <Icon name="arrow-up-right" /></span>
                  </a>
                )}
              </aside>

              <div className="featured-anime-modal__information">
                <div className="featured-anime-modal__facts">
                  <div className="featured-anime-modal__fact featured-anime-modal__fact--yellow" data-featured-modal-piece><span>放送季度</span><strong className="featured-anime-modal__fact-value">{displayPeriod(anime.period)}</strong></div>
                  <div className="featured-anime-modal__fact featured-anime-modal__fact--pink" data-featured-modal-piece><span>综合评分</span><FeaturedModalScore score={anime.score} /></div>
                  <div className="featured-anime-modal__fact featured-anime-modal__fact--teal" data-featured-modal-piece><span>制作公司</span><StudioMarquee value={anime.studio} /></div>
                  <div className="featured-anime-modal__fact featured-anime-modal__fact--white" data-featured-modal-piece><span>话数情况</span><strong className="featured-anime-modal__fact-value">{anime.episodeCount}</strong></div>
                </div>

                <div className="featured-anime-modal__tags" data-featured-modal-piece>
                  <span>标签分类 / TAGS</span>
                  <div>{anime.tags.slice(0, 8).map((tag) => <TagChip tag={tag} color={anime.tagColors?.[tag]} key={tag} />)}</div>
                </div>

                <div className="featured-anime-modal__content-piece" data-featured-modal-piece>
                  <div className={`featured-anime-modal__tabs${anime.watchHistory?.length ? " has-history" : ""}`} role="tablist" aria-label="番剧档案内容">
                    <button type="button" role="tab" aria-selected={activeTab === "summary"} aria-controls={tabPanelId} className={activeTab === "summary" ? "is-active" : ""} onClick={() => setActiveTab("summary")}><Icon name="list" /> 剧情简介</button>
                    <button type="button" role="tab" aria-selected={activeTab === "review"} aria-controls={tabPanelId} className={activeTab === "review" ? "is-active" : ""} onClick={() => setActiveTab("review")}><Icon name="edit" /> 个人评价</button>
                    {anime.watchHistory?.length > 0 && <button type="button" role="tab" aria-selected={activeTab === "history"} aria-controls={tabPanelId} className={activeTab === "history" ? "is-active" : ""} onClick={() => setActiveTab("history")}><Icon name="history" /> 观看情况</button>}
                  </div>

                  <div className="featured-anime-modal__copy" id={tabPanelId} role="tabpanel" tabIndex={0} aria-label={activeTab === "summary" ? "剧情简介" : activeTab === "review" ? "个人评价" : "观看情况"}>
                    {activeTab === "summary" && renderParagraphs(anime.summary, "暂无剧情简介。")}
                    {activeTab === "review" && renderParagraphs(anime.personalReview, "暂未记录个人评价。")}
                    {activeTab === "history" && <WatchHistoryList records={anime.watchHistory || []} />}
                  </div>
                </div>
              </div>
            </div>

            <footer className="featured-anime-modal__footer">
              <button type="button" onClick={requestClose}>关闭</button>
            </footer>
          </div>
        </section>
      </div>
    </div>
  );

  return createPortal(modal, document.body);
}
