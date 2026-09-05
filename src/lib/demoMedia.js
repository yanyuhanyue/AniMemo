import {
  ANIMEMO_AVATAR_PATH,
  ANIMEMO_POSTER_FALLBACK_PATH,
  normalizeBundledPosterPath,
} from "./mediaAssets.js";
import { validateTrustedPosterUrl } from "./posterSources.js";

function readBangumiIdentity(record) {
  const identity = record?.resourceIdentity || record?.resource_identity;
  const provider = String(identity?.provider || "").trim().toLowerCase();
  const externalId = String(
    identity?.externalId || identity?.external_id || "",
  ).trim();
  if (provider !== "bangumi" || !/^\d+$/.test(externalId)) return null;
  return { provider, externalId };
}

function providerPoster(record, payload) {
  const identity = readBangumiIdentity(record);
  if (
    !identity
    || payload?.provider !== "bangumi"
    || String(payload?.external_id || "") !== identity.externalId
  ) return "";
  const expectedJapaneseTitle = String(record?.bangumiJapaneseTitle || "").trim();
  if (
    expectedJapaneseTitle
    && String(payload?.japanese_title || payload?.name || "").trim() !== expectedJapaneseTitle
  ) return "";
  const poster = String(payload?.poster_url || "").trim();
  return poster && !validateTrustedPosterUrl(poster) ? poster : "";
}

async function fetchBangumiSubject(record, client, cache) {
  const identity = readBangumiIdentity(record);
  if (!identity) return null;
  const key = `${identity.provider}:${identity.externalId}`;
  if (!cache.has(key)) {
    cache.set(key, client.get(
      `external-media/providers/bangumi/subjects/${encodeURIComponent(identity.externalId)}/`,
      { timeout: 4000 },
    ).then((response) => response?.data || null).catch(() => null));
  }
  return cache.get(key);
}

async function mapWithConcurrency(items, limit, callback) {
  const results = new Array(items.length);
  let cursor = 0;
  const workers = Array.from(
    { length: Math.min(Math.max(1, limit), items.length || 1) },
    async () => {
      while (cursor < items.length) {
        const index = cursor;
        cursor += 1;
        results[index] = await callback(items[index], index);
      }
    },
  );
  await Promise.all(workers);
  return results;
}

export function reconcileDemoRecords(records, catalog) {
  const byId = new Map((catalog || []).map((record) => [String(record.id), record]));
  return (records || []).map((record) => {
    const canonical = byId.get(String(record?.id));
    if (!canonical) return record;
    return {
      ...record,
      title: canonical.title,
      japaneseTitle: canonical.japaneseTitle,
      resourceIdentity: canonical.resourceIdentity,
      bangumiTitle: canonical.bangumiTitle,
      bangumiJapaneseTitle: canonical.bangumiJapaneseTitle,
      poster: normalizeBundledPosterPath(canonical.poster),
      posterOriginal: "",
      posterUrl: "",
      posterSource: "none",
      externalIdentities: canonical.externalIdentities || [],
      externalUrl: canonical.externalUrl || canonical.baikeUrl || "",
      externalSource: canonical.externalSource || "Bangumi",
      baikeUrl: canonical.baikeUrl || "",
    };
  });
}

export async function hydrateDemoAnimeRecords(
  records,
  {
    client = null,
    cache = new Map(),
    concurrency = 4,
  } = {},
) {
  if (!client || typeof client.get !== "function") {
    return (records || []).map((record) => ({
      ...record,
      poster: normalizeBundledPosterPath(record?.poster),
      posterOriginal: "",
      posterUrl: "",
      posterSource: "none",
    }));
  }
  return mapWithConcurrency(records || [], concurrency, async (record) => {
    const payload = await fetchBangumiSubject(record, client, cache);
    const poster = providerPoster(record, payload);
    if (!poster) {
      return {
        ...record,
        poster: normalizeBundledPosterPath(record?.poster),
        posterOriginal: "",
        posterUrl: "",
        posterSource: "none",
      };
    }
    return {
      ...record,
      poster: normalizeBundledPosterPath(poster),
      posterOriginal: poster,
      posterUrl: poster,
      posterSource: "default_url",
      externalIdentities: [{
        provider: "bangumi",
        external_id: String(payload.external_id),
        canonical_url: String(payload.canonical_url || ""),
        metadata: payload,
      }],
    };
  });
}

export async function hydrateDemoFeaturedColumns(columns, options) {
  const sharedOptions = { ...options, cache: options?.cache || new Map() };
  const anime = await hydrateDemoAnimeRecords(
    (columns || []).map((column) => column.anime || {}),
    sharedOptions,
  );
  const related = await Promise.all((columns || []).map((column) => (
    hydrateDemoAnimeRecords(column.relatedAnime || [], sharedOptions)
  )));
  return (columns || []).map((column, index) => ({
    ...column,
    authorAvatar: ANIMEMO_AVATAR_PATH,
    cover: anime[index]?.poster || ANIMEMO_POSTER_FALLBACK_PATH,
    anime: anime[index],
    relatedAnime: related[index],
  }));
}

export async function hydrateDemoUniverseOwners(owners, options) {
  const sharedOptions = { ...options, cache: options?.cache || new Map() };
  return Promise.all((owners || []).map(async (owner) => {
    const records = await hydrateDemoAnimeRecords(
      owner.records || [],
      sharedOptions,
    );
    const byId = new Map(records.map((record) => [String(record.id), record]));
    const topPicks = (owner.top_picks || [])
      .map((record) => byId.get(String(record.id)))
      .filter(Boolean);
    return {
      ...owner,
      avatar: ANIMEMO_AVATAR_PATH,
      records,
      top_picks: topPicks,
    };
  }));
}

export async function hydrateDemoUniverseOwner(owner, options) {
  if (!owner) return null;
  return (await hydrateDemoUniverseOwners([owner], options))[0] || null;
}
