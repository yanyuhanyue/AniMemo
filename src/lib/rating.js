export function normalizeRating(rawScore) {
  if (rawScore === null || rawScore === undefined || rawScore === "") return null;
  const score = Number(rawScore);
  if (!Number.isFinite(score)) return null;
  return Math.min(10, Math.max(0, Math.round(score * 10) / 10));
}

export function getRatingTier(rawScore) {
  const score = normalizeRating(rawScore);
  if (score === null) return "unrated";
  if (score >= 9.9) return "rainbow";
  if (score >= 9.5) return "red";
  if (score >= 9) return "orange-red";
  return "black";
}

export function getFeaturedScoreTier(rawScore) {
  const score = normalizeRating(rawScore);
  if (score === null) return "pending";
  if (score >= 9.9) return "gradient";
  if (score >= 9.5) return "orange-red";
  return "yellow";
}

export function getFilledStarCount(rawScore) {
  const score = normalizeRating(rawScore);
  if (score === null) return 0;
  return Math.min(5, Math.max(0, Math.round(score / 2)));
}
