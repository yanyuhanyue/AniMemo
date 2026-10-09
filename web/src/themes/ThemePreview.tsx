import { ThemeBoundary, ThemeFrame, ThemeTemplate, type ThemeRelease, type ThemeScope } from './ThemeTemplate';

export function ThemePreview({ release, scope = 'private.notes', preview = 'cards' }: { release?: ThemeRelease; scope?: ThemeScope; preview?: 'cards' | 'list' | 'detail' }) {
  const fallback = scope === 'private.journal' ? <article className="entry-card"><h3>给夏天的来信</h3><p className="muted">TV 动画 · 看过 · 仅自己</p><p>还记得散场时的心情，具体哪一天已经模糊。</p><span className="note-sheet-tag">#温柔的故事</span></article> : <article className="note-sheet"><span className="note-sheet-work">喜欢的那部作品</span><h3>把喜欢的故事留在纸上</h3><p className="note-sheet-excerpt">故事结束后，仍有一些画面留在心里。翻开这一页，再读一读当时的心情。</p><span className="note-sheet-tag">#温柔的故事</span></article>;
  if (scope === 'private.journal' && release?.manifest.journal_theme) {
    const p = release.manifest.journal_theme.presentation;
    const slots = {
      title: <h3>给夏天的来信</h3>, original: <p>Letters to Summer · 示例作品</p>,
      metadata: <div className="theme-entry-metadata"><span>TV 动画</span><span>看过，细节未记</span><span className="theme-visibility">仅自己</span><span className="theme-rating">8.5 分</span></div>,
      poster: <div className="theme-cover-empty">作品海报</div>, notes: <p>还记得散场时的心情，具体哪一天已经模糊。</p>,
      tags: <div className="theme-entry-tags"><span className="theme-tag">#温柔的故事</span></div>, facts: <p className="theme-entry-facts">观看细节还没有补记</p>,
      recollection: <p className="theme-recollection">还记得散场时的心情，具体哪一天已经模糊。想起更多细节时，再补上也不迟。</p>,
      memory: <span className="theme-memory-link">翻开作品记忆 →</span>, read: <span className="theme-read">打开详情 →</span>, actions: <span className="theme-read">留点回忆</span>,
    };
    const items = <ThemeTemplate release={release} scope={scope} source={preview === 'list' ? p.row : p.card} slots={slots} />;
    return <ThemeBoundary key={release.digest} fallback={fallback}><ThemeFrame release={release} scope={scope}><ThemeTemplate release={release} scope={scope} source={preview === 'detail' ? p.detail : p.list} slots={preview === 'detail' ? slots : { items }} /></ThemeFrame></ThemeBoundary>;
  }
  const p = release?.manifest.notes_theme?.presentation;
  if (!p || !release) return fallback;
  const items = ['把喜欢的故事留在纸上', '某个夏天留下的片段'].map(title => <ThemeTemplate key={title} release={release} source={p.card} slots={{
    title: <h3>{title}</h3>, excerpt: <p>故事结束后，仍有一些画面留在心里。翻开这一页，再读一读当时的心情。</p>, work: <span>喜欢的那部作品</span>,
    metadata: <div className="theme-metadata"><span>日期未记</span><span className="theme-visibility">仅自己</span><span className="theme-tag">#温柔的故事</span></div>, read: <span className="theme-read">阅读全文 →</span>,
  }} />);
  return <ThemeBoundary key={release.digest} fallback={fallback}><ThemeFrame release={release}><ThemeTemplate release={release} source={p.list} slots={{ items }} /></ThemeFrame></ThemeBoundary>;
}
