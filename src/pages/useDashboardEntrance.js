import { useEffect } from "react";
import gsap from "gsap";

export function useDashboardEntrance({ dashboardReady, modeTransition, rootRef }) {
  useEffect(() => {
    if (!dashboardReady || modeTransition) return undefined;
    const context = gsap.context(() => {
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
      const consolePiece = rootRef.current?.querySelector(".dashboard-memphis-console");
      const entrancePieces = rootRef.current?.querySelectorAll(".dashboard-entrance-piece");
      if (consolePiece) {
        gsap.fromTo(consolePiece, {
          y: -110,
          rotation: -1.5,
          autoAlpha: 0,
          willChange: "transform,opacity",
        }, {
          y: 0,
          rotation: 0,
          autoAlpha: 1,
          duration: 0.78,
          ease: "bounce.out",
          clearProps: "transform,opacity,visibility,willChange",
        });
      }
      if (entrancePieces?.length) {
        gsap.fromTo(entrancePieces, {
          y: 42,
          scale: 0.96,
          autoAlpha: 0,
          willChange: "transform,opacity",
        }, {
          y: 0,
          scale: 1,
          autoAlpha: 1,
          duration: 0.48,
          stagger: 0.1,
          ease: "back.out(1.5)",
          clearProps: "transform,opacity,visibility,willChange",
        });
      }
    }, rootRef);
    return () => context.revert();
  }, [dashboardReady, modeTransition, rootRef]);
}
