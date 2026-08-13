import { resolveTagColors } from "../lib/tagPresets.js";

export const STORAGE_KEY = "anime_journal_records_v1";
export const SETTINGS_KEY = "anime_journal_settings_v1";
export const FILTERS_KEY = "anime_journal_quick_filters_v1";
export const DEFAULT_SETTINGS = { email: "", nickname: "AniMemo", subtitle: "把每一次与动画相遇认真收藏。", avatar: "/assets/avatar.png", accent: "#4ecdc4", publicProfile: false, publicSlug: "", publicStatus: "private", isStaff: false, isSuperuser: false, twoFactorEnabled: false };

export const STATUS_OPTIONS = [
  ["all", "全部状态"],
  ["planned", "想看"],
  ["watching", "在看"],
  ["completed", "看过"],
  ["on_hold", "搁置"],
  ["dropped", "弃番"],
];

export const SORT_OPTIONS = [
  ["date-desc", "开播时间 (新 → 旧) [推荐]"],
  ["date-asc", "开播时间 (旧 → 新)"],
  ["score-desc", "评分 (高 → 低)"],
  ["score-asc", "评分 (低 → 高)"],
  ["updated-desc", "记录更新 (新 → 旧)"],
  ["updated-asc", "记录更新 (旧 → 新)"],
];

export const DEFAULT_QUICK_FILTERS = [
  { id: "all", name: "全部", tags: [] },
  { id: "mix", name: "百合 (真/轻)", tags: ["百合", "真百", "轻百"] },
  { id: "daily", name: "萌系 & 日常", tags: ["萌系", "日常"] },
  { id: "school", name: "搞笑 & 校园", tags: ["搞笑", "校园"] },
  { id: "healing", name: "原创 & 治愈", tags: ["原创", "治愈"] },
  { id: "extras", name: "剧场版 & OVA & 泡面番", tags: ["剧场版", "OVA", "泡面番"] },
];

export const blankRecord = () => ({
  id: `local-${Date.now()}`,
  title: "《新番剧记录》",
  japaneseTitle: "",
  period: "2026-10",
  studio: "待补充",
  episodes: "12",
  score: null,
  status: "planned",
  statusLabel: "想看",
  tags: ["日常"],
  poster: "/assets/posters/poster-01.webp",
  description: "写下这部作品的剧情简介。",
  review: "",
  baikeUrl: "https://mzh.moegirl.org.cn/",
  watchHistory: [],
  watchHistoryCount: 0,
  firstWatchedOn: null,
  lastWatchedOn: null,
  latestEpisodeStart: null,
  latestEpisodeEnd: null,
  tagColors: { 日常: "blue" },
  shared: false,
  visibility: "private",
  externalIdentities: [],
});

export function apiToRecord(item, presetColors) {
  return {
    id: item.id,
    title: item.title,
    japaneseTitle: item.japanese_title || "",
    period: item.airing_period || "未定档",
    studio: item.studio || "待补充",
    episodes: item.episodes || "待定",
    score: item.personal_score === null ? null : Number(item.personal_score),
    status: item.watch_status,
    statusLabel: item.watch_status_display || { completed: "看过", watching: "在看", planned: "想看", on_hold: "搁置", dropped: "弃番" }[item.watch_status],
    tags: item.tags || [],
    savedTagColors: item.tag_colors || {},
    tagColors: resolveTagColors(item.tags || [], item.tag_colors || {}, presetColors),
    poster: item.poster || item.poster_url || "/assets/posters/poster-01.webp",
    posterUrl: item.poster_url || "",
    customPosterUrl: item.custom_poster_url || "",
    posterSource: item.poster_source || (item.poster_file ? "upload" : item.poster_url ? "default_url" : "none"),
    clearCustomPoster: false,
    description: item.description || "",
    review: item.review || "",
    baikeUrl: item.baike_url || "https://mzh.moegirl.org.cn/",
    watchHistory: [],
    watchHistoryCount: Number(item.watch_history_count || 0),
    firstWatchedOn: item.first_watched_on || null,
    lastWatchedOn: item.last_watched_on || null,
    latestEpisodeStart: item.latest_episode_start ?? null,
    latestEpisodeEnd: item.latest_episode_end ?? null,
    shared: item.visibility !== "private",
    visibility: item.visibility || "private",
    externalIdentities: Array.isArray(item.external_identities) ? item.external_identities : [],
    updatedAt: item.updated_at,
  };
}

