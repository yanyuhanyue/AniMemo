import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import gsap from "gsap";
import { BOOT_PROGRESS_STAGES, nextMonotonicProgress } from "../lib/bootProgress.js";

const MINIMUM_DISPLAY_MS = 1250;
const COMPLETE_HOLD_MS = 190;

function wait(duration) {
  return new Promise((resolve) => window.setTimeout(resolve, Math.max(0, duration)));
}

function nextStablePaint() {
  return new Promise((resolve) => {
    window.requestAnimationFrame(() => window.requestAnimationFrame(resolve));
  });
}

function now() {
  return typeof performance === "undefined" ? Date.now() : performance.now();
}

function decodeImage(src) {
  return new Promise((resolve) => {
    if (!src) {
      resolve();
      return;
    }

    const image = new Image();
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      resolve();
    };

    image.decoding = "async";
    image.onload = () => {
      if (typeof image.decode === "function") image.decode().catch(() => {}).finally(finish);
      else finish();
    };
    image.onerror = finish;
    image.src = src;
    if (image.complete) image.onload();
  });
}

export function AppBootLoader({ dataReady, criticalImages, onComplete }) {
  const rootRef = useRef(null);
  const statusRef = useRef(null);
  const finishRef = useRef(null);
  const runIdRef = useRef(0);
  const displayedProgressRef = useRef(0);
  const isCompleteRef = useRef(false);
  const criticalImagePlanRef = useRef(null);
  const mountedAtRef = useRef(now());
  const completedRef = useRef(false);
  const notifiedRef = useRef(false);
  const exitStartTimerRef = useRef(null);
  const exitCompleteTimerRef = useRef(null);
  const onCompleteRef = useRef(onComplete);
  const [progress, setProgress] = useState(0);

  const updateProgress = useCallback((nextValue) => {
    const monotonic = nextMonotonicProgress(displayedProgressRef.current, nextValue);
    if (isCompleteRef.current && monotonic < BOOT_PROGRESS_STAGES.complete) return;

    displayedProgressRef.current = monotonic;
    if (monotonic >= BOOT_PROGRESS_STAGES.complete) isCompleteRef.current = true;
    setProgress((current) => nextMonotonicProgress(current, monotonic));
  }, []);

  useEffect(() => {
    onCompleteRef.current = onComplete;
  }, [onComplete]);

  useLayoutEffect(() => {
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const notifyComplete = () => {
      if (notifiedRef.current) return;
      notifiedRef.current = true;
      window.clearTimeout(exitStartTimerRef.current);
      window.clearTimeout(exitCompleteTimerRef.current);
      gsap.set(rootRef.current, { autoAlpha: 0 });
      onCompleteRef.current?.();
    };
    const context = gsap.context(() => {
      gsap.set(rootRef.current, { autoAlpha: 1 });

      if (!reducedMotion) {
        gsap.from(".app-boot-loader__panel", {
          yPercent: -105,
          duration: 0.38,
          stagger: 0.045,
          ease: "power3.out",
          immediateRender: false,
        });
        gsap.from(".app-boot-loader__copy > *", {
          y: -20,
          opacity: 0,
          duration: 0.28,
          stagger: 0.05,
          delay: 0.15,
          ease: "power3.out",
          immediateRender: false,
        });
      }

      finishRef.current = () => {
        if (completedRef.current) return;
        completedRef.current = true;
        updateProgress(BOOT_PROGRESS_STAGES.complete);
        if (statusRef.current) statusRef.current.textContent = "SYNC COMPLETE / 同步完成";

        exitStartTimerRef.current = window.setTimeout(() => {
          if (reducedMotion) {
            gsap.to(rootRef.current, {
              autoAlpha: 0,
              duration: 0.1,
              onComplete: notifyComplete,
            });
            exitCompleteTimerRef.current = window.setTimeout(notifyComplete, 220);
            return;
          }

          gsap.timeline({ onComplete: notifyComplete })
            .to(".app-boot-loader__copy", { x: 8, y: 8, duration: 0.08, ease: "power2.in" }, 0)
            .to(".app-boot-loader__copy", { x: 0, y: 0, duration: 0.1, ease: "power2.out" }, 0.08)
            .to(".app-boot-loader__panel:nth-child(odd)", { yPercent: 105, duration: 0.34, ease: "power3.in" }, 0.14)
            .to(".app-boot-loader__panel:nth-child(even)", { yPercent: -105, duration: 0.34, ease: "power3.in" }, 0.18)
            .to(rootRef.current, { autoAlpha: 0, duration: 0.12 }, 0.48);
          gsap.ticker.wake();
          exitCompleteTimerRef.current = window.setTimeout(notifyComplete, 820);
        }, reducedMotion ? 20 : COMPLETE_HOLD_MS);
      };
    }, rootRef);

    return () => {
      window.clearTimeout(exitStartTimerRef.current);
      window.clearTimeout(exitCompleteTimerRef.current);
      finishRef.current = null;
      context.revert();
    };
  }, [updateProgress]);

  useEffect(() => {
    const fallbackTimer = window.setTimeout(() => finishRef.current?.(), 4600);
    return () => window.clearTimeout(fallbackTimer);
  }, []);

  useEffect(() => {
    const runId = ++runIdRef.current;
    const isCurrentRun = () => runId === runIdRef.current && !completedRef.current;
    const safeUpdate = (value) => {
      if (isCurrentRun()) updateProgress(value);
    };

    safeUpdate(BOOT_PROGRESS_STAGES.mounted);

    const fontsReady = Promise.resolve(document.fonts?.ready).catch(() => undefined);
    fontsReady.then(() => safeUpdate(BOOT_PROGRESS_STAGES.fontsReady));

    if (dataReady) {
      safeUpdate(BOOT_PROGRESS_STAGES.dataReady);
      if (!criticalImagePlanRef.current) {
        criticalImagePlanRef.current = [...new Set(criticalImages.filter(Boolean))].slice(0, 5);
      }

      const imagesReady = Promise.allSettled(criticalImagePlanRef.current.map(decodeImage))
        .then(() => safeUpdate(BOOT_PROGRESS_STAGES.imagesReady));

      Promise.allSettled([fontsReady, imagesReady]).then(async () => {
        if (!isCurrentRun()) return;
        await nextStablePaint();
        safeUpdate(BOOT_PROGRESS_STAGES.layoutReady);
        await wait(MINIMUM_DISPLAY_MS - (now() - mountedAtRef.current));
        if (isCurrentRun()) finishRef.current?.();
      });
    }

    return () => {
      if (runId === runIdRef.current) runIdRef.current += 1;
    };
  }, [criticalImages, dataReady, updateProgress]);

  return (
    <div className={`app-boot-loader${dataReady ? " is-data-ready" : ""}`} ref={rootRef} role="status" aria-live="polite" aria-label="正在同步番剧手账">
      <div className="app-boot-loader__panels" aria-hidden="true">
        <i className="app-boot-loader__panel yellow" />
        <i className="app-boot-loader__panel pink" />
        <i className="app-boot-loader__panel teal" />
        <i className="app-boot-loader__panel coral" />
      </div>
      <div className="app-boot-loader__grid" aria-hidden="true" />
      <div className="app-boot-loader__copy">
        <span>PRIVATE WATCH LOG / BOOT SIGNAL</span>
        <h1><b>ANIME</b><strong>JOURNAL</strong></h1>
        <div className="app-boot-loader__rail"><i style={{ transform: `scaleX(${progress / 100})` }} /></div>
        <footer>
          <small ref={statusRef}>SYNCING LIVE JOURNAL / 同步手账信号</small>
          <b><span className="app-boot-loader__progress-value">{String(progress).padStart(2, "0")}</span>%</b>
        </footer>
      </div>
    </div>
  );
}
