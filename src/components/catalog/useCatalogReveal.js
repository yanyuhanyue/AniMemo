import { useEffect, useRef } from "react";

const REVEAL_SELECTOR = ".catalog-reveal-entry:not(.is-revealed)";

export function useCatalogReveal({ enabled = true, dependency = "" } = {}) {
  const containerRef = useRef(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || !enabled) return undefined;

    const entries = Array.from(container.querySelectorAll(REVEAL_SELECTOR));
    if (!entries.length) return undefined;

    const frames = new Set();
    const cleanupTimers = new Set();
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const reveal = (element, delayMs = 0) => {
      element.style.animationDelay = `${delayMs}ms`;
      const frame = window.requestAnimationFrame(() => {
        frames.delete(frame);
        element.classList.add("is-revealed");
      });
      frames.add(frame);

      const cleanupTimer = window.setTimeout(() => {
        cleanupTimers.delete(cleanupTimer);
        element.style.willChange = "auto";
        element.classList.add("is-animation-complete");
      }, 500);
      cleanupTimers.add(cleanupTimer);
    };

    if (reducedMotion || !("IntersectionObserver" in window)) {
      entries.forEach((element) => reveal(element));
      return () => {
        frames.forEach((frame) => window.cancelAnimationFrame(frame));
        cleanupTimers.forEach((timer) => window.clearTimeout(timer));
      };
    }

    let batchIndex = 0;
    let batchResetTimer;
    const observer = new IntersectionObserver((observedEntries) => {
      const visibleEntries = observedEntries.filter((entry) => entry.isIntersecting);
      if (!visibleEntries.length) return;

      visibleEntries.forEach((entry) => {
        observer.unobserve(entry.target);
        reveal(entry.target, Math.min(batchIndex * 20, 100));
        batchIndex += 1;
      });

      window.clearTimeout(batchResetTimer);
      batchResetTimer = window.setTimeout(() => {
        batchIndex = 0;
      }, 50);
    }, {
      root: null,
      rootMargin: "0px 0px 100px 0px",
      threshold: 0,
    });

    entries.forEach((element) => observer.observe(element));

    return () => {
      observer.disconnect();
      window.clearTimeout(batchResetTimer);
      frames.forEach((frame) => window.cancelAnimationFrame(frame));
      cleanupTimers.forEach((timer) => window.clearTimeout(timer));
    };
  }, [dependency, enabled]);

  return containerRef;
}