export function recordToApi(record) {
  const payload = {
    title: record.title,
    japanese_title: record.japaneseTitle,
    airing_period: record.period,
    studio: record.studio,
    episodes: record.episodes,
    personal_score: record.score === null || record.score === "" ? null : Number(record.score),
    watch_status: record.status,
    tags: record.tags,
    tag_colors: record.tagColors || {},
    poster_url: record.posterUrl || "",
    custom_poster_url: record.customPosterUrl || "",
    clear_custom_poster: Boolean(record.clearCustomPoster),
    description: record.description,
    review: record.review,
    baike_url: record.baikeUrl,
    visibility: record.visibility || (record.shared ? "public" : "private"),
  };
  if (record.externalIdentity) payload.external_identity = record.externalIdentity;
  return payload;
}

export function importIdentity(value) {
  return String(value || "")
    .normalize("NFKC")
    .toLocaleLowerCase()
    .replace(/[\s《》「」『』【】〔〕〈〉·・:：,，。.!！?？'"“”‘’\-—_]/g, "");
}

export function importIdentityValues(record) {
  return new Set([record?.title, record?.japaneseTitle, record?.japanese_title].map(importIdentity).filter(Boolean));
}

export function parseLocalImportRecords(raw, presetColors) {
  if (!Array.isArray(raw)) throw new Error("导入内容必须是记录数组");
  return raw.map((item) => {
    if (!item || typeof item !== "object") return null;
    const tags = Array.isArray(item.tags) ? item.tags : String(item.tags || "").split(/[，,]/).map((tag) => tag.trim()).filter(Boolean);
    const status = item.watch_status || "planned";
    const statusLabels = { completed: "看过", watching: "在看", planned: "想看", on_hold: "搁置", dropped: "弃番" };
    const posterUrl = item.poster_url || "";
    const customPosterUrl = item.custom_poster_url || "";
    return {
      id: `local-import-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      title: item.title || "",
      japaneseTitle: item.japanese_title || "",
      period: item.airing_period || "未定档",
      studio: item.studio || "待补充",
      episodes: item.episodes || "待定",
      score: item.personal_score ?? null,
      status,
      statusLabel: statusLabels[status] || "想看",
      tags,
      tagColors: resolveTagColors(tags, item.tag_colors || {}, presetColors),
      poster: customPosterUrl || posterUrl || "/assets/posters/poster-01.webp",
      posterUrl,
      customPosterUrl,
      posterSource: customPosterUrl ? "trusted_url" : posterUrl ? "default_url" : "none",
      clearCustomPoster: false,
      description: item.description || "",
      review: item.review || "",
      baikeUrl: item.baike_url || "",
      watchHistory: item.watch_history || [],
      watchHistoryCount: Array.isArray(item.watch_history) ? item.watch_history.length : 0,
      firstWatchedOn: null,
      lastWatchedOn: null,
      latestEpisodeStart: null,
      latestEpisodeEnd: null,
      shared: item.visibility === "public",
      visibility: item.visibility || "private",
      externalIdentities: Array.isArray(item.external_identities) ? item.external_identities : [],
    };
  });
}

export function comparePeriod(a, b) {
  return String(a.period || "").localeCompare(String(b.period || ""), "zh-CN", { numeric: true });
}

export function matchesQuickFilter(record, filter) {
  const filterTags = filter?.tags || [];
  const keywords = filter?.title_keywords || [];
  if (!filterTags.length && !keywords.length) return true;
  const tags = new Set([...(record.tags || []), record.statusLabel, record.studio].filter(Boolean));
  const matches = filterTags.map((tag) => [...tags].some((value) => String(value).includes(tag)));
  const tagMatched = filterTags.length ? (filter.match_mode === "all" ? matches.every(Boolean) : matches.some(Boolean)) : null;
  const title = `${record.title} ${record.japaneseTitle}`.toLowerCase();
  const keywordMatches = keywords.map((keyword) => title.includes(String(keyword).toLowerCase()));
  const keywordMatched = keywords.length ? (filter.match_mode === "all" ? keywordMatches.every(Boolean) : keywordMatches.some(Boolean)) : null;
  if (tagMatched === null) return keywordMatched;
  if (keywordMatched === null) return tagMatched;
  return filter.match_mode === "all" ? tagMatched && keywordMatched : tagMatched || keywordMatched;
}

