import { Icon } from "../Icon.jsx";
import { RatingDisplay } from "../RatingDisplay.jsx";
import { TagChip } from "../TagChip.jsx";
import { getSeasonFromPeriod, SeasonBadge } from "../featured/SeasonBadge.jsx";
import { getTagText, isOversizedTag, splitTagsIntoRows } from "../../lib/tagLayout.js";
import { usePosterReady } from "./usePosterReady.js";

const TAG_ROTATIONS = ["-2deg", "2deg"];

function getTagRotation(tag) {
  if (tag.kind === "status") return "0deg";
  return TAG_ROTATIONS[tag.rotationIndex % TAG_ROTATIONS.length];
}

function centerFocusedEntry(event) {
  if (!event.currentTarget.matches(":focus-visible")) return;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  event.currentTarget.scrollIntoView({
    block: "center",
    inline: "nearest",
    behavior: reducedMotion ? "auto" : "smooth",
  });
}

export function AnimeListRow({ record, rank, onOpen, catalogReady, variant = "default" }) {
  const {
    imageRef,
    posterFailed,
    onPosterLoad,
    onPosterError,
  } = usePosterReady(record.poster);
  const season = getSeasonFromPeriod(record.period);
  const listTags = [
    ...(record.statusLabel ? [{ label: record.statusLabel, kind: "status" }] : []),
    ...record.tags.map((label, rotationIndex) => ({ label, kind: "topic", rotationIndex })),
  ];
  const tagRows = splitTagsIntoRows(
    listTags,
    { maxItems: 3, maxVisualUnits: 16 },
  );

  const openFromKeyboard = (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    onOpen(record, event.currentTarget);
  };

  return (
    <article
      className={[
        "anime-list-row",
        "catalog-reveal-entry",
        rank % 2 === 0 ? "anime-list-row--alt" : "",
        catalogReady ? "is-ready" : "is-pending",
      ].filter(Boolean).join(" ")}
      onClick={(event) => onOpen(record, event.currentTarget)}
      onKeyDown={openFromKeyboard}
      onFocus={centerFocusedEntry}
      role="button"
      tabIndex="0"
      aria-label={variant === "editable" ? `编辑 ${record.title}` : `打开 ${record.title} 动漫档案`}
    >
      <div className="anime-list-row__content">
        <span className="anime-list-row__rank">{rank}</span>
        <div className="anime-list-row__poster">
          {!posterFailed ? (
            <img
              ref={imageRef}
              src={record.poster}
              alt=""
              loading="lazy"
              decoding="async"
              onLoad={onPosterLoad}
              onError={onPosterError}
            />
          ) : <span className="poster-fallback" aria-hidden="true">NO POSTER</span>}
        </div>
        <div className="anime-list-row__title">
          <strong className="anime-list-row__title-cn">{record.title}</strong>
          <small className="anime-list-row__title-jp">{record.japaneseTitle || "\u00a0"}</small>
        </div>
        <span className={`anime-list-row__season-hitbox anime-list-row__season-hitbox--${season}`}>
          <span className="anime-list-row__season-tilt">
            <SeasonBadge
              period={record.period}
              variant="pill"
              showSeasonName={false}
              className="season-badge--list-continuous"
            />
          </span>
        </span>
        <div className="anime-list-row__tags anime-list-row__tag-rows">
          {tagRows.map((row, rowIndex) => (
            <div className="anime-list-row__tag-row" key={`tag-row-${rowIndex}`}>
              {row.map((tag) => {
                const tagText = getTagText(tag);
                const tagKey = tag?.id ?? tag?.label ?? tagText;
                const isStatus = tag.kind === "status";

                return (
                  <span
                    className={[
                      "anime-list-row__tag-slot",
                      isStatus ? "anime-list-row__tag-slot--status" : "",
                      isOversizedTag(tagText) ? "anime-list-row__tag-slot--multiline" : "",
                    ].filter(Boolean).join(" ")}
                    key={`${record.id}:${tag.kind}:${tagKey}`}
                    style={{ "--tag-rotation": getTagRotation(tag) }}
                  >
                    <span className="anime-list-row__tag-tilt">
                      <TagChip
                        tag={tagText}
                        color={isStatus ? undefined : record.tagColors?.[tagText]}
                      />
                    </span>
                  </span>
                );
              })}
            </div>
          ))}
        </div>
        <RatingDisplay score={record.score} className="anime-list-row__rating" />
        <button
          className={`detail-button${variant === "editable" ? " detail-button--edit" : ""}`}
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            onOpen(record, event.currentTarget.closest(".anime-list-row"));
          }}
        >
          <span className="detail-button__visual"><Icon name={variant === "editable" ? "edit" : "layers"} /> {variant === "editable" ? "修改" : "详情"}</span>
        </button>
      </div>
    </article>
  );
}
