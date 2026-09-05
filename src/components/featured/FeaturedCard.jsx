import { useRef } from "react";
import { TagChip } from "../TagChip.jsx";
import { FeaturedDateBadge } from "./FeaturedDateBadge.jsx";
import { FeaturedScoreMeter } from "./FeaturedScoreMeter.jsx";
import { fallbackAvatarImage, fallbackPosterImage } from "../../lib/mediaAssets.js";

function formatAnimeTitle(title) {
  const value = String(title || "未命名番剧").trim();
  const unwrapped = value.replace(/^《+/, "").replace(/》+$/, "");
  return `《${unwrapped}》`;
}

export function FeaturedCard({ column, onOpen }) {
  const cardRef = useRef(null);
  const animeTitle = formatAnimeTitle(column.anime?.title || column.title);
  const animeJapaneseTitle = String(column.anime?.japaneseTitle || "日文原名待补充").trim();
  const summaryParagraphs = (Array.isArray(column.summary) ? column.summary : String(column.summary || "").split(/\n{2,}/))
    .map((paragraph) => paragraph.trim())
    .filter(Boolean);
  const open = () => onOpen?.(column, cardRef.current);
  const handleKeyDown = (event) => {
    if (!["Enter", " "].includes(event.key)) return;
    event.preventDefault();
    open();
  };

  return (
    <article ref={cardRef} className="featured-card" role="button" tabIndex={0} aria-haspopup="dialog" onClick={open} onKeyDown={handleKeyDown} aria-label={`打开番剧档案：${animeTitle}`}>
      <div className="featured-card__cover">
        <img src={column.cover} alt={`${animeTitle} 封面`} loading="lazy" decoding="async" onError={fallbackPosterImage} />
        <span className="featured-card__status">{column.statusLabel}</span>
      </div>
      <div className="featured-card__content">
        <div className="featured-author"><img src={column.authorAvatar} alt="" onError={fallbackAvatarImage} /><span>由 <b>{column.author}</b> 撰写</span></div>
        <div className="featured-card__titles">
          <h2>{animeTitle}</h2>
          <p className="featured-card__jp" lang="ja">{animeJapaneseTitle}</p>
        </div>
        <div className="featured-card__facts">
          <FeaturedDateBadge period={column.period} />
          <FeaturedScoreMeter score={column.score} />
        </div>
        <div className="featured-card__summary">
          {summaryParagraphs.map((paragraph, index) => <p key={`${column.slug}-summary-${index}`}>{paragraph}</p>)}
        </div>
        <div className="featured-card__tags">{column.tags.slice(0, 5).map((tag) => <TagChip tag={tag} key={tag} />)}</div>
      </div>
    </article>
  );
}
