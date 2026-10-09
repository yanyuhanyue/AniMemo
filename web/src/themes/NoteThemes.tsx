import { createContext, useContext, useState, type CSSProperties, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { client, result } from '../api/client';
import type { components } from '../api/schema';
import { Button } from '../components/ui/Button';
import { Dialog } from '../components/ui/Dialog';
import { Icon } from '../components/ui/Icon';
import { Problem } from '../public/Public';
import { ThemePreview } from './ThemePreview';
import './themes.css';

type Theme = components['schemas']['NotesTheme'];
type Options = components['schemas']['ThemeOptions'];
const NoteThemesContext = createContext<{ userID: string; options?: Options; error: Error | null; loading: boolean }>({ userID: '', error: null, loading: false });

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

export function NoteThemeProvider({ userID, children }: { userID: string; children: ReactNode }) {
  const themes = useQuery({ queryKey: ['themes', userID], queryFn: ({ signal }) => result(client.GET('/api/v1/themes', { signal })), refetchInterval: 30000 });
  return <NoteThemesContext.Provider value={{ userID, options: themes.isError ? undefined : themes.data, error: themes.error, loading: themes.isPending }}>{children}</NoteThemesContext.Provider>;
}

export function useNoteTheme() {
  const { options } = useContext(NoteThemesContext);
  return options?.items.find(item => item.manifest.slug === options.selected_slug);
}
export function useNoteThemeStyle() {
  return notesThemeStyle(useNoteTheme()?.manifest.notes_theme);
}

export function NoteThemeButton() {
  const [open, setOpen] = useState(false);
  return <><Button className="text-button notes-appearance-button" onClick={() => setOpen(true)}><Icon name="sparkle" />札记外观</Button>{open && <ThemePicker onClose={() => setOpen(false)} />}</>;
}

function ThemePicker({ onClose }: { onClose: () => void }) {
  const { userID, options, error, loading } = useContext(NoteThemesContext);
  const cache = useQueryClient();
  const [chosen, setChosen] = useState<string | undefined>();
  const slug = chosen ?? options?.selected_slug ?? '';
  const theme = options?.items.find(item => item.manifest.slug === slug);
  const save = useMutation({ mutationFn: () => result(client.PUT('/api/v1/themes/selection', { body: { slug, revision: theme?.revision ?? 0 } })), onSuccess: async () => { await cache.invalidateQueries({ queryKey: ['themes', userID] }); onClose(); }, onError: () => { void cache.invalidateQueries({ queryKey: ['themes', userID] }); } });
  return <Dialog title="札记外观" onClose={onClose} wide>
    <div className="notes-appearance-picker">
      <p>选择适合自己的札记排版。这里使用示例内容预览，应用后仅改变你的私人札记。</p>
      {loading ? <p role="status">正在读取可用外观…</p> : <>
        <div className="notes-appearance-options" role="group" aria-label="选择札记外观">
          <Button aria-pressed={!slug} disabled={save.isPending} onClick={() => setChosen('')}><strong>纸页原色</strong><span>默认外观</span></Button>
          {options?.items.map(item => <Button key={item.manifest.slug} aria-pressed={slug === item.manifest.slug} disabled={save.isPending} onClick={() => setChosen(item.manifest.slug)}><strong>{item.manifest.name}</strong><span>{item.publisher_id === 'ANIMEMO_FIRST_PARTY' ? '官方主题' : '已安装主题'}</span></Button>)}
        </div>
        {!options?.items.length && !error && <p className="muted">管理员启用主题扩展后，会出现在这里。纸页原色始终可用。</p>}
        <section className="notes-surface notes-appearance-preview" style={notesThemeStyle(theme?.manifest.notes_theme)} aria-label="外观预览">
          <span className="notes-preview-caption">外观预览</span>
          <ThemePreview release={theme} />
        </section>
        <div className="notes-appearance-actions"><Button className="button secondary" disabled={save.isPending} onClick={onClose}>取消</Button><Button className="button primary" disabled={save.isPending || (!!slug && !theme)} onClick={() => save.mutate()}>{save.isPending ? '正在保存…' : slug ? '应用外观' : '恢复默认外观'}</Button></div>
      </>}
      {(error || save.error) && <Problem error={save.error || error} />}
    </div>
  </Dialog>;
}
