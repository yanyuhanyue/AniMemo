import { Button } from "../components/ui/Button";
import { Icon } from "../components/ui/Icon";
import { CoverImage } from "../journal/CoverImage";
import type { components } from "../api/schema";
import { MemoryImage, visibilityLabels, type Note } from "./shared";

const visibilityIcons = { private: "lock", unlisted: "link", public: "globe" } as const;

export function noteDate(note: Note) {
  if (!note.occurred_on || note.time_precision === "unknown") return "日期未记";
  const parts = note.occurred_on.split("-");
  const date = `${parts[0]}年${parts[1] ? `${Number(parts[1])}月` : ""}${parts[2] ? `${Number(parts[2])}日` : ""}`;
  return `${note.time_precision === "approximate" ? "约 " : ""}${date}`;
}

export function NoteWork({ title, linked }: { title?: string; linked: boolean }) {
  return <span className="note-sheet-work"><Icon name="book" />{linked ? title ?? "正在读取作品…" : "随记"}</span>;
}

export function NoteList({ notes, titles, works, onRead }: {
  notes: Note[];
  titles: Map<string, string>;
  works: components["schemas"]["MemoryReference"][];
  onRead: (note: Note) => void;
}) {
  if (!notes.length) return null;
  const posters = new Map(works.flatMap(work => work.available && work.entry_id && work.cover_revision
    ? [[work.id, { id: work.entry_id, title: work.title, cover_revision: work.cover_revision }] as const] : []));
  return <div className="note-sheet">
    {notes.map(note => <article key={note.id} className="note-sheet-entry" data-highlight={note.highlight || undefined}>
      <div className="note-sheet-margin">
        <div className="note-sheet-margin-meta">
          <span className="note-sheet-date-label">回忆日期</span><span>{noteDate(note)}</span>
          {note.highlight && <span className="note-sheet-kept"><Icon name="star" />珍藏</span>}
        </div>
        {posters.has(note.anime_id) && <div className="note-sheet-poster">
          <CoverImage card entry={posters.get(note.anime_id)!} />
        </div>}
      </div>
      <div className="note-sheet-content">
        <div className="note-sheet-topline">
          <NoteWork title={titles.get(note.anime_id)} linked={!!note.anime_id} />
        </div>
        <div className="note-sheet-article">
          <div className="note-sheet-text">
            <h3><Button className="memory-title-button" onClick={() => onRead(note)}>{note.title}</Button></h3>
            {note.spoiler ? <p className="note-sheet-spoiler">含剧透 · 阅读时展开</p> : <>
              {note.body && <p className="note-sheet-excerpt">{note.body}</p>}
              {!note.body && note.anchor.quote && <blockquote>{note.anchor.quote}</blockquote>}
            </>}
          </div>
          {note.media_ids[0] && !note.spoiler && <div className="note-sheet-picture">
            <MemoryImage id={note.media_ids[0]} small caption={`${note.title}的配图`} />
            {note.media_ids.length > 1 && <span>{note.media_ids.length} 张配图</span>}
          </div>}
        </div>
        <footer className="note-sheet-footer">
          <div className="note-sheet-details">
            <span className="note-sheet-visibility">
              <span className="sr-only">可见范围：</span><Icon name={visibilityIcons[note.visibility]} />
              {visibilityLabels[note.visibility]}
            </span>
            {note.tags.map(tag => <span className="note-sheet-tag" key={tag}>#{tag}</span>)}
          </div>
          <Button className="text-button note-sheet-read" aria-label={`阅读 ${note.title}`} onClick={() => onRead(note)}>阅读全文<Icon name="arrow" /></Button>
        </footer>
      </div>
    </article>)}
  </div>;
}
