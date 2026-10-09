import { useState } from 'react';
import { useNoteTheme } from '../themes/Themes';
import { ThemeBoundary, ThemeFrame, ThemeTemplate } from '../themes/ThemeTemplate';
import { NoteBody } from './NoteBody';
import { NoteMetadata, NoteWork, noteDate } from './NoteList';
import { visibilityLabels, type Note } from './shared';

export function NoteReading({ note, workTitle, themed }: { note: Note; workTitle?: string; themed: boolean }) {
  const release = useNoteTheme();
  const presentation = themed ? release?.manifest.notes_theme?.presentation : undefined;
  const [revealed, setRevealed] = useState(false);
  const work = themed ? <NoteWork title={workTitle} linked={!!note.anime_id} /> : null;
  const meta = <p className="memory-meta">{themed ? noteDate(note) : note.occurred_on || '不记日期'} · {visibilityLabels[note.visibility]}</p>;
  const fallback = <>{work}{meta}<NoteBody note={note} /></>;
  const content = presentation && release ? <ThemeBoundary key={release.digest} fallback={fallback}><ThemeFrame release={release}><ThemeTemplate release={release} source={presentation.reader} slots={{
    work: <span className="theme-work">{note.anime_id ? workTitle ?? '正在读取作品…' : '随记'}</span>, metadata: <NoteMetadata note={note} />, body: <NoteBody note={note} />,
  }} /></ThemeFrame></ThemeBoundary> : fallback;
  // The disclosure gate is outside theme CSS and body/media do not mount until
  // the user opens it. A template cannot reveal hidden spoilers using selectors.
  return note.spoiler ? <>{!revealed && <>{work}{meta}</>}<details open={revealed} onToggle={event => setRevealed(event.currentTarget.open)}><summary>展开剧透内容</summary>{revealed && content}</details></> : content;
}
