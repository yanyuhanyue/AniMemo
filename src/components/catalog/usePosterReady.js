import { useCallback, useEffect, useRef, useState } from "react";
import { ANIMEMO_POSTER_FALLBACK_PATH } from "../../lib/mediaAssets.js";

export function usePosterReady(source, timeout = 1600) {
  const imageRef = useRef(null);
  const [posterSettled, setPosterSettled] = useState(false);
  const [posterFailed, setPosterFailed] = useState(false);

  const settlePoster = useCallback(() => setPosterSettled(true), []);
  const failPoster = useCallback(() => {
    const image = imageRef.current;
    const fallbackUrl = typeof window === "undefined"
      ? ANIMEMO_POSTER_FALLBACK_PATH
      : new URL(ANIMEMO_POSTER_FALLBACK_PATH, window.location.origin).href;
    if (image && image.src !== fallbackUrl && image.dataset.animemoFallback !== "true") {
      image.dataset.animemoFallback = "true";
      setPosterSettled(false);
      image.src = ANIMEMO_POSTER_FALLBACK_PATH;
      return;
    }
    setPosterFailed(true);
    setPosterSettled(true);
  }, []);

  useEffect(() => {
    setPosterSettled(false);
    setPosterFailed(false);

    const image = imageRef.current;
    if (image) delete image.dataset.animemoFallback;
    if (image?.complete) {
      if (image.naturalWidth > 0) settlePoster();
      else failPoster();
    }

    const timer = window.setTimeout(settlePoster, timeout);
    return () => window.clearTimeout(timer);
  }, [failPoster, settlePoster, source, timeout]);

  return {
    imageRef,
    posterSettled,
    posterFailed,
    onPosterLoad: settlePoster,
    onPosterError: failPoster,
  };
}
