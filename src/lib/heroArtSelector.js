import { DEFAULT_POSTER, isTrustedProviderPoster, resolveDemoPoster } from "./demoMedia.js";

const SHANGHAI_DATE_FORMATTER = new Intl.DateTimeFormat("en-US", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

export function shanghaiDateKey(value = new Date()) {
  const parts = Object.fromEntries(
    SHANGHAI_DATE_FORMATTER.formatToParts(new Date(value)).map(({ type, value: part }) => [type, part]),
  );
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function dateOrdinal(dateKey) {
  const [year, month, day] = String(dateKey).split("-").map(Number);
  if (![year, month, day].every(Number.isInteger)) return 0;
  return Math.floor(Date.UTC(year, month - 1, day) / 86400000);
}

function eligiblePosterRecords(records) {
  return (Array.isArray(records) ? records : [])
    .filter((record) => !record?.tags?.includes("R18"))
    .filter((record) => record?.externalIdentity?.provider === "bangumi")
    .filter((record) => isTrustedProviderPoster(record?.posterUrl || record?.poster));
}

export function selectDailyHeroPosters(records = [], { now = new Date(), domain = "universe" } = {}) {
  const candidates = [...new Set(eligiblePosterRecords(records).map(resolveDemoPoster))];
  if (!candidates.length) return [DEFAULT_POSTER, DEFAULT_POSTER];
  if (candidates.length === 1) return [candidates[0], candidates[0]];

  const domainOffset = domain === "featured" ? 1 : 0;
  const start = (dateOrdinal(shanghaiDateKey(now)) + domainOffset) % candidates.length;
  return [candidates[start], candidates[(start + 1) % candidates.length]];
}
