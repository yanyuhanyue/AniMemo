import gsap from "gsap";

export function pressBeforeOpen(target, open) {
  if (!target || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    open();
    return;
  }
  if (target.dataset.modalPressing === "true") return;

  target.dataset.modalPressing = "true";
  gsap.killTweensOf(target);
  let committed = false;
  let fallbackTimer;
  let restoreTimer;

  const clearPressState = () => {
    window.clearTimeout(restoreTimer);
    delete target.dataset.modalPressing;
    gsap.set(target, { clearProps: "transform" });
  };

  const restore = () => {
    delete target.dataset.modalPressing;
    gsap.to(target, {
      scale: 1,
      duration: 0.08,
      ease: "power2.out",
      clearProps: "transform",
      onComplete: clearPressState,
    });
    restoreTimer = window.setTimeout(clearPressState, 220);
  };

  const commit = () => {
    if (committed) return;
    committed = true;
    window.clearTimeout(fallbackTimer);
    open();
    restore();
  };

  gsap.timeline({ onComplete: commit })
    .to(target, {
      scale: 0.95,
      transformOrigin: "50% 50%",
      duration: 0.1,
      ease: "power2.inOut",
    });
  gsap.ticker.wake();
  fallbackTimer = window.setTimeout(commit, 260);
}
