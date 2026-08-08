import { useEffect, useState } from "react";

const TARGET_WIDTH = 1120;
const TARGET_HEIGHT = 1075;
const HORIZONTAL_SAFE_SPACE = 64;
const VERTICAL_SAFE_SPACE = 80;
const MOBILE_BREAKPOINT = 760;

function calculateModalSize() {
  if (typeof window === "undefined") {
    return { width: TARGET_WIDTH, height: TARGET_HEIGHT, scale: 1 };
  }

  if (window.innerWidth <= MOBILE_BREAKPOINT) {
    return {
      width: Math.max(280, window.innerWidth - 20),
      height: Math.max(480, window.innerHeight - 20),
      scale: 1,
    };
  }

  const availableWidth = window.innerWidth - HORIZONTAL_SAFE_SPACE;
  const availableHeight = window.innerHeight - VERTICAL_SAFE_SPACE;
  const scale = Math.min(
    1,
    availableWidth / TARGET_WIDTH,
    availableHeight / TARGET_HEIGHT,
  );

  return {
    width: Math.floor(TARGET_WIDTH * scale),
    height: Math.floor(TARGET_HEIGHT * scale),
    scale,
  };
}

export function useModalViewportSize() {
  const [size, setSize] = useState(calculateModalSize);

  useEffect(() => {
    let frameId = 0;

    const update = () => {
      frameId = 0;
      setSize(calculateModalSize());
    };

    const handleResize = () => {
      if (frameId) return;
      frameId = window.requestAnimationFrame(update);
    };

    window.addEventListener("resize", handleResize, { passive: true });

    return () => {
      window.removeEventListener("resize", handleResize);
      if (frameId) window.cancelAnimationFrame(frameId);
    };
  }, []);

  return size;
}
