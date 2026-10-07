import type { Accent, Entry, Status } from '../api/client';

export const statusLabels: Record<Status, string> = { planned: '想看', watching: '在看', completed: '看完', on_hold: '搁置', dropped: '弃番' };
export const formatLabels: Record<Entry['format'], string> = { tv: 'TV 动画', movie: '剧场版', ova: 'OVA / ONA', other: '其他' };
export const accentLabels: Record<Accent, string> = { violet: '鸢尾紫', coral: '珊瑚橘', blue: '晴空蓝', green: '薄荷绿', amber: '日光黄' };
export const statuses = Object.keys(statusLabels) as Status[];

export function localDate() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
}
