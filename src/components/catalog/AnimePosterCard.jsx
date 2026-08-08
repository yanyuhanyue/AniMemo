import { Icon } from "../Icon.jsx";
import { RatingDisplay } from "../RatingDisplay.jsx";
import { TagChip } from "../TagChip.jsx";
import { SeasonBadge } from "../featured/SeasonBadge.jsx";
import { usePosterReady } from "./usePosterReady.js";

function centerFocusedEntry(event) {
  if (!event.currentTarget.matches(":focus-visible")) return;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  event.currentTarget.scrollIntoView({
    block: "center",
    inline: "nearest",
    behavior: reducedMotion ? "auto" : "smooth",
  });
}

export function AnimePosterCard({ record, shadowColor, expanded, onToggleTags, onOpen, catalogReady, variant = "default" }) {
  const visibleTags = expanded ? record.tags : record.tags.slice(0, 3);
  const {
    imageRef,
    posterFailed,
    onPosterLoad,
    onPosterError,
  } = usePosterReady(record.poster);

  const openCard = (event) => {
    if (event.target.closest(".anime-poster-card__tag-more, .anime-poster-card__edit")) return;
    onOpen(record, event.currentTarget);
  };

  const openFromKeyboard = (event) => {
    if (event.target !== event.currentTarget || !["Enter", " "].includes(event.key)) return;
    event.preventDefault();
    openCard(event);
  };

  return (
    <div
      className={[
        "anime-poster-card-entry",
        "catalog-reveal-entry",
        catalogReady ? "is-ready" : "is-pending",
      ].filter(Boolean).join(" ")}
      style={{ "--poster-shadow-color": shadowColor }}
    >
      <article
        className={`anime-poster-card__interaction${expanded ? " is-expanded" : ""}`}
        onClick={openCard}
        onKeyDown={openFromKeyboard}
        onFocus={centerFocusedEntry}
        role="button"
        tabIndex="0"
        aria-label={variant === "editable" ? `编辑 ${record.title}` : `打开 ${record.title} 动漫档案`}
      >
        <div className="anime-poster-card__content">
          <div className="anime-poster-card__media">
            {variant === "editable" && (
              <button
                className="anime-poster-card__edit"
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  onOpen(record, event.currentTarget.closest(".anime-poster-card__interaction"));
                }}
                aria-label={`修改 ${record.title}`}
              >
                <Icon name="edit" />
              </button>
            )}
            {!posterFailed ? (
              <img
                ref={imageRef}
                src={record.poster}
                alt={`${record.title} 海报`}
                loading="lazy"
                decoding="async"
                onLoad={onPosterLoad}
                onError={onPosterError}
              />
            ) : <span className="poster-fallback" aria-hidden="true">NO POSTER</span>}
            <RatingDisplay score={record.score} compact singleStar className="anime-poster-card__score" />
          </div>
          <div className="anime-poster-card__meta">
            <SeasonBadge
              period={record.period}
              variant="inline"
              showSeasonName={false}
              className="anime-poster-card__period"
            />
            <span className={`anime-poster-card__status anime-poster-card__status--${record.status}`}>
              {record.statusLabel}
            </span>
          </div>
          <div className="anime-poster-card__copy">
            <strong>{record.title}</strong>
            <small>{record.japaneseTitle || "\u00a0"}</small>
          </div>
          <div className="anime-poster-card__tags">
            {visibleTags.map((tag) => (
              <TagChip tag={tag} color={record.tagColors?.[tag]} key={tag} />
            ))}
            {record.tags.length > 3 && (
              <button
                className="anime-poster-card__tag-more"
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  onToggleTags(record.id);
                }}
                aria-expanded={expanded}
                aria-label={expanded ? `收起 ${record.title} 的标签` : `展开 ${record.title} 的全部标签`}
              >
                <span className="anime-poster-card__tag-more-visual" aria-hidden="true">
                  {expanded ? "−" : "…"}
                </span>
              </button>
            )}
          </div>
        </div>
      </article>
    </div>
  );
}
