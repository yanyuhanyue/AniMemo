import { validateTrustedPosterUrl } from "./posterSources.js";

export const DEFAULT_POSTERS = Object.freeze([
  "/assets/posters/poster-01.webp",
  "/assets/posters/poster-02.webp",
]);

export const [DEFAULT_POSTER] = DEFAULT_POSTERS;

export function isTrustedProviderPoster(value) {
  return Boolean(value) && validateTrustedPosterUrl(value) === "";
}

export function resolveDemoPoster(record) {
  const candidate = record?.posterUrl || record?.poster_url || record?.poster || "";
  return isTrustedProviderPoster(candidate) ? candidate : DEFAULT_POSTER;
}

export function resolveDemoIdentity(record) {
  const identity = record?.externalIdentity || record?.external_identity;
  if (!identity || String(identity.provider).toLowerCase() !== "bangumi") return null;
  const externalId = String(identity.external_id || identity.externalId || "").trim();
  return externalId ? { provider: "bangumi", external_id: externalId } : null;
}
