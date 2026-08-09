export const LONG_IDLE_DAYS = 14;
export const RECENT_WATCH_DAYS = 30;
export const RECENT_UPDATE_DAYS = 14;

const DAY_MS = 24 * 60 * 60 * 1000;

function dateValue(value) {
  const parsed = value ? new Date(value) : null;
  return parsed && !Number.isNaN(parsed.getTime()) ? parsed.getTime() : 0;
}

function positiveInteger(value) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

export function formatEpisodeRange(record = {}) {
  const start = positiveInteger(record.episode_start ?? record.latestEpisodeStart);
  const end = positiveInteger(record.episode_end ?? record.latestEpisodeEnd);
  if (start && end) return start === end ? `第 ${start} 话` : `第 ${start}-${end} 话`;
  if (start) return `从第 ${start} 话开始`;
  if (end) return `看到第 ${end} 话`;
  return "未记录话数";
}

export function sortContinueWatching(records = []) {
  return records
    .filter((record) => record.status === "watching")
    .sort((left, right) => {
      const watchedDifference = dateValue(right.lastWatchedOn) - dateValue(left.lastWatchedOn);
      if (watchedDifference) return watchedDifference;
      const updatedDifference = dateValue(right.updatedAt) - dateValue(left.updatedAt);
      if (updatedDifference) return updatedDifference;
      return String(left.id).localeCompare(String(right.id), "zh-CN", { numeric: true });
    });
}

export function suggestNextEpisode(records = [], totalEpisodes) {
  const latest = [...records]
    .sort((left, right) => {
      const dateDifference = String(right.watched_on || "").localeCompare(String(left.watched_on || ""));
      if (dateDifference) return dateDifference;
      return Number(right.sequence || right.id || 0) - Number(left.sequence || left.id || 0);
    })
    .find((record) => positiveInteger(record.episode_end) || positiveInteger(record.episode_start));
  const current = positiveInteger(latest?.episode_end) || positiveInteger(latest?.episode_start);
  if (!current) return { episodeStart: "", episodeEnd: "" };
  const total = positiveInteger(String(totalEpisodes || "").match(/\d+/)?.[0]);
  if (total && current >= total) return { episodeStart: "", episodeEnd: "" };
  const next = total ? Math.min(current + 1, total) : current + 1;
  return { episodeStart: String(next), episodeEnd: String(next) };
}

export function deriveEntryStatistics(records = []) {
  const ordered = [...records].sort((left, right) => String(left.watched_on || "").localeCompare(String(right.watched_on || "")));
  const starts = ordered.map((record) => positiveInteger(record.episode_start)).filter(Boolean);
  const ends = ordered.map((record) => positiveInteger(record.episode_end) || positiveInteger(record.episode_start)).filter(Boolean);
  const brushNumbers = ordered.map((record) => positiveInteger(record.brush_number)).filter(Boolean);
  return {
    count: ordered.length,
    firstWatchedOn: ordered[0]?.watched_on || null,
    lastWatchedOn: ordered.at(-1)?.watched_on || null,
    highestBrushNumber: brushNumbers.length ? Math.max(...brushNumbers) : null,
    episodeStart: starts.length ? Math.min(...starts) : null,
    episodeEnd: ends.length ? Math.max(...ends) : null,
  };
}

export function matchesActivityFilter(record, filter, now = new Date()) {
  if (!filter || filter === "all") return true;
  if (filter === "never-watched") return !record.lastWatchedOn;
  if (filter === "unrated") return !(Number(record.score) > 0);
  if (filter === "external-bound") return Boolean(record.externalIdentities?.length);
  if (filter === "external-unbound") return !record.externalIdentities?.length;
  const today = now.getTime();
  if (filter === "recent-watched") {
    const watched = dateValue(record.lastWatchedOn);
    return watched > 0 && today - watched <= RECENT_WATCH_DAYS * DAY_MS;
  }
  if (filter === "recent-updated") {
    const updated = dateValue(record.updatedAt);
    return updated > 0 && today - updated <= RECENT_UPDATE_DAYS * DAY_MS;
  }
  return true;
}

export function buildSmartReminders(records = [], now = new Date()) {
  const reminders = [];
  const nowValue = now.getTime();
  for (const record of records) {
    if (record.status === "completed" && !(Number(record.score) > 0)) {
      reminders.push({ type: "unrated", record, message: "已经看完，补一份评分吧" });
    }
    const lastWatched = dateValue(record.lastWatchedOn);
    if (record.status === "watching" && lastWatched && nowValue - lastWatched > LONG_IDLE_DAYS * DAY_MS) {
      reminders.push({ type: "idle", record, message: `超过 ${LONG_IDLE_DAYS} 天没有观看记录` });
    }
    if (!record.externalIdentities?.length) {
      reminders.push({ type: "external", record, message: "尚未关联外部资料" });
    }
    if (!record.poster || /poster-01\.webp$/.test(record.poster)) {
      reminders.push({ type: "poster", record, message: "还可以补充一张封面" });
    }
    if (reminders.length >= 6) break;
  }
  return reminders.slice(0, 6);
}

export async function runBounded(items, worker, concurrency = 4) {
  const results = new Array(items.length);
  let cursor = 0;
  async function consume() {
    while (cursor < items.length) {
      const index = cursor;
      cursor += 1;
      try {
        results[index] = { status: "fulfilled", value: await worker(items[index], index), item: items[index] };
      } catch (reason) {
        results[index] = { status: "rejected", reason, item: items[index] };
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(Math.max(1, concurrency), items.length) }, consume));
  return results;
}
