import { useMutation } from '@tanstack/react-query';
import type { FormEvent } from 'react';
import { client, errorMessage, result } from '../api/client';
import type { Accent, Entry, Status } from '../api/client';
import { Dialog } from '../ui/Dialog';
import { Icon } from '../ui/Icon';
import { accentLabels, formatLabels, statusLabels, statuses } from './labels';

export function EntryEditor({ entry, onClose, onSaved }: { entry: Entry | null; onClose: () => void; onSaved: (message: string) => void }) {
  const save = useMutation({
    mutationFn: async (data: FormData) => {
      const score = Number(data.get('score'));
      const body = {
        title: String(data.get('title')).trim(),
        original_title: String(data.get('original_title')).trim(),
        format: String(data.get('format')) as Entry['format'],
        status: String(data.get('status')) as Status,
        total_episodes: Number(data.get('total_episodes')),
        notes: String(data.get('notes')).trim(),
        tags: String(data.get('tags')).split(/[,，]/).map(tag => tag.trim()).filter(Boolean),
        accent: String(data.get('accent')) as Accent,
      };
      return entry
        ? result(client.PATCH('/api/v1/entries/{id}', { params: { path: { id: entry.id } }, body: { ...body, score, version: entry.version } }))
        : result(client.POST('/api/v1/entries', { body: { ...body, score: score || null } }));
    },
    onSuccess: () => onSaved(entry ? '番剧记录已更新' : '新番剧已加入手账'),
  });
  function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); save.mutate(new FormData(event.currentTarget)); }
  return <Dialog title={entry ? '编辑番剧' : '加入一部番剧'} eyebrow={entry ? 'EDIT YOUR MEMORY' : 'A NEW STORY BEGINS'} onClose={onClose} wide>
    <form className="editor-form" onSubmit={submit}>
      <fieldset disabled={save.isPending}>
        <label>番剧名称 <span className="required">*</span><input name="title" placeholder="例如：葬送的芙莉莲" defaultValue={entry?.title} required maxLength={160} data-initial-focus /></label>
        <label>原名 <span className="optional">选填</span><input name="original_title" placeholder="作品的原文名称" defaultValue={entry?.original_title} maxLength={160} /></label>
        <div className="form-row">
          <label>类型<select name="format" defaultValue={entry?.format || 'tv'}>{Object.entries(formatLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label>观看状态<select name="status" defaultValue={entry?.status || 'planned'}>{statuses.map(value => <option key={value} value={value}>{statusLabels[value]}</option>)}</select></label>
        </div>
        <div className="form-row">
          <label>总话数<input name="total_episodes" type="number" min="0" max="10000" step="1" required defaultValue={entry?.total_episodes || 0} /><span className="field-hint">暂时不知道，可以填 0。</span></label>
          <label>我的评分<select name="score" defaultValue={entry?.score || 0}><option value="0">还没有评分</option>{Array.from({ length: 10 }, (_, i) => 10 - i).map(value => <option key={value} value={value}>{value} 分</option>)}</select></label>
        </div>
        <label>标签 <span className="optional">选填</span><input name="tags" defaultValue={entry?.tags.join('，')} placeholder="治愈，冒险，想要重看" maxLength={200} /><span className="field-hint">用逗号分隔，最多 8 个标签。</span></label>
        <label>留一点感想 <span className="optional">选填</span><textarea name="notes" defaultValue={entry?.notes} placeholder="为什么想看？哪一幕让你念念不忘？" maxLength={4000} rows={3} /></label>
        <div className="palette-field"><span className="field-label">封面颜色</span><div className="palette">{Object.entries(accentLabels).map(([value, label]) => <label className="color-option" data-accent={value} key={value} title={label}><input name="accent" type="radio" value={value} defaultChecked={value === (entry?.accent || 'violet')} /><span><Icon name="check" /></span><span className="sr-only">{label}</span></label>)}</div></div>
      </fieldset>
      {save.isError && <p className="error-message" role="alert">{errorMessage(save.error)}</p>}
      <footer className="dialog-footer"><button type="button" className="button quiet" onClick={onClose}>取消</button><button className="button primary" disabled={save.isPending}>{save.isPending ? '正在保存…' : entry ? '保存修改' : '加入手账'}<Icon name="check" /></button></footer>
    </form>
  </Dialog>;
}
