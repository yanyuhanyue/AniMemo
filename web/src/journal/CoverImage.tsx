import { useState } from 'react';
import type { Entry } from '../api/client';

export function CoverImage({ entry, card = false }: { entry: Entry; card?: boolean }) {
  const [failed, setFailed] = useState('');
  if (!entry.cover_revision) return null;
  const src = `/api/v1/entries/${encodeURIComponent(entry.id)}/cover/${encodeURIComponent(entry.cover_revision)}`;
  if (failed === src) return card ? null : <p className="muted" role="status">封面暂时无法显示，请重新打开详情。</p>;
  return <span className={card ? 'cover-photo' : 'detail-cover-photo'}><img src={src} alt={card ? '' : `${entry.title}的封面`} loading={card ? 'lazy' : 'eager'} onError={() => setFailed(src)} /></span>;
}
