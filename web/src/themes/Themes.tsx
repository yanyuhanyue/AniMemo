import { createContext, useContext, useState, type CSSProperties, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { client, result } from '../api/client';
import type { components } from '../api/schema';
import { Button } from '../components/ui/Button';
import { Dialog } from '../components/ui/Dialog';
import { Icon } from '../components/ui/Icon';
import { Problem } from '../public/Public';
import { themeDefinition, type ThemeScope } from './ThemeTemplate';
import { ThemePreview } from './ThemePreview';
import './themes.css';

type Theme = Omit<components['schemas']['NotesTheme'], 'presentation'>;
const ThemeUserContext = createContext('');

// Only documented values become variables on the notes surface; never inject a
// style tag, URL, selector or extension-supplied CSS property name.
export function notesThemeStyle(theme?: Theme): CSSProperties | undefined {
  if (!theme) return undefined;
  return {
    '--notes-canvas': theme.canvas, '--notes-paper': theme.paper,
    '--notes-ink': theme.ink, '--notes-muted': theme.muted,
    '--notes-primary': theme.primary, '--notes-border': theme.border, '--notes-rule': theme.rule,
    '--notes-heading-font': theme.heading_font === 'sans' ? 'var(--am-font-body)' : 'Georgia, "Noto Serif CJK SC", "Songti SC", SimSun, serif',
    '--notes-reading-size': theme.reading_size === 'large' ? '17px' : '15px',
    '--notes-prose-size': theme.reading_size === 'large' ? '19px' : '17px',
    '--notes-mobile-space': theme.spacing === 'relaxed' ? '28px' : '24px',
  } as CSSProperties;
}

export function ThemeProvider({ userID, children }: { userID: string; children: ReactNode }) {
  return <ThemeUserContext.Provider value={userID}>{children}</ThemeUserContext.Provider>;
}
function useThemeOptions(scope: ThemeScope) {
  const userID = useContext(ThemeUserContext);
  return useQuery({ queryKey: ['themes', userID, scope], enabled: !!userID, queryFn: ({ signal }) => result(client.GET('/api/v1/themes', { signal, params: { query: { scope } } })), refetchInterval: 30000 });
}
export function useTheme(scope: ThemeScope = 'private.notes') {
  const options = useThemeOptions(scope);
  return options.isError ? undefined : options.data?.items.find(item => item.manifest.slug === options.data.selected_slug);
}
export function useNoteTheme() { return useTheme(); }
export function useNoteThemeStyle() { return notesThemeStyle(useNoteTheme()?.manifest.notes_theme); }

export function ThemeButton({ scope = 'private.notes' }: { scope?: ThemeScope }) {
  const [open, setOpen] = useState(false);
  return <><Button className="text-button notes-appearance-button" onClick={() => setOpen(true)}><Icon name="sparkle" />{scope === 'private.notes' ? '札记外观' : '手账外观'}</Button>{open && <ThemePicker scope={scope} onClose={() => setOpen(false)} />}</>;
}
export function NoteThemeButton() { return <ThemeButton />; }

function ThemePicker({ onClose, scope }: { onClose: () => void; scope: ThemeScope }) {
  const userID = useContext(ThemeUserContext);
  const query = useThemeOptions(scope);
  const { error, isPending: loading } = query;
  const options = query.isError ? undefined : query.data;
  const journal = scope === 'private.journal';
  const [preview, setPreview] = useState<'cards' | 'list' | 'detail'>('cards');
  const cache = useQueryClient();
  const [chosen, setChosen] = useState<string | undefined>();
  const slug = chosen ?? options?.selected_slug ?? '';
  const theme = options?.items.find(item => item.manifest.slug === slug);
  const save = useMutation({ mutationFn: () => result(client.PUT('/api/v1/themes/selection', { body: { slug, scope, revision: theme?.revision ?? 0 } })), onSuccess: async () => { await cache.invalidateQueries({ queryKey: ['themes', userID, scope] }); onClose(); }, onError: () => { void cache.invalidateQueries({ queryKey: ['themes', userID, scope] }); } });
  return <Dialog title={journal ? "手账外观" : "札记外观"} onClose={onClose} wide>
    <div className="notes-appearance-picker">
      <p>{journal ? "用喜欢的排版收藏看过的故事。应用到你的手账清单和作品详情，札记外观可单独选择。" : "选择适合自己的札记排版。这里使用示例内容预览，应用后仅改变你的私人札记。"}</p>
      {loading ? <p role="status">正在读取可用外观…</p> : <>
        <div className="notes-appearance-options" role="group" aria-label={journal ? "选择手账外观" : "选择札记外观"}>
          <Button aria-pressed={!slug} disabled={save.isPending} onClick={() => setChosen('')}><strong>{journal ? "手账原色" : "纸页原色"}</strong><span>默认外观</span></Button>
          {options?.items.map(item => <Button key={item.manifest.slug} aria-pressed={slug === item.manifest.slug} disabled={save.isPending} onClick={() => setChosen(item.manifest.slug)}><strong>{item.manifest.name}</strong><span>{item.publisher_id === 'ANIMEMO_FIRST_PARTY' ? '官方主题' : '已安装主题'}</span></Button>)}
        </div>
        {!options?.items.length && !error && <p className="muted">管理员启用主题扩展后，会出现在这里。默认外观始终可用。</p>}
        {journal && theme?.manifest.journal_theme && <div className="notes-preview-modes" role="group" aria-label="预览页面">{(['cards', 'list', 'detail'] as const).map(mode => <Button key={mode} aria-pressed={preview === mode} onClick={() => setPreview(mode)}>{ {cards:'卡片', list:'列表', detail:'作品详情'}[mode] }</Button>)}</div>}
        <section className="notes-surface notes-appearance-preview" style={notesThemeStyle(themeDefinition(theme, scope))} aria-label="外观预览">
          <span className="notes-preview-caption">外观预览</span>
          <ThemePreview release={theme} scope={scope} preview={preview} />
        </section>
        <div className="notes-appearance-actions"><Button className="button secondary" disabled={save.isPending} onClick={onClose}>取消</Button><Button className="button primary" disabled={save.isPending || (!!slug && !theme)} onClick={() => save.mutate()}>{save.isPending ? '正在保存…' : slug ? '应用外观' : '恢复默认外观'}</Button></div>
      </>}
      {(error || save.error) && <Problem error={save.error || error} />}
    </div>
  </Dialog>;
}
