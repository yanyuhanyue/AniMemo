import { Component, Fragment, createElement, useCallback, useLayoutEffect, useMemo, useState, type CSSProperties, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import type { components } from '../api/schema';
import baseCSS from './theme-content.css?inline';
import ratingCSS from '../components/rating.css?inline';

export type ThemeScope = components['schemas']['ThemeScope'];
export function themeDefinition(release: ThemeRelease | undefined, scope: ThemeScope = 'private.notes') {
  return scope === 'private.notes' ? release?.manifest.notes_theme : release?.manifest.journal_theme;
}
export type ThemeRelease = components['schemas']['PluginRelease'];
type Slots = Record<string, ReactNode>;
const tags = new Set('div section article header footer aside figure figcaption span p h3 h4 strong em small ul li br hr slot img'.split(' '));
export function themeAssetURL(release: ThemeRelease, name: string) {
  return `/api/v1/themes/${encodeURIComponent(release.manifest.slug)}/${encodeURIComponent(release.manifest.version)}/assets/${encodeURIComponent(name)}`;
}

// Parse inert HTML and construct React elements. No raw HTML, event attributes,
// custom components, scripts or runtime imports enter the document.
export function ThemeTemplate({ source, slots, release, scope = 'private.notes' }: { source: string; slots: Slots; release: ThemeRelease; scope?: ThemeScope }) {
  const nodes = useMemo(() => Array.from(new DOMParser().parseFromString(source, 'text/html').body.childNodes), [source]);
  function render(node: Node, key: number): ReactNode {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent;
    if (!(node instanceof HTMLElement) || !tags.has(node.localName)) throw new Error('Invalid theme element');
    if (node.localName === 'slot') return <Fragment key={key}>{slots[node.getAttribute('name') || '']}</Fragment>;
    if (node.localName === 'img') {
      const name = node.getAttribute('src')?.replace(/^asset:/, '');
      if (!themeDefinition(release, scope)?.presentation?.assets?.some(a => a.name === name && a.content_type.startsWith('image/'))) throw new Error('Invalid theme resource');
      return <img key={key} className={node.className} src={themeAssetURL(release, name!)} alt="" loading="lazy" />;
    }
    return createElement(node.localName, { key, className: node.className || undefined }, ...Array.from(node.childNodes).map(render));
  }
  return <>{nodes.map(render)}</>;
}

const fonts = new Map<string, { face: FontFace; users: number }>();
function retainFont(family: string, url: string) {
  let entry = fonts.get(family);
  if (!entry) {
    entry = { face: new FontFace(family, `url("${url}")`), users: 0 };
    fonts.set(family, entry);
    const current = entry;
    void current.face.load().then(face => { if (fonts.get(family) === current && current.users) document.fonts.add(face); }).catch(() => { /* System font remains usable if a decorative font fails. */ });
  }
  entry.users++;
  return () => { if (--entry.users === 0) { document.fonts.delete(entry.face); fonts.delete(family); } };
}

export function ThemeFrame({ release, children, scope = 'private.notes' }: { release: ThemeRelease; children: ReactNode; scope?: ThemeScope }) {
  const presentation = themeDefinition(release, scope)!.presentation!;
  const [root, setRoot] = useState<ShadowRoot | null>(null);
  const attach = useCallback((node: HTMLDivElement | null) => { if (node) setRoot(node.shadowRoot || node.attachShadow({ mode: 'open' })); }, []);
  const font = presentation.assets?.find(a => a.content_type === 'font/woff2');
  const family = `am-theme-${release.digest.slice(0, 16)}-${font?.sha256.slice(0, 16) || "system"}`;
  useLayoutEffect(() => font ? retainFont(family, themeAssetURL(release, font.name)) : undefined, [family, font, release]);
  useLayoutEffect(() => {
    if (!root) return;
    const base = new CSSStyleSheet(), theme = new CSSStyleSheet();
    base.replaceSync(baseCSS + ratingCSS);
    theme.replaceSync(presentation.css.replace(/asset:([a-z][a-z0-9.-]{0,63})/g, (_match, name: string) => themeAssetURL(release, name)));
    root.adoptedStyleSheets = [base, theme];
    return () => { root.adoptedStyleSheets = []; };
  }, [root, release, presentation.css]);
  return <div className="theme-frame" ref={attach} style={{ '--theme-font': font ? family : 'var(--am-font-body)' } as CSSProperties}>
    {root && createPortal(children, root)}
  </div>;
}

// The fallback contains the same core data/actions; an invalid rendering must
// never strand a user behind a broken theme. Remount when package identity changes.
export class ThemeBoundary extends Component<{ children: ReactNode; fallback: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() { return this.state.failed ? this.props.fallback : this.props.children; }
}
