import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import gsap from "gsap";

const PageColorTransitionContext = createContext(null);

const COVER_POLYGONS = [
  "polygon(-18% -12%, 124% -12%, 103% 112%, -39% 112%)",
  "polygon(-14% -12%, 128% -12%, 107% 112%, -35% 112%)",
  "polygon(-10% -12%, 132% -12%, 111% 112%, -31% 112%)",
];

const HIDDEN_LEFT_POLYGONS = [
  "polygon(-42% -12%, -10% -12%, -31% 112%, -63% 112%)",
  "polygon(-48% -12%, -16% -12%, -37% 112%, -69% 112%)",
  "polygon(-54% -12%, -22% -12%, -43% 112%, -75% 112%)",
];

const HIDDEN_RIGHT_POLYGONS = [
  "polygon(110% -12%, 142% -12%, 121% 112%, 89% 112%)",
  "polygon(116% -12%, 148% -12%, 127% 112%, 95% 112%)",
  "polygon(122% -12%, 154% -12%, 133% 112%, 101% 112%)",
];

function afterRouteCommit() {
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      resolve();
    };
    window.requestAnimationFrame(() => window.requestAnimationFrame(finish));
    window.setTimeout(finish, 120);
  });
}

export function PageColorTransition({ children }) {
  const navigate = useNavigate();
  const rootRef = useRef(null);
  const layerRefs = useRef([]);
  const timelineRef = useRef(null);
  const progressRootRef = useRef(null);
  const progressLayerRefs = useRef([]);
  const progressCopyRef = useRef(null);
  const progressRailRef = useRef(null);
  const progressStatusRef = useRef(null);
  const progressValueRef = useRef(null);
  const progressTimelineRef = useRef(null);
  const activeRef = useRef(false);
  const mountedRef = useRef(true);
  const [transitionMode, setTransitionMode] = useState(null);
  const isTransitioning = transitionMode !== null;

  const reset = useCallback(() => {
    timelineRef.current?.kill();
    timelineRef.current = null;
    progressTimelineRef.current?.kill();
    progressTimelineRef.current = null;
    const root = rootRef.current;
    const layers = layerRefs.current.filter(Boolean);
    const progressRoot = progressRootRef.current;
    if (root) gsap.set(root, { autoAlpha: 0 });
    if (progressRoot) gsap.set(progressRoot, { autoAlpha: 0 });
    layers.forEach((layer, index) => gsap.set(layer, { clipPath: HIDDEN_LEFT_POLYGONS[index] }));
    if (progressRailRef.current) gsap.set(progressRailRef.current, { scaleX: 0 });
    activeRef.current = false;
    if (mountedRef.current) setTransitionMode(null);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      timelineRef.current?.kill();
      progressTimelineRef.current?.kill();
    };
  }, []);

  const playTimeline = useCallback((timelineHolder, build, fallbackDelay) => new Promise((resolve) => {
    let finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      resolve();
    };
    timelineHolder.current?.kill();
    timelineHolder.current = build(finish);
    gsap.ticker.wake();
    window.setTimeout(finish, fallbackDelay);
  }), []);

  const navigateWithTransition = useCallback(async (path, options) => {
    if (!path || activeRef.current) return false;
    activeRef.current = true;
    setTransitionMode("color");

    const root = rootRef.current;
    const layers = layerRefs.current.filter(Boolean);
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    try {
      if (reduced || !root || layers.length !== 3) {
        navigate(path, options);
        await afterRouteCommit();
        return true;
      }

      gsap.set(root, { autoAlpha: 1 });
      layers.forEach((layer, index) => gsap.set(layer, { clipPath: HIDDEN_LEFT_POLYGONS[index] }));

      await playTimeline(timelineRef, (finish) => gsap.timeline({ onComplete: finish, onInterrupt: finish })
        .to(layers[0], { clipPath: COVER_POLYGONS[0], duration: .24, ease: "expo.out" }, 0)
        .to(layers[1], { clipPath: COVER_POLYGONS[1], duration: .24, ease: "expo.out" }, .035)
        .to(layers[2], { clipPath: COVER_POLYGONS[2], duration: .24, ease: "expo.out" }, .07), 430);

      navigate(path, options);
      await afterRouteCommit();

      await playTimeline(timelineRef, (finish) => gsap.timeline({ onComplete: finish, onInterrupt: finish })
        .to(layers[0], { clipPath: HIDDEN_RIGHT_POLYGONS[0], duration: .21, ease: "power4.inOut" }, 0)
        .to(layers[1], { clipPath: HIDDEN_RIGHT_POLYGONS[1], duration: .21, ease: "power4.inOut" }, .03)
        .to(layers[2], { clipPath: HIDDEN_RIGHT_POLYGONS[2], duration: .21, ease: "power4.inOut" }, .06), 390);
      return true;
    } catch {
      return false;
    } finally {
      reset();
    }
  }, [navigate, playTimeline, reset]);

  const navigateWithProgress = useCallback(async (path, options) => {
    if (!path || activeRef.current) return false;
    activeRef.current = true;
    setTransitionMode("progress");

    const root = progressRootRef.current;
    const layers = progressLayerRefs.current.filter(Boolean);
    const copy = progressCopyRef.current;
    const rail = progressRailRef.current;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    try {
      if (reduced || !root || !copy || !rail || layers.length !== 4) {
        navigate(path, options);
        await afterRouteCommit();
        return true;
      }

      const progress = { value: 0 };
      if (progressStatusRef.current) progressStatusRef.current.textContent = "LOADING NEXT PAGE / 正在准备目标页面";
      if (progressValueRef.current) progressValueRef.current.textContent = "00";
      gsap.set(root, { autoAlpha: 1 });
      gsap.set(layers, { yPercent: (index) => index % 2 === 0 ? -105 : 105 });
      gsap.set(copy, { autoAlpha: 0, x: 0, y: -18 });
      gsap.set(rail, { scaleX: 0, transformOrigin: "left center" });

      await playTimeline(progressTimelineRef, (finish) => gsap.timeline({ onComplete: finish, onInterrupt: finish })
        .to(layers, { yPercent: 0, duration: .34, stagger: .04, ease: "power3.out" }, 0)
        .to(copy, { autoAlpha: 1, y: 0, duration: .24, ease: "power3.out" }, .12)
        .to(progress, {
          value: 100,
          duration: .72,
          ease: "power2.inOut",
          onUpdate: () => {
            const value = Math.round(progress.value);
            gsap.set(rail, { scaleX: value / 100 });
            if (progressValueRef.current) progressValueRef.current.textContent = String(value).padStart(2, "0");
          },
        }, .2), 1150);

      if (progressValueRef.current) progressValueRef.current.textContent = "100";
      if (progressStatusRef.current) progressStatusRef.current.textContent = "ROUTE READY / 页面准备完成";
      navigate(path, options);
      await afterRouteCommit();

      await playTimeline(progressTimelineRef, (finish) => gsap.timeline({ onComplete: finish, onInterrupt: finish })
        .to(copy, { x: 7, y: 7, duration: .07, ease: "power2.in" }, 0)
        .to(copy, { autoAlpha: 0, x: 0, y: 0, duration: .1, ease: "power2.out" }, .07)
        .to(layers.filter((_, index) => index % 2 === 0), { yPercent: 105, duration: .34, ease: "power3.in" }, .13)
        .to(layers.filter((_, index) => index % 2 === 1), { yPercent: -105, duration: .34, ease: "power3.in" }, .17)
        .to(root, { autoAlpha: 0, duration: .08 }, .48), 720);
      return true;
    } catch {
      return false;
    } finally {
      reset();
    }
  }, [navigate, playTimeline, reset]);

  useEffect(() => {
    const handleInternalLink = (event) => {
      if (
        window.location.pathname === "/"
        || event.defaultPrevented
        || event.button !== 0
        || event.metaKey
        || event.ctrlKey
        || event.shiftKey
        || event.altKey
        || !(event.target instanceof Element)
      ) return;

      const anchor = event.target.closest("a[href]");
      if (
        !anchor
        || anchor.hasAttribute("download")
        || anchor.dataset.noPageTransition === "true"
        || (anchor.target && anchor.target !== "_self")
      ) return;

      const destination = new URL(anchor.href, window.location.href);
      if (destination.origin !== window.location.origin) return;
      // The showcase owns its boot/progress transition when it mounts.
      if (destination.pathname === "/") return;
      if (
        destination.pathname === window.location.pathname
        && destination.search === window.location.search
      ) return;

      event.preventDefault();
      void navigateWithTransition(`${destination.pathname}${destination.search}${destination.hash}`);
    };

    document.addEventListener("click", handleInternalLink, true);
    return () => document.removeEventListener("click", handleInternalLink, true);
  }, [navigateWithTransition]);

  const value = useMemo(() => ({
    isTransitioning,
    navigateWithProgress,
    navigateWithTransition,
  }), [isTransitioning, navigateWithProgress, navigateWithTransition]);

  return (
    <PageColorTransitionContext.Provider value={value}>
      {children}
      <div
        ref={progressRootRef}
        className="page-progress-transition"
        data-active={transitionMode === "progress" ? "true" : "false"}
        role="status"
        aria-live="polite"
        aria-label="正在切换页面"
      >
        <div className="app-boot-loader__panels" aria-hidden="true">
          {["yellow", "pink", "teal", "coral"].map((color, index) => (
            <i
              className={`app-boot-loader__panel ${color}`}
              key={color}
              ref={(node) => { progressLayerRefs.current[index] = node; }}
            />
          ))}
        </div>
        <div className="app-boot-loader__grid" aria-hidden="true" />
        <div className="app-boot-loader__copy" ref={progressCopyRef}>
          <span>PRIVATE WATCH LOG / ROUTE SIGNAL</span>
          <h1><b>ANIME</b><strong>JOURNAL</strong></h1>
          <div className="app-boot-loader__rail"><i ref={progressRailRef} /></div>
          <footer>
            <small ref={progressStatusRef}>LOADING NEXT PAGE / 正在准备目标页面</small>
            <b><span ref={progressValueRef}>00</span>%</b>
          </footer>
        </div>
      </div>
      <div
        ref={rootRef}
        className="page-color-transition"
        data-active={transitionMode === "color" ? "true" : "false"}
        aria-hidden="true"
      >
        {["yellow", "pink", "teal"].map((color, index) => (
          <i
            className={`page-color-transition__slash page-color-transition__slash--${color}`}
            key={color}
            ref={(node) => { layerRefs.current[index] = node; }}
          />
        ))}
      </div>
    </PageColorTransitionContext.Provider>
  );
}

export function usePageColorTransition() {
  const context = useContext(PageColorTransitionContext);
  if (!context) throw new Error("usePageColorTransition must be used inside PageColorTransition");
  return context;
}
