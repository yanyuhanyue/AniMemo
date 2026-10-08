import { MemoryImage, type Note } from './shared';

// The caller controls spoiler disclosure; private images keep their authenticated URL.
export function NoteBody({ note }: { note: Note }) {
  return <>
    <p className="memory-prose">{note.body}</p>
    {note.anchor.quote && <blockquote>{note.anchor.quote}</blockquote>}
    {note.anchor.scene && <p className="muted">{note.anchor.scene}</p>}
    {note.anchor.timestamp_seconds !== null && <p>定位：{Math.floor(note.anchor.timestamp_seconds / 60)}:{String(note.anchor.timestamp_seconds % 60).padStart(2, '0')}</p>}
    <div className="memory-reading-photos">{note.media_ids.map(id => <MemoryImage key={id} id={id} />)}</div>
  </>;
}
