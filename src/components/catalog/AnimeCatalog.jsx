import { AnimeListView } from "./AnimeListView.jsx";
import { AnimePosterWall } from "./AnimePosterWall.jsx";

export function AnimeCatalog({
  records,
  viewMode,
  onOpenDetail,
  sort,
  onSortChange,
  ready,
  variant = "default",
  onAddRecord,
}) {
  return (
    <div className={`anime-catalog anime-catalog--${viewMode} anime-catalog--${variant}`} aria-busy={!ready}>
      {viewMode === "list" ? (
        <AnimeListView
          records={records}
          onOpenDetail={onOpenDetail}
          sort={sort}
          onSortChange={onSortChange}
          ready={ready}
          variant={variant}
          onAddRecord={onAddRecord}
        />
      ) : (
        <AnimePosterWall records={records} onOpenDetail={onOpenDetail} ready={ready} variant={variant} onAddRecord={onAddRecord} />
      )}
    </div>
  );
}
