import { useState } from "react";
import { AnimePosterCard } from "./AnimePosterCard.jsx";
import { AddAnimePlaceholder } from "./AddAnimePlaceholder.jsx";
import { useCatalogReveal } from "./useCatalogReveal.js";

const POSTER_SHADOWS = ["#ff5f68", "#4ecbc4", "#ffe36e"];

export function AnimePosterWall({ records, onOpenDetail, ready, variant, onAddRecord }) {
  const [expandedTags, setExpandedTags] = useState(() => new Set());
  const revealDependency = records.map((record) => record.id).join(":");
  const wallRef = useCatalogReveal({ enabled: ready, dependency: revealDependency });

  const toggleTags = (recordId) => {
    setExpandedTags((current) => {
      const next = new Set(current);
      if (next.has(recordId)) next.delete(recordId);
      else next.add(recordId);
      return next;
    });
  };

  return (
    <div className="anime-poster-wall" ref={wallRef}>
      {records.map((record, index) => (
        <AnimePosterCard
          key={record.id}
          record={record}
          shadowColor={POSTER_SHADOWS[index % POSTER_SHADOWS.length]}
          expanded={expandedTags.has(record.id)}
          onToggleTags={toggleTags}
          onOpen={onOpenDetail}
          catalogReady={ready}
          variant={variant}
        />
      ))}
      {onAddRecord && <AddAnimePlaceholder layout="grid" onAdd={onAddRecord} />}
    </div>
  );
}
