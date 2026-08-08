import { normalizeRating } from "../../lib/rating.js";

export function FeaturedModalScore({ score }) {
  const normalized = normalizeRating(score);

  if (normalized === null) {
    return <span className="featured-modal-score is-pending">待定</span>;
  }

  const formatted = normalized.toFixed(1);

  return (
    <span
      className="featured-modal-score"
      aria-label={`综合评分 ${formatted} 分，满分 10 分`}
    >
      <strong>{formatted}</strong>
      <span aria-hidden="true">/</span>
      <strong>10</strong>
    </span>
  );
}
