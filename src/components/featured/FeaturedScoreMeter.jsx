import { getFeaturedScoreTier, normalizeRating } from "../../lib/rating.js";

export function FeaturedScoreMeter({ score }) {
  const normalized = normalizeRating(score);
  const tier = getFeaturedScoreTier(normalized);
  const percentage = normalized === null ? 0 : normalized * 10;
  const displayValue = normalized === null ? "待定" : normalized.toFixed(1);

  return (
    <div
      className={`featured-score-meter featured-score-meter--${tier}`}
      aria-label={normalized === null ? "主观评分待定" : `主观评分 ${displayValue} 分，满分 10 分`}
    >
      <div className="featured-score-meter__header">
        <span className="featured-score-meter__label">主观评分</span>
        <span className="featured-score-meter__value">
          <span className="featured-score-meter__star" aria-hidden="true">★</span>
          <strong className="featured-score-meter__number">{displayValue}</strong>
          {normalized !== null && (
            <span className="featured-score-meter__suffix">
              <span aria-hidden="true">/</span>
              <strong>10</strong>
            </span>
          )}
        </span>
      </div>
      <div className="featured-score-meter__track" aria-hidden="true">
        <span
          className="featured-score-meter__fill"
          style={{ width: `${percentage}%` }}
        />
      </div>
    </div>
  );
}
