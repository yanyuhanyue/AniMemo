import { useCallback, useEffect, useRef, useState } from "react";

export function usePosterReady(source, timeout = 1600) {
  const imageRef = useRef(null);
  const [posterSettled, setPosterSettled] = useState(false);
  const [posterFailed, setPosterFailed] = useState(false);

  const settlePoster = useCallback(() => setPosterSettled(true), []);
  const failPoster = useCallback(() => {
    setPosterFailed(true);
    setPosterSettled(true);
  }, []);

  useEffect(() => {
    setPosterSettled(false);
    setPosterFailed(false);

    const image = imageRef.current;
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
