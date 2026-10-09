import { ThemeBoundary, ThemeFrame, ThemeTemplate, type ThemeRelease } from './ThemeTemplate';

export function ThemePreview({ release }: { release?: ThemeRelease }) {
  const fallback = <article className="note-sheet"><span className="note-sheet-work">喜欢的那部作品</span><h3>把喜欢的故事留在纸上</h3><p className="note-sheet-excerpt">故事结束后，仍有一些画面留在心里。翻开这一页，再读一读当时的心情。</p><span className="note-sheet-tag">#温柔的故事</span></article>;
  const p = release?.manifest.notes_theme?.presentation;
  if (!p || !release) return fallback;
  const items = ['把喜欢的故事留在纸上', '某个夏天留下的片段'].map(title => <ThemeTemplate key={title} release={release} source={p.card} slots={{
    title: <h3>{title}</h3>, excerpt: <p>故事结束后，仍有一些画面留在心里。翻开这一页，再读一读当时的心情。</p>, work: <span>喜欢的那部作品</span>,
    metadata: <div className="theme-metadata"><span>日期未记</span><span className="theme-visibility">仅自己</span><span className="theme-tag">#温柔的故事</span></div>, read: <span className="theme-read">阅读全文 →</span>,
  }} />);
  return <ThemeBoundary key={release.digest} fallback={fallback}><ThemeFrame release={release}><ThemeTemplate release={release} source={p.list} slots={{ items }} /></ThemeFrame></ThemeBoundary>;
}
