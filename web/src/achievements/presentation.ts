import type { components } from "../api/schema";

export type BadgeKind = components["schemas"]["AchievementRule"]["badge"];
export type Achievement = components["schemas"]["Achievement"];

export const badgeOptions: { value: BadgeKind; label: string }[] = [
  { value: "spark", label: "初光之星" },
  { value: "moon", label: "月夜来信" },
  { value: "orbit", label: "漫游星轨" },
  { value: "flower", label: "花开之时" },
  { value: "book", label: "记忆手札" },
  { value: "ticket", label: "故事入场券" },
  { value: "shelf", label: "回忆收藏柜" },
  { value: "album", label: "时光纪念册" },
];

export const badgeGrades = [
  { id: "bronze", label: "青铜" },
  { id: "silver", label: "白银" },
  { id: "gold", label: "黄金" },
  { id: "platinum", label: "铂金" },
  { id: "prismatic", label: "幻彩" },
] as const;
export const badgeGrade = (tier: number) =>
  badgeGrades[Math.min(5, Math.max(1, Math.trunc(tier) || 1)) - 1] ??
  badgeGrades[0];

type Metric = Achievement["metric"];

export const metricNames: Record<Metric, string> = {
  recorded_anime: "已记录作品",
  memory_notes: "独立记忆",
  memory_collections: "有内容的收藏夹",
  yearly_albums: "有选入记忆的年度册",
  watch_records: "观看记录",
  distinct_anime: "有观看记录的作品",
  completed_anime: "已看完作品",
  watched_episodes: "记录中的观看话数",
};

export const metricUnits: Record<Metric, string> = {
  recorded_anime: "部",
  memory_notes: "份",
  memory_collections: "个",
  yearly_albums: "本",
  watch_records: "条",
  distinct_anime: "部",
  completed_anime: "部",
  watched_episodes: "话",
};

export const metricDescriptions: Record<Metric, string> = {
  recorded_anime:
    "按作品身份去重，只记录名字也计入。不计“想看”分类和已删除记录，不要求看完或填写日期。",
  memory_notes:
    "计入未删除的笔记与瞬间，日期不详也计入。修改同一份记忆不会重复计数。",
  memory_collections:
    "每个收藏夹至少放入一项内容才计入，空收藏夹不计入。修改同一个收藏夹不会重复计数。",
  yearly_albums:
    "以每本年度册的最新修订为准，至少选入一份记忆才计入。多次保存同一本仍计为一本，不推断观看年份。",
  watch_records:
    "计入单独保存的观看记录；仅添加作品名不会生成观看记录，也不推断话数或日期。",
  distinct_anime:
    "计入至少有一条观看记录的不同作品，与仅记录作品名的数量分别统计。",
  completed_anime:
    "计入自己明确标为“已看完”且未删除的作品，不根据作品总话数推断。",
  watched_episodes: "合计观看记录中填写的话数范围，不根据作品总话数推断。",
};

export function achievementState(a: Achievement) {
  return a.granted ? "earned" : a.unlock_id ? "revoked" : "locked";
}

export const achievementStateNames = {
  earned: "已获得",
  locked: "未获得",
  revoked: "已撤回",
};
