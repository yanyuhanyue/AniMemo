export const palette = {
  coral: "#ff6b6b",
  pink: "#ff8fab",
  teal: "#4ecdc4",
  yellow: "#ffe66d",
  cream: "#fff5ee",
  ink: "#111111",
};

// Watch states use the reference site's fixed palette. Topic tags may still
// receive a user-defined color, but these four labels must never be overridden.
export const statusTagColors = {
  看过: "coral",
  在看: "teal",
  想看: "yellow",
  搁置: "white",
};

export const tagColors = {
  ...statusTagColors,
  真百: "pink",
  轻百: "rose",
  萌系: "amber",
  日常: "blue",
  喜剧: "slate",
  搞笑: "orange",
  校园: "lime",
  恋爱: "fuchsia",
  奇幻: "indigo",
  异世界: "violet",
  战斗: "slate",
  冒险: "slate",
  原创: "cyan",
  治愈: "emerald",
  剧场版: "sky",
  泡面番: "yellow",
  R18: "slate",
  魔法少女: "rose",
  音乐: "slate",
  悬疑: "slate",
  职场: "slate",
  游戏改: "slate",
  轩皇力推: "purple",
};

const hasOvaMarker = (record) => /(^|\s|《)OVA($|\s|》)/i.test(`${record.title || ""} ${record.japaneseTitle || ""}`)
  || record.tags?.some((tag) => tag.toUpperCase() === "OVA");

export function calculateShowcaseStats(records, remoteStats = null) {
  const scored = records.filter((record) => Number.isFinite(Number(record.score)) && Number(record.score) > 0);
  const computed = {
    completed_count: records.filter((record) => record.status === "completed").length,
    average_score: scored.length
      ? scored.reduce((sum, record) => sum + Number(record.score), 0) / scored.length
      : null,
    movie_count: records.filter((record) => record.tags?.includes("剧场版")).length,
    ova_count: records.filter(hasOvaMarker).length,
    short_count: records.filter((record) => record.tags?.includes("泡面番")).length,
    masterpiece_count: scored.filter((record) => Number(record.score) >= 9.5).length,
    pending_count: records.filter((record) => (
      record.status === "planned"
      || !Number.isFinite(Number(record.score))
      || Number(record.score) <= 0
      || /待定|未定档/.test(record.period || "")
    )).length,
  };
  const stats = remoteStats ? { ...computed, ...remoteStats } : computed;
  const average = Number(stats.average_score);

  return [
    { key: "completed", label: "追完的番剧数量", value: String(stats.completed_count ?? 0), note: `${stats.pending_count ?? 0} 部计划 / 待定`, color: "yellow", icon: "circle-check" },
    { key: "average", label: "平均评分", value: Number.isFinite(average) && average > 0 ? average.toFixed(2) : "N/A", note: "仅统计已评分作品", color: "coral", icon: "chart-line" },
    { key: "movies", label: "剧场版", value: String(stats.movie_count ?? 0), note: "独立大银幕作品", color: "teal", icon: "film" },
    { key: "ova", label: "OVA", value: String(stats.ova_count ?? 0), note: "纪念 / 特别篇", color: "white", icon: "compact-disc" },
    { key: "short", label: "泡面番", value: String(stats.short_count ?? 0), note: "轻松短篇作品", color: "teal", icon: "bowl-food" },
    { key: "masterpiece", label: "极高推荐 (9.5+)", value: String(stats.masterpiece_count ?? 0), note: "殿堂级佳作", color: "coral", icon: "crown" },
  ];
}

export const quickFilters = [
  { id: "all", label: "全部", tags: [] },
  { id: "yuri", label: "百合 (真/轻)", tags: ["真百", "轻百"] },
  { id: "daily", label: "萌系 & 日常", tags: ["萌系", "日常"] },
  { id: "school", label: "搞笑 & 校园", tags: ["搞笑", "校园"] },
  { id: "original", label: "原创 & 治愈", tags: ["原创", "治愈"] },
  { id: "special", label: "剧场版 & OVA & 泡面番", tags: ["剧场版", "OVA", "泡面番"] },
];
