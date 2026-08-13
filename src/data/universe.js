import { animeRecords } from "./anime.js";

function summarizeRecords(records) {
  const scored = records.filter((record) => Number(record.score) > 0);
  const average = scored.length
    ? scored.reduce((sum, record) => sum + Number(record.score), 0) / scored.length
    : 0;

  return {
    completed_count: records.filter((record) => record.status === "completed").length,
    average_score: average,
    masterpiece_count: scored.filter((record) => Number(record.score) >= 9.5).length,
    movie_count: records.filter((record) => record.tags?.includes("剧场版")).length,
    ova_count: records.filter((record) => record.tags?.includes("OVA") || /OVA/i.test(record.title)).length,
    short_count: records.filter((record) => record.tags?.includes("泡面番")).length,
  };
}

function createDemoOwner(publicSlug, nickname, subtitle, records, avatar) {
  return {
    id: publicSlug,
    public_slug: publicSlug,
    nickname,
    subtitle,
    avatar,
    records,
    stats: summarizeRecords(records),
    top_picks: [...records]
      .filter((record) => Number(record.score) > 0)
      .sort((a, b) => Number(b.score) - Number(a.score))
      .slice(0, 3),
  };
}

const keplerRecords = animeRecords.map((record, index) => ({
  ...record,
  id: `kepler-${record.id}`,
  status: index % 5 === 4 ? "planned" : record.status,
  statusLabel: index % 5 === 4 ? "想看" : record.statusLabel,
}));

export const demoUniverseOwners = [
  createDemoOwner(
    "animemo-demo",
    "AniMemo",
    "把每一次与动画相遇认真收藏，整理成自己的动漫记忆库。",
    animeRecords,
    "/assets/avatar.png",
  ),
  createDemoOwner(
    "demo-rabbit",
    "兔子",
    "我是一直追番无害的小兔子~",
    animeRecords.filter((_, index) => index % 2 === 0),
    "/assets/posters/poster-11.webp",
  ),
  createDemoOwner(
    "576932588@qq.com",
    "Kepler",
    "加番好麻烦，懒得添了(=ｘェｘ=)",
    keplerRecords,
    "/assets/posters/poster-06.webp",
  ),
];

export function getDemoUniverseOwner(publicSlug) {
  if (!publicSlug) return null;
  const decoded = decodeURIComponent(String(publicSlug)).trim().toLocaleLowerCase("zh-CN");
  return demoUniverseOwners.find((owner) => owner.public_slug.toLocaleLowerCase("zh-CN") === decoded) || null;
}
