import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { client, result } from '../api/client';
import type { components } from '../api/schema';
import { Icon } from './ui/Icon';
import './site-identity.css';

export type SiteSettings = components['schemas']['SiteSettings'];
export function useSite() {
  return useQuery({ queryKey: ['site'], queryFn: ({ signal }) => result(client.GET('/api/v1/site', { signal })) });
}
export function siteImageURL(kind: 'icon' | 'cover', revision?: string) {
  return revision ? `/api/v1/site/images/${kind}/${encodeURIComponent(revision)}` : undefined;
}
export function SiteIdentity() {
  const { data: site } = useSite();
  useEffect(() => {
    if (!site) return;
    document.title = `${site.name} · 我的动漫记忆库`;
    document.querySelector('meta[name="description"]')?.setAttribute('content', site.description);
    const icon = document.querySelector<HTMLLinkElement>('link[rel="icon"]');
    if (icon) { icon.href = siteImageURL('icon', site.icon_revision) || '/favicon.svg'; icon.type = site.icon_revision ? '' : 'image/svg+xml'; }
  }, [site]);
  return null;
}
export function SiteBrand({ href = '/', className = 'brand', caption }: { href?: string; className?: string; caption?: string }) {
  const { data: site } = useSite();
  const name = site?.name || 'AniMemo';
  return <a className={`${className} site-brand`} href={href} title={name}>
    <span className="brand-mark site-logo">{site?.icon_revision ? <img src={siteImageURL('icon', site.icon_revision)} alt="" /> : <Icon name="play" />}</span>
    <span className="site-brand-copy"><span className="site-brand-name">{name}<span className="brand-dot">.</span></span>{caption && <small>{caption}</small>}</span>
  </a>;
}
export function SiteCard({ site, heading = 'h1' }: { site?: SiteSettings; heading?: 'h1' | 'h2' }) {
  const Heading = heading;
  return <section className="site-card" aria-label="站点名片">
    <div className="site-card-cover">{site?.cover_revision && <img src={siteImageURL('cover', site.cover_revision)} alt="" />}</div>
    <div className="site-card-copy"><span className="site-card-icon">{site?.icon_revision ? <img src={siteImageURL('icon', site.icon_revision)} alt="" /> : <Icon name="play" />}</span>
      <Heading>{site?.name || 'AniMemo'}</Heading><p>{site?.description || '故事会完结，记忆继续放映。'}</p>
    </div>
  </section>;
}
