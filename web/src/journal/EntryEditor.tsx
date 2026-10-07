import { useMutation } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { client, errorMessage, result } from '../api/client';
import type { Accent, Entry, Status } from '../api/client';
import { Dialog } from '../ui/Dialog';
import { Icon } from '../ui/Icon';
import { accentLabels, formatLabels, statusLabels, statuses } from './labels';
import { SourcePicker } from './SourcePicker';

export function EntryEditor({ entry, onClose, onSaved }: { entry: Entry | null; onClose: () => void; onSaved: (message: string) => void }) {
  const [source, setSource] = useState(false);
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
        visibility: String(data.get('visibility')) as Entry['visibility'],
        details: { studio: String(data.get('studio')).trim(), airing_period: String(data.get('airing_period')).trim(), description: String(data.get('description')).trim(), reference_url: String(data.get('reference_url')).trim() },
      };
      return entry
        ? result(client.PATCH('/api/v1/entries/{id}', { params: { path: { id: entry.id } }, body: { ...body, score, version: entry.version } }))
        : result(client.POST('/api/v1/entries', { body: { ...body, score: score || null } }));
    },
    onSuccess: () => onSaved(entry ? '番剧记录已更新' : '新番剧已加入手账'),
  });
  function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); save.mutate(new FormData(event.currentTarget)); }
  if (source) return <SourcePicker entry={null} onClose={() => setSource(false)} onSaved={onSaved} />;
  return <Dialog title={entry ? '编辑番剧' : '加入一部番剧'} eyebrow={entry ? 'EDIT YOUR MEMORY' : 'A NEW STORY BEGINS'} onClose={onClose} wide>
    {!entry && <div className="source-shortcut"><button className="button secondary" onClick={() => setSource(true)}><Icon name="search" />从 Bangumi 搜索并填写</button><span className="muted">也可以在下方手动填写。</span></div>}
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
          <label>我的评分<input name="score" type="number" min="0" max="10" step="0.1" defaultValue={entry?.score || 0} /><span className="field-hint">1–10 分，支持一位小数；0 表示未评分。</span></label>
        </div>
        <label>标签 <span className="optional">选填</span><input name="tags" defaultValue={entry?.tags.join('，')} placeholder="治愈，冒险，想要重看" maxLength={200} /><span className="field-hint">用逗号分隔，最多 8 个标签。</span></label>
        <label>留一点感想 <span className="optional">选填</span><textarea name="notes" defaultValue={entry?.notes} placeholder="为什么想看？哪一幕让你念念不忘？" maxLength={4000} rows={3} /></label>
        <details className="extra-fields"><summary>作品资料（选填）</summary><div className="form-row"><label>制作公司<input name="studio" defaultValue={entry?.details.studio} maxLength={120} /></label><label>播出时期<input name="airing_period" defaultValue={entry?.details.airing_period} maxLength={50} placeholder="例如：2026 年春季" /></label></div><label>作品简介<textarea name="description" defaultValue={entry?.details.description} maxLength={8000} rows={4} /></label><label>资料链接<input name="reference_url" type="url" defaultValue={entry?.details.reference_url} maxLength={1000} placeholder="https://…" /></label></details>
        <label>可见性<select name="visibility" defaultValue={entry?.visibility || 'private'}><option value="private">私密 · 仅自己</option><option value="unlisted">持链接可见</option><option value="public">公开 · 可列入公开手账</option></select></label><p className="field-hint">分享会包含作品资料、评分、短评、标签和封面。观看日期与逐次记录始终保密；请先在账号设置中开启分享。</p>
        <div className="palette-field"><span className="field-label">封面颜色</span><div className="palette">{Object.entries(accentLabels).map(([value, label]) => <label className="color-option" data-accent={value} key={value} title={label}><input name="accent" type="radio" value={value} defaultChecked={value === (entry?.accent || 'violet')} /><span><Icon name="check" /></span><span className="sr-only">{label}</span></label>)}</div></div>
      </fieldset>
      {save.isError && <p className="error-message" role="alert">{errorMessage(save.error)}</p>}
      <footer className="dialog-footer"><button type="button" className="button quiet" onClick={onClose}>取消</button><button className="button primary" disabled={save.isPending}>{save.isPending ? '正在保存…' : entry ? '保存修改' : '加入手账'}<Icon name="check" /></button></footer>
    </form>
  </Dialog>;
}
