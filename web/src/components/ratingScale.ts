// Preserve the original AniMemo's score bands across every presentation.
export function ratingTier(score: number | null) {
  if (score === null) return 'unrated';
  score = Math.round(score * 10) / 10;
  if (score >= 9.9) return 'rainbow';
  if (score >= 9.5) return 'red';
  if (score >= 9) return 'orange-red';
  return 'black';
}
