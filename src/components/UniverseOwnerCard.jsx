import { useEffect, useRef } from "react";
import gsap from "gsap";
import { Icon } from "./Icon.jsx";
import { RatingDisplay } from "./RatingDisplay.jsx";
import { fallbackAvatarImage, fallbackPosterImage } from "../lib/mediaAssets.js";

const PHOTO_LAYOUTS = {
  1: [
    {
      idle: { x: 0, y: 22, rotation: -2, zIndex: 3 },
      open: { x: 0, y: 2, rotation: -2, zIndex: 5 },
      mobile: { x: 0, y: 18, rotation: -1, zIndex: 3 },
      shadow: "#ff2f92",
    },
  ],
  2: [
    {
      idle: { x: -96, y: 34, rotation: -6, zIndex: 3 },
      open: { x: -96, y: 14, rotation: -6, zIndex: 5 },
      mobile: { x: -64, y: 24, rotation: -6, zIndex: 3 },
      shadow: "#ff2f92",
    },
    {
      idle: { x: 96, y: 26, rotation: 3, zIndex: 2 },
      open: { x: 96, y: 6, rotation: 3, zIndex: 4 },
      mobile: { x: 64, y: 14, rotation: 5, zIndex: 2 },
      shadow: "#4ecdc4",
    },
  ],
  3: [
    {
      idle: { x: -172, y: 14, rotation: -6, zIndex: 3 },
      open: { x: -172, y: -6, rotation: -6, zIndex: 5 },
      mobile: { x: -82, y: 30, rotation: -7, zIndex: 3 },
      shadow: "#ff2f92",
    },
    {
      idle: { x: 0, y: -19, rotation: 3, zIndex: 2 },
      open: { x: 0, y: -39, rotation: 3, zIndex: 4 },
      mobile: { x: 0, y: 0, rotation: 2.5, zIndex: 2 },
      shadow: "#4ecdc4",
    },
    {
      idle: { x: 172, y: 21, rotation: -2, zIndex: 1 },
      open: { x: 172, y: 1, rotation: -2, zIndex: 3 },
      mobile: { x: 82, y: 28, rotation: 6, zIndex: 1 },
      shadow: "#ffe66d",
    },
  ],
};

function getPhotoMotion(count, index) {
  return PHOTO_LAYOUTS[Math.min(Math.max(count, 1), 3)][index];
}

function getPhotoState(count, index, stateName) {
  const motion = getPhotoMotion(count, index);
  return motion[stateName];
}

function TopPickPhotoStack({ owner, picks, photoRefs, onPhotoEnter, onPhotoLeave }) {
  return (
    <div className="owner-poster-stage">
      {!picks.length && <p className="owner-empty-picks">这本手账正在等待第一条公开高分记录。</p>}
      {picks.map((pick, index) => {
        const motion = getPhotoMotion(picks.length, index);
        return (
          <figure
            className="owner-polaroid"
            data-photo-index={index}
            key={pick.id || `${owner.id}-${index}`}
            onPointerEnter={() => onPhotoEnter(index)}
            onPointerLeave={() => onPhotoLeave(index)}
            ref={(node) => { photoRefs.current[index] = node; }}
            style={{
              "--idle-x": `${motion.idle.x}px`,
              "--idle-y": `${motion.idle.y}px`,
              "--idle-r": `${motion.idle.rotation}deg`,
              "--mobile-x": `${motion.mobile.x}px`,
              "--mobile-y": `${motion.mobile.y}px`,
              "--mobile-r": `${motion.mobile.rotation}deg`,
              "--photo-shadow": motion.shadow,
              zIndex: motion.idle.zIndex,
            }}
          >
            <div className="owner-polaroid__image">
              <img
                src={pick.poster}
                alt={`${pick.title || `高分作品 ${index + 1}`} 海报`}
                width="400"
                height="600"
                onError={fallbackPosterImage}
              />
            </div>
            <figcaption>
              <b>NO.{index + 1}</b>
              <span>TOP PICK</span>
            </figcaption>
          </figure>
        );
      })}
    </div>
  );
}

