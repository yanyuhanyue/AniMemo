import { forwardRef, useImperativeHandle, useLayoutEffect, useRef } from "react";
import gsap from "gsap";

const STORAGE_KEY = "anime-journal-auth-transition";

export const AuthRouteTransition = forwardRef(function AuthRouteTransition(_, forwardedRef) {
  const curtainRef = useRef(null);

  useImperativeHandle(forwardedRef, () => ({
    cover(direction) {
      sessionStorage.setItem(STORAGE_KEY, direction);
      const curtain = curtainRef.current;
      if (!curtain || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return Promise.resolve();
      gsap.killTweensOf(curtain);
      return new Promise((resolve) => {
        let resolved = false;
        const finish = () => {
          if (resolved) return;
          resolved = true;
          resolve();
        };
        gsap.set(curtain, { visibility: "visible", scaleX: 0, transformOrigin: direction === "to-staff" ? "100% 50%" : "0% 50%" });
        gsap.to(curtain, { scaleX: 1, duration: .18, ease: "power3.in", overwrite: "auto", onComplete: finish });
        gsap.ticker.wake();
        window.setTimeout(finish, 240);
      });
    },
  }), []);

  useLayoutEffect(() => {
    const direction = sessionStorage.getItem(STORAGE_KEY);
    if (!direction || !curtainRef.current) return undefined;
    sessionStorage.removeItem(STORAGE_KEY);
    const curtain = curtainRef.current;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      gsap.set(curtain, { visibility: "hidden", scaleX: 0 });
      return undefined;
    }
    gsap.set(curtain, { visibility: "visible", scaleX: 1, transformOrigin: direction === "to-staff" ? "0% 50%" : "100% 50%" });
    gsap.to(curtain, { scaleX: 0, duration: .24, delay: .03, ease: "power3.out", overwrite: "auto", onComplete: () => gsap.set(curtain, { visibility: "hidden" }) });
    gsap.ticker.wake();
    return () => gsap.killTweensOf(curtain);
  }, []);

  return (
    <div className="auth-route-curtain" ref={curtainRef} aria-hidden="true">
      <span /><span /><span /><span />
    </div>
  );
});
