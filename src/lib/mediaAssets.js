export const ANIMEMO_AVATAR_PATH = "/assets/avatar.png";
export const ANIMEMO_POSTER_FALLBACK_PATH = "/assets/posters/poster-01.webp";

const NUMBERED_BUNDLED_POSTER_PATTERN = /^\/assets\/posters\/poster-(\d{2})\.webp$/;

export function normalizeBundledPosterPath(value, fallback = ANIMEMO_POSTER_FALLBACK_PATH) {
  const path = String(value || "").trim();
  const match = NUMBERED_BUNDLED_POSTER_PATTERN.exec(path);
  if (match && match[1] !== "01") return fallback;
  return path || fallback;
}

function fallbackUrl(path) {
  if (typeof window === "undefined") return path;
  return new URL(path, window.location.origin).href;
}

export function applyImageFallback(event, path) {
  const image = event?.currentTarget;
  if (!image) return false;
  const resolved = fallbackUrl(path);
  if (image.dataset.animemoFallback === path || image.src === resolved) {
    image.hidden = true;
    return false;
  }
  image.dataset.animemoFallback = path;
  image.src = path;
  return true;
}

export function fallbackPosterImage(event) {
  return applyImageFallback(event, ANIMEMO_POSTER_FALLBACK_PATH);
}

export function fallbackAvatarImage(event) {
  return applyImageFallback(event, ANIMEMO_AVATAR_PATH);
}
