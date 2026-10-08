import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { client, result } from '../api/client';
import { Button } from '../components/ui/Button';
import { Dialog } from '../components/ui/Dialog';
import { Icon } from '../components/ui/Icon';
import { Problem } from '../public/Public';
import { NoteBody } from './NoteBody';
import { visibilityLabels, type Collection } from './shared';

const labels = { anime: '作品', character: '角色', note: '札记', moment: '瞬间' };

export function CollectionReader({ userID, collection, onClose, onEdit, onShare, onRemove }: {
  userID: string; collection: Collection; onClose: () => void;
  onEdit: () => void; onShare: () => void; onRemove: () => void;
}) {
  const [selected, setSelected] = useState('');
  const refs = useQuery({
    queryKey: ['memory', userID, 'references', collection.items],
    queryFn: ({ signal }) => result(client.POST('/api/v1/memory/references', { signal, body: { items: collection.items } })),
  });
  const note = useQuery({
    queryKey: ['memory', userID, 'collection-note', selected], enabled: !!selected,
    queryFn: ({ signal }) => result(client.GET('/api/v1/memory/notes/{id}', { signal, params: { path: { id: selected } } })),
  });
  return <Dialog title={collection.title} eyebrow="A LITTLE BOOK OF MEMORIES" onClose={onClose} wide className="collection-reader">
    <div className="collection-reading-layout">
      <aside className="collection-frontispiece">
        <Icon name="book" />
        <p className="eyebrow">我的私人选集</p>
        <h3>{collection.title}</h3>
        {collection.description && <p className="collection-description">{collection.description}</p>}
        <p className="memory-meta">{collection.items.length} 段收藏 · {visibilityLabels[collection.visibility]}</p>
        <Button className="button secondary" onClick={onEdit}><Icon name="edit" />编辑小册</Button>
      </aside>
      <section className="collection-pages" aria-label="小册内容">
        {selected ? <>
          <Button className="text-button collection-back" onClick={() => setSelected('')}><Icon name="arrow" />返回小册目录</Button>
          {note.isPending ? <p role="status">正在翻开这一页…</p> : note.error ? <Problem error={note.error} /> : <article className="memory-reading collection-note" key={selected}>
            <p className="memory-meta">{note.data.occurred_on || '日期未记'} · {visibilityLabels[note.data.visibility]}</p>
            <h3>{note.data.title}</h3>
            {note.data.spoiler ? <details><summary>展开剧透内容</summary><NoteBody note={note.data} /></details> : <NoteBody note={note.data} />}
          </article>}
        </> : <>
          <div className="collection-contents-heading"><h3>翻开这些故事</h3><span>CONTENTS</span></div>
          {refs.isPending ? <p role="status">正在整理小册目录…</p> : refs.error ? <Problem error={refs.error} /> : <ol className="collection-contents">
            {collection.items.map((item, index) => {
              const ref = refs.data.items.find(r => r.kind === item.kind && r.id === item.id);
              const label = <><span className="collection-page-number">{String(index + 1).padStart(2, '0')}</span><span><small>{labels[item.kind]}</small><strong>{ref?.available ? ref.title : '这份内容已不在记忆库'}</strong></span><Icon name="arrow" /></>;
              return <li key={`${item.kind}:${item.id}`}>{!ref?.available ? <div className="collection-unavailable">{label}</div> : item.kind === 'anime' || item.kind === 'character' ? <a href={`/memory?${item.kind === 'anime' ? 'anime_id' : 'character_id'}=${encodeURIComponent(item.id)}#notes`}>{label}</a> : <Button onClick={() => setSelected(item.id)}>{label}</Button>}</li>;
            })}
          </ol>}
          {!collection.items.length && <div className="memory-empty"><Icon name="book" /><h3>给喜欢的故事留几页</h3><p>编辑小册，把作品、角色或札记收进来。</p><Button className="button secondary" onClick={onEdit}>添加第一份收藏</Button></div>}
        </>}
      </section>
    </div>
    <footer className="dialog-footer"><Button className="text-button danger-text" onClick={onRemove}>移除收藏夹</Button><Button className="button secondary" onClick={onShare}>分享小册</Button></footer>
  </Dialog>;
}
