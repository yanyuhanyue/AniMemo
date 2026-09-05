import { useEffect, useRef } from "react";
import gsap from "gsap";
import { ANIMEMO_AVATAR_PATH } from "../lib/mediaAssets.js";

const HERO_ART = [
  { className: "one", src: ANIMEMO_AVATAR_PATH, drift: 10, duration: 2.8 },
  { className: "two", brandMark: true, drift: -8, duration: 3.25 },
];

function UniverseHeroPiece({ item }) {
  const rootRef = useRef(null);
  const floatRef = useRef(null);

  useEffect(() => {
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const context = gsap.context(() => {
      if (!reducedMotion) {
        gsap.to(floatRef.current, {
          y: item.drift,
          duration: item.duration,
          repeat: -1,
          yoyo: true,
          ease: "sine.inOut",
        });
      }
    }, rootRef);

    return () => context.revert();
  }, [item.drift, item.duration]);

  return (
    <div className={`universe-art-item universe-art-item--${item.className}`} ref={rootRef}>
      <div className="universe-art-float-layer" ref={floatRef}>
        <div className="universe-art-hover-layer">
          <figure className="universe-art-image-layer">
            {item.brandMark ? (
              <div className="universe-art-brand-mark">
                <span aria-hidden="true">✦</span>
                <i aria-hidden="true" />
                <b aria-hidden="true" />
              </div>
            ) : <img src={item.src} alt="" />}
          </figure>
        </div>
      </div>
    </div>
  );
}

export function UniverseHeroArt({ socialHandle = "X: @ANIMEMO" }) {
  return (
    <div className="universe-hero__art" aria-hidden="true">
      <div className="universe-art-spark">✦</div>
      {HERO_ART.map((item) => <UniverseHeroPiece item={item} key={item.className} />)}
      <span className="universe-art-handle">{socialHandle}</span>
    </div>
  );
}
