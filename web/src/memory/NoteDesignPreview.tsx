import { Button } from "../components/ui/Button";
import { Icon } from "../components/ui/Icon";
import { MemoryImage, visibilityLabels, type Note } from "./shared";

// Temporary, URL-selected alternatives using the existing note actions.
export const noteDesigns = {
  review: "番剧影评",
  paper: "纸本札记",
  classic: "旧版风格",
  hybrid: "融合版",
} as const;
export type NoteDesign = keyof typeof noteDesigns;
const visibilityIcons = { private: "lock", unlisted: "link", public: "globe" } as const;

export function currentNoteDesign(): NoteDesign | undefined {
  const value = new URLSearchParams(location.search).get("notes_ui");
  return value && Object.hasOwn(noteDesigns, value) ? value as NoteDesign : undefined;
}

export function NoteDesignSwitcher({ design, onChange }: {
  design: NoteDesign;
  onChange: (design: NoteDesign | undefined) => void;
}) {
  return <aside className="note-design-switcher" aria-label="札记版式试用">
    <div><strong>札记版式试用</strong><span>共用同一份札记，保存会生效</span></div>
    <div className="note-design-choices" role="group" aria-label="选择札记版式">
      {Object.entries(noteDesigns).map(([key, label]) => <Button key={key}
        aria-pressed={design === key} onClick={() => onChange(key as NoteDesign)}>{label}</Button>)}
    </div>
    <Button className="text-button" onClick={() => onChange(undefined)}>返回现有版</Button>
  </aside>;
}

export function noteDate(note: Note) {
  if (!note.occurred_on || note.time_precision === "unknown") return "日期未记";
  const parts = note.occurred_on.split("-");
  const date = `${parts[0]}年${parts[1] ? `${Number(parts[1])}月` : ""}${parts[2] ? `${Number(parts[2])}日` : ""}`;
  return `${note.time_precision === "approximate" ? "约 " : ""}${date}`;
}

export function NoteWork({ title, linked }: { title?: string; linked: boolean }) {
  return <span className="preview-note-work"><Icon name="book" />{linked ? title ?? "正在读取作品…" : "随记"}</span>;
}

export function NotePreviewList({ notes, design, titles, onRead }: {
  notes: Note[];
  design: NoteDesign;
  titles: Map<string, string>;
  onRead: (note: Note) => void;
}) {
  const paperLayout = design === "paper" || design === "hybrid";
  return <div className={`note-preview-list note-preview-${design}${design === "hybrid" ? " note-preview-paper" : ""}`}>
    {notes.map(note => <article key={note.id} className="preview-note" data-highlight={note.highlight || undefined}>
      {paperLayout && <div className="preview-note-margin">
        <span>回忆日期</span><span>{noteDate(note)}</span>
        {note.highlight && <span className="preview-note-kept"><Icon name="star" />珍藏</span>}
      </div>}
      <div className="preview-note-content">
        <div className="preview-note-topline">
          <NoteWork title={titles.get(note.anime_id)} linked={!!note.anime_id} />
          {!paperLayout && note.highlight && <span className="preview-note-kept"><Icon name="star" />珍藏</span>}
        </div>
        <div className="preview-note-article">
          <div className="preview-note-text">
            <h3><Button className="memory-title-button" onClick={() => onRead(note)}>{note.title}</Button></h3>
            {note.spoiler ? <p className="preview-note-spoiler">含剧透 · 阅读时展开</p> : <>
              {note.body && <p className="preview-note-excerpt">{paperLayout ? note.body : note.body.replace(/\s+/g, " ").trim()}</p>}
              {!note.body && note.anchor.quote && <blockquote>{note.anchor.quote}</blockquote>}
            </>}
          </div>
          {note.media_ids[0] && !note.spoiler && <div className="preview-note-picture">
            <MemoryImage id={note.media_ids[0]} small caption={`${note.title}的配图`} />
            {note.media_ids.length > 1 && <span>{note.media_ids.length} 张配图</span>}
          </div>}
        </div>
        <footer className="preview-note-footer">
          <div className="preview-note-details">
            {!paperLayout && <span>{noteDate(note)}</span>}
            <span className={design === "hybrid" ? "preview-note-visibility" : undefined}>
              {design === "hybrid" && <><span className="sr-only">可见范围：</span><Icon name={visibilityIcons[note.visibility]} /></>}
              {visibilityLabels[note.visibility]}
            </span>
            {note.tags.map(tag => <span className="preview-note-tag" key={tag}>#{tag}</span>)}
          </div>
          <Button className="text-button preview-note-read" aria-label={`阅读 ${note.title}`} onClick={() => onRead(note)}>阅读全文<Icon name="arrow" /></Button>
        </footer>
      </div>
    </article>)}
  </div>;
}