export function UniverseOwnerCard({ owner, onOpen }) {
  const picks = Array.isArray(owner.top_picks) ? owner.top_picks.slice(0, 3) : [];
  const entryRef = useRef(null);
  const cardRef = useRef(null);
  const surfaceRef = useRef(null);
  const photoRefs = useRef([]);
  const timelineRef = useRef(null);
  const photoFallbackRef = useRef(null);
  const cardFallbackRef = useRef(null);
  const entryFallbackRef = useRef(null);
  const leaveTimerRef = useRef(null);
  const expandedRef = useRef(false);
  const finePointerRef = useRef(false);
  const reducedMotionRef = useRef(false);

  const applyPhotoState = (stateName, immediate = false) => {
    const photos = photoRefs.current.filter(Boolean);
    if (!photos.length) return;

    window.clearTimeout(photoFallbackRef.current);
    timelineRef.current?.kill();
    gsap.killTweensOf(photos);
    const opening = stateName === "open";

    photos.forEach((photo) => photo.style.removeProperty("filter"));

    const finalize = () => {
      photos.forEach((photo, index) => {
        const motion = getPhotoMotion(photos.length, index);
        const state = getPhotoState(photos.length, index, stateName, cardRef.current?.getBoundingClientRect().width || 0);
        photo.style.zIndex = String(state.zIndex);
        gsap.set(photo, {
          xPercent: -50,
          x: state.x,
          y: state.y,
          rotation: state.rotation,
          scale: 1,
          boxShadow: `9px 9px 0 ${motion.shadow}`,
        });
      });
    };

    if (immediate) {
      finalize();
      return;
    }

    const timeline = gsap.timeline({
      defaults: { overwrite: "auto" },
      onComplete: () => window.clearTimeout(photoFallbackRef.current),
    });

    photos.forEach((photo, index) => {
      const motion = getPhotoMotion(photos.length, index);
      const state = getPhotoState(photos.length, index, stateName, cardRef.current?.getBoundingClientRect().width || 0);
      if (opening || stateName === "mobile") {
        photo.style.zIndex = String(state.zIndex);
      }
      timeline.to(photo, {
        xPercent: -50,
        x: state.x,
        y: state.y,
        rotation: state.rotation,
        scale: 1,
        boxShadow: `9px 9px 0 ${motion.shadow}`,
        duration: opening ? 0.18 : 0.15,
        ease: "power3.out",
        onComplete: opening || stateName === "mobile" ? undefined : () => {
          photo.style.zIndex = String(state.zIndex);
        },
      }, opening ? index * 0.035 : (photos.length - index - 1) * 0.025);
    });

    timelineRef.current = timeline;
    gsap.ticker.wake();
    photoFallbackRef.current = window.setTimeout(() => {
      timeline.kill();
      finalize();
    }, opening ? 420 : 360);
  };

  const animateCard = (expanded, immediate = false) => {
    expandedRef.current = expanded;
    const duration = immediate ? 0 : 0.12;
    const ease = immediate ? "none" : "steps(3)";
    window.clearTimeout(cardFallbackRef.current);
    const surface = surfaceRef.current;
    if (!surface) return;
    gsap.killTweensOf(surface);
    surface.style.removeProperty("transform");
    const compact = (cardRef.current?.getBoundingClientRect().width || 0) < 500;
    const offset = compact ? 0 : expanded ? 6 : 0;
    const shadow = compact ? "7px 7px 0 #111" : expanded ? "6px 6px 0 #111" : "12px 12px 0 #111";
    const finalizeCard = () => {
      gsap.set(surface, {
        left: offset,
        top: offset,
        boxShadow: shadow,
      });
    };
    if (immediate) {
      finalizeCard();
      applyPhotoState(expanded ? "open" : "idle", true);
      return;
    }
    gsap.to(surface, {
      left: offset,
      top: offset,
      boxShadow: shadow,
      duration,
      ease,
      overwrite: "auto",
      onComplete: () => window.clearTimeout(cardFallbackRef.current),
    });
    applyPhotoState(expanded ? "open" : "idle", immediate);
    gsap.ticker.wake();
    cardFallbackRef.current = window.setTimeout(finalizeCard, 360);
  };

  const pressSurface = (pressed) => {
    const expanded = expandedRef.current;
    const surface = surfaceRef.current;
    if (!surface) return;
    gsap.killTweensOf(surface);
    surface.style.removeProperty("transform");
    const compact = (cardRef.current?.getBoundingClientRect().width || 0) < 500;
    const restOffset = compact ? 0 : expanded ? 6 : 0;
    const restShadow = compact ? "7px 7px 0 #111" : expanded ? "6px 6px 0 #111" : "12px 12px 0 #111";
    gsap.to(surface, {
      left: pressed ? (compact ? 7 : 12) : restOffset,
      top: pressed ? (compact ? 7 : 12) : restOffset,
      boxShadow: pressed ? "0 0 0 #111" : restShadow,
      duration: 0.08,
      ease: "steps(2)",
      overwrite: "auto",
    });
  };

  useEffect(() => {
    const reducedQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
    const pointerQuery = window.matchMedia("(hover: hover) and (pointer: fine)");
    const mobileQuery = window.matchMedia("(max-width: 767px)");
    reducedMotionRef.current = reducedQuery.matches;
    finePointerRef.current = pointerQuery.matches && !mobileQuery.matches;

    const context = gsap.context(() => {
      if (reducedQuery.matches) {
        animateCard(false, true);
        applyPhotoState(finePointerRef.current ? "idle" : "mobile", true);
        return;
      }

      gsap.fromTo(entryRef.current, { autoAlpha: 0, y: 28 }, {
        autoAlpha: 1,
        y: 0,
        duration: 0.34,
        ease: "power3.out",
        clearProps: "transform,opacity,visibility",
        immediateRender: false,
      });
      gsap.ticker.wake();
      entryFallbackRef.current = window.setTimeout(() => gsap.set(entryRef.current, { clearProps: "transform,opacity" }), 620);
      animateCard(false, true);
      if (!finePointerRef.current) applyPhotoState("mobile", true);
    }, cardRef);

    const card = cardRef.current;
    const handleMouseEnter = () => {
      if (!finePointerRef.current || reducedMotionRef.current) return;
      window.clearTimeout(leaveTimerRef.current);
      animateCard(true);
    };
    const handleMouseLeave = () => {
      if (!finePointerRef.current || reducedMotionRef.current) return;
      window.clearTimeout(leaveTimerRef.current);
      leaveTimerRef.current = window.setTimeout(() => {
        if (card?.matches(":hover") || card?.contains(document.activeElement)) {
          animateCard(true);
          return;
        }
        animateCard(false);
      }, 24);
    };
    card?.addEventListener("mouseenter", handleMouseEnter);
    card?.addEventListener("mouseleave", handleMouseLeave);

    return () => {
      card?.removeEventListener("mouseenter", handleMouseEnter);
      card?.removeEventListener("mouseleave", handleMouseLeave);
      window.clearTimeout(leaveTimerRef.current);
      window.clearTimeout(photoFallbackRef.current);
      window.clearTimeout(cardFallbackRef.current);
      window.clearTimeout(entryFallbackRef.current);
      timelineRef.current?.kill();
      gsap.killTweensOf(photoRefs.current.filter(Boolean));
      context.revert();
    };
  }, [picks.length]);

  const expand = () => {
    if (!finePointerRef.current || reducedMotionRef.current) return;
    animateCard(true);
  };

  const collapse = () => {
    if (!finePointerRef.current || reducedMotionRef.current) return;
    if (cardRef.current?.contains(document.activeElement)) return;
    animateCard(false);
  };

  const hoverPhoto = (index, entering) => {
    if (!finePointerRef.current || !expandedRef.current || reducedMotionRef.current) return;
    const photo = photoRefs.current[index];
    if (!photo) return;
    const motion = getPhotoMotion(picks.length, index);
    const state = getPhotoState(picks.length, index, "open", cardRef.current?.getBoundingClientRect().width || 0);
    gsap.killTweensOf(photo);
    if (entering) photo.style.zIndex = "10";
    else photo.style.zIndex = String(state.zIndex);
    gsap.to(photo, {
      xPercent: -50,
      x: state.x,
      y: entering ? state.y - 20 : state.y,
      rotation: entering ? 0 : state.rotation,
      scale: 1,
      boxShadow: entering ? "12px 12px 0 #111" : `9px 9px 0 ${motion.shadow}`,
      duration: 0.15,
      ease: "power2.out",
      overwrite: "auto",
    });
  };

  const stats = owner.stats || {};
  return (
    <div className="universe-owner-entry" ref={entryRef}>
      <article
        className="journal-owner-card"
        ref={cardRef}
        role="link"
        tabIndex="0"
        aria-label={`打开 ${owner.nickname} 的公开手账`}
        onClick={() => onOpen(owner)}
        onKeyDown={(event) => {
          if (!["Enter", " "].includes(event.key)) return;
          event.preventDefault();
          onOpen(owner);
        }}
        onPointerDown={() => pressSurface(true)}
        onPointerUp={() => pressSurface(false)}
        onPointerCancel={() => pressSurface(false)}
        onFocus={expand}
        onBlur={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget)) collapse();
        }}
      >
        <div className="journal-owner-card__surface" ref={surfaceRef}>
          <header className="journal-owner-card__header">
            <img className="journal-owner-card__avatar" src={owner.avatar} alt={`${owner.nickname} 的头像`} width="108" height="108" onError={fallbackAvatarImage} />
            <div className="journal-owner-card__identity">
              <span className="owner-badge">JOURNAL OWNER</span>
              <h3>{owner.nickname}</h3>
              <p>{owner.subtitle}</p>
              <div className="owner-stat-row">
                <span className="owner-stat yellow"><Icon name="circle-check" /> 追完 <b>{stats.completed_count ?? 0}</b></span>
                <span className="owner-stat pink"><Icon name="chart-line" /> 均分 <RatingDisplay score={stats.average_score} compact showStars={false} precision={2} /></span>
                <span className="owner-stat teal"><Icon name="crown" /> 9.5+ <b>{stats.masterpiece_count ?? 0}</b></span>
              </div>
              <div className="owner-mini-stats">
                <span><Icon name="film" /> 剧场版 {stats.movie_count ?? 0}</span>
                <span><Icon name="compact-disc" /> OVA {stats.ova_count ?? 0}</span>
                <span><Icon name="bowl-food" /> 泡面番 {stats.short_count ?? 0}</span>
              </div>
            </div>
            <i className="owner-open"><Icon name="arrow-up-right" /></i>
          </header>
          <div className="owner-top-picks__title">
            <span>TOP 3 / 高分轨道</span>
            <small><Icon name="hand-pointer" /> 悬停抽取照片</small>
          </div>
          <TopPickPhotoStack
            owner={owner}
            picks={picks}
            photoRefs={photoRefs}
            onPhotoEnter={(index) => hoverPhoto(index, true)}
            onPhotoLeave={(index) => hoverPhoto(index, false)}
          />
        </div>
      </article>
    </div>
  );
}
