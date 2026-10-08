import type { Accent, Entry, Status } from '../api/client';

export const statusLabels: Record<Status, string> = { recorded: '看过，细节未记', completed: '记得看完', watching: '记录过部分观看', caught_up: '当时看到已播部分', on_hold: '当时搁置', dropped: '没有继续看', planned: '想看（已有分类）' };
export const formatLabels: Record<Entry['format'], string> = { tv: 'TV 动画', movie: '剧场版', ova: 'OVA / ONA', other: '其他' };
export const accentLabels: Record<Accent, string> = { violet: '鸢尾紫', coral: '珊瑚橘', blue: '晴空蓝', green: '薄荷绿', amber: '日光黄' };
export const statuses = Object.keys(statusLabels) as Status[];

export function localDate() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
}

export function watchDate(value: string, precision = "day") { return precision === "unknown" ? "日期未详" : precision === "year" ? `${value.slice(0,4)} 年` : precision === "month" ? `${value.slice(0,7)} 月` : value; }
