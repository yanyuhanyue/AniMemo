import { useId } from "react";
import { getFilledStarCount, getRatingTier, normalizeRating } from "../lib/rating.js";
import { Icon } from "./Icon.jsx";

function RatingStar({ filled, position }) {
  return (
    <Icon
      name="star"
      aria-hidden="true"
      className={`rating-display__star${filled ? " is-filled" : ""}`}
      data-star-position={position}
    />
  );
}

export function RatingDisplay({
  score,
  className = "",
  compact = false,
  precision = 1,
  showStars = true,
  singleStar = false,
  label = "综合评分",
}) {
  const normalized = normalizeRating(score);
  const tier = getRatingTier(normalized);
  const filledStars = getFilledStarCount(normalized);
  const spectrumId = `rating-star-spectrum-${useId().replace(/:/g, "")}`;
  const classes = [
    "rating-display",
    `rating--${tier}`,
    compact ? "rating-display--compact" : "",
    singleStar ? "rating-display--single-star" : "",
    className,
  ].filter(Boolean).join(" ");

  return (
    <span
      className={classes}
      aria-label={normalized === null ? `${label}待定` : `${label}${normalized.toFixed(1)}分`}
      style={tier === "rainbow" ? { "--rating-star-spectrum": `url(#${spectrumId})` } : undefined}
    >
      {tier === "rainbow" && (
        <svg className="rating-display__spectrum-defs" aria-hidden="true" focusable="false">
          <defs>
            <linearGradient id={spectrumId} x1="0%" y1="50%" x2="100%" y2="50%">
              <stop offset="0%" stopColor="#ec4899" />
              <stop offset="50%" stopColor="#a855f7" />
              <stop offset="100%" stopColor="#6366f1" />
            </linearGradient>
          </defs>
        </svg>
      )}
      <span className="rating-display__number">
        <strong className="rating-display__value">{normalized === null ? "待定" : normalized.toFixed(precision)}</strong>
        {normalized !== null && <small className="rating-display__suffix">/10</small>}
      </span>
      {showStars && (
        <span className="rating-display__stars" aria-hidden="true">
          {singleStar ? (
            <RatingStar
              filled={normalized !== null}
              position={1}
            />
          ) : Array.from({ length: 5 }, (_, index) => {
              const filled = index < filledStars;
              return (
                <RatingStar
                  key={index}
                  filled={filled}
                  position={index + 1}
                />
              );
            })}
        </span>
      )}
    </span>
  );
}
