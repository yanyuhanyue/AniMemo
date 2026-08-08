import { forwardRef, useCallback, useImperativeHandle, useLayoutEffect, useRef } from "react";
import gsap from "gsap";

export const AuthModeTransition = forwardRef(function AuthModeTransition({ mode, sequenceKey, children }, forwardedRef) {
  const contentRef = useRef(null);
  const timelineRef = useRef(null);
  const fallbackRef = useRef(null);
  const exitResolveRef = useRef(null);

  const finishExit = useCallback(() => {
    window.clearTimeout(fallbackRef.current);
    fallbackRef.current = null;
    const resolve = exitResolveRef.current;
    exitResolveRef.current = null;
    resolve?.();
  }, []);

  const stopTimeline = useCallback(() => {
    timelineRef.current?.kill();
    timelineRef.current = null;
    finishExit();
  }, [finishExit]);

  useImperativeHandle(forwardedRef, () => ({
    exit() {
      const content = contentRef.current;
      if (!content) return Promise.resolve();
      stopTimeline();

      const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      const steps = content.querySelectorAll("[data-auth-step]");
      if (reduced || !steps.length) {
        gsap.set(steps, { clearProps: "transform,opacity,visibility" });
        return Promise.resolve();
      }

      return new Promise((resolve) => {
        exitResolveRef.current = resolve;
        timelineRef.current = gsap.to(steps, {
          y: -8,
          opacity: 0,
          duration: .14,
          stagger: .018,
          ease: "power2.in",
          overwrite: "auto",
          onComplete: finishExit,
          onInterrupt: finishExit,
        });
        fallbackRef.current = window.setTimeout(finishExit, 360);
        gsap.ticker.wake();
      });
    },
  }), [finishExit, stopTimeline]);

  useLayoutEffect(() => {
    const content = contentRef.current;
    if (!content) return undefined;
    stopTimeline();

    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const steps = content.querySelectorAll("[data-auth-step]");
    gsap.set(content, { clearProps: "transform,opacity" });
    gsap.set(steps, { clearProps: "transform,opacity,visibility" });

    if (reduced || !steps.length) return undefined;

    timelineRef.current = gsap.fromTo(steps, {
      y: -18,
      scale: .985,
      opacity: 0,
    }, {
      y: 0,
      scale: 1,
      opacity: 1,
      duration: .36,
      stagger: .045,
      ease: "back.out(1.35)",
      overwrite: "auto",
      clearProps: "transform,opacity,visibility",
    });
    gsap.ticker.wake();

    return () => {
      window.clearTimeout(fallbackRef.current);
      timelineRef.current?.kill();
      timelineRef.current = null;
      gsap.set(content, { clearProps: "transform,opacity" });
      gsap.set(steps, { clearProps: "transform,opacity,visibility" });
    };
  }, [mode, sequenceKey, stopTimeline]);

  return <div className="auth-mode-frame auth-mode-stage"><div className="auth-mode-content" ref={contentRef}>{children}</div></div>;
});
