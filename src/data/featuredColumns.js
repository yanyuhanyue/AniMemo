import { animeRecords } from "./anime.js";

const articleBodies = [
  [
    "有些作品的重启不是把过去再演一遍，而是让曾经被略过的人重新获得一次选择。《回复术士的重启人生》最值得讨论的，正是它怎样把创伤、权力与重新开始绑在一起。",
    "这篇记录不回避作品引发的争议，也不急着替它寻找统一答案。我们沿着角色动机、世界规则与观众的不适感逐层拆开，看看一部极端作品为什么仍会留下如此鲜明的观看记忆。",
    "评分只是入口。真正决定它是否值得被记住的，是看完之后仍然需要花时间整理的复杂情绪。",
  ],
  [
    "蓝色月光、玻璃般的夜景和不安定的亲密关系，让《Happy Sugar Life》拥有一种无法被普通悬疑标签概括的质感。",
    "它把幸福塑造成一间人为封闭的房间：越想保护，越会暴露出控制与恐惧。角色不断越界，却又在自己的逻辑里保持惊人的一致。",
    "回看这部作品时，比剧情反转更难忘的，是那些甜美色彩和危险选择同时出现的瞬间。",
  ],
  [
    "轻百并不是把关系写浅。恰恰相反，优秀的日常作品会把靠近、退让和默契藏进一次递东西、一次并肩回家，或一句没有说完的话里。",
    "这里选出的几部作品使用了完全不同的节奏：有的靠喜剧推进，有的让风景承担情绪，有的则把成长埋在每周重复的小事中。",
    "当观众愿意继续陪角色过普通的一天，关系本身就已经成立。",
  ],
  [
    "《葬送的芙莉莲》最特别的地方，不是长生种看待时间的设定，而是镜头真的愿意为时间停下来。",
    "旅程中的空镜、短暂的沉默和迟到许多年的理解，共同构成了作品的情绪结构。它不把怀念写成闪回合集，而是让过去持续改变现在的选择。",
    "这也是为什么一段很短的相处，可以在几十年后仍然拥有重量。",
  ],
  [
    "一季动画结束后，真正留下来的往往不是榜单名次，而是某个周末晚上准时等待更新的习惯。",
    "这份季末记录从表演、演出、音乐和陪伴感四个角度回看六部作品。高分作品当然重要，但那些并不完美、却刚好出现在合适时刻的动画也值得被写下来。",
    "季度总结不是结论，而是一张保留当时心情的时间票根。",
  ],
  [
    "音乐动画的魅力不只在演奏场面。排练时的犹豫、成员之间不同步的呼吸，以及登台前最后一次确认眼神，都会让一首歌拥有叙事。",
    "我们把舞台拆成声音、剪辑、色彩和角色关系四条轨道，观察它们如何在高潮处汇合。",
    "当音乐真正响起时，观众听见的也包括角色一路没有说出口的话。",
  ],
  [
    "治愈不是没有冲突，而是作品愿意给冲突留出被理解和消化的时间。",
    "从料理、旅行到社团日常，这些动画用稳定的生活节奏重新校准观众的呼吸。它们不承诺问题会立即消失，却让人相信明天仍然可以好好吃饭、出门和见面。",
    "温柔并不轻，它需要持续而准确的观察。",
  ],
  [
    "有些新作仍在等待完整评价。把分数暂时留空，不是逃避判断，而是承认观看体验仍在变化。",
    "这篇观察笔记记录前三集的角色建立、视觉语言和叙事风险，并明确区分已经成立的优点与仍需等待的部分。",
    "待定也是一种有效状态，它提醒我们不要把第一印象误当成最终答案。",
  ],
];

const source = [
  ["restart-life", 2, "《回复术士的重启人生》", "回復術士のやり直し", "烧人棍这一次", "双叶夏洛", "2021-1", 8.9, ["异世界", "奇幻", "黑暗", "后宫"]],
  ["happy-sugar-life", 6, "《Happy Sugar Life 砂糖的幸福生活》", "ハッピーシュガーライフ", "甜蜜与危险共享同一种颜色", "AniMemo", "2018-7", 9.0, ["真百", "悬疑", "黑暗", "入坑之作", "独立推荐"]],
  ["yuri-spectrum", 11, "百合动画入门：从轻松日常到浓烈情感", "百合作品の感情スペクトル", "八部作品，八种靠近彼此的方式", "AniMemo", "2026-4", 9.4, ["真百", "轻百", "日常", "独立推荐"]],
  ["frieren-time", 5, "为什么《葬送的芙莉莲》如此擅长表现时间", "葬送のフリーレン・時間論", "从留白、旅程与迟到的理解开始", "NorthStar", "2026-1", 9.5, ["奇幻", "冒险", "演出分析"]],
  ["season-afterglow", 16, "这一季最舍不得完结的六部动画", "季節の余韻を集めて", "不只看分数，也记录陪伴留下的余温", "Mochi", "2025-10", 9.8, ["日常", "治愈", "季度总结"]],
  ["music-fireworks", 6, "当音乐响起：动画舞台上的情绪烟花", "音楽が物語になる瞬間", "声音、剪辑与角色关系的同步抵达", "Hikari", "2026-1", 9.9, ["音乐", "原创", "剧场版", "演出分析"]],
  ["healing-rhythm", 13, "治愈系动画如何重新校准我们的呼吸", "癒やしのリズム", "温柔不是停滞，而是给情绪足够时间", "Mochi", "2025-4", 10.0, ["治愈", "日常", "旅行"]],
  ["three-episode-watch", 9, "前三集观察室：先不急着给新番下结论", "三話までの観測記録", "保留判断，也认真记录已经发生的变化", "NorthStar", "2026-7", null, ["新番", "观察中", "待定"]],
];

const featuredAnimeOverrides = {
  "restart-life": {
    title: "《回复术士的重启人生》",
    japaneseTitle: "回復術士のやり直し",
    period: "2021-1",
    studio: "TNK",
    episodes: "12",
    score: 8.9,
    tags: ["异世界", "奇幻", "黑暗", "后宫"],
    description: "回复术士凯亚尔在被利用与夺走一切后，借由贤者之石让时间回到四年前。他保留了上一轮人生的记忆，并试图重新掌握自己的命运。",
    review: "作品把复仇、权力与创伤推到非常极端的位置。它并不适合所有观众，但鲜明的冲突和角色动机确实留下了强烈记忆。",
    baikeUrl: "https://mzh.moegirl.org.cn/回复术士的重启人生",
  },
  "happy-sugar-life": {
    title: "《Happy Sugar Life 砂糖的幸福生活》",
    japaneseTitle: "ハッピーシュガーライフ",
    period: "2018-7",
    studio: "Ezo'la",
    episodes: "12",
    score: 9.0,
    tags: ["真百", "悬疑", "黑暗", "入坑之作", "轩皇力推"],
    description: "松坂砂糖与神户盐共同生活在一间与外界隔绝的公寓里。为了守护自己认定的幸福，砂糖不断跨越道德与法律的边界。",
    review: "甜美视觉与危险关系的反差至今仍很有冲击力。角色的三观普遍偏离常态，却因此形成了一套完整而令人不安的叙事逻辑。",
    baikeUrl: "https://mzh.moegirl.org.cn/Happy_Sugar_Life",
  },
};

const representativeAnimeBySlug = {
  "yuri-spectrum": 11,
  "frieren-time": 5,
  "season-afterglow": 16,
  "music-fireworks": 6,
  "healing-rhythm": 13,
  "three-episode-watch": 9,
};

function buildFeaturedAnime(slug, posterNumber, cover) {
  const override = featuredAnimeOverrides[slug];
  const linkedId = representativeAnimeBySlug[slug] ?? posterNumber;
  const record = override || animeRecords.find((anime) => anime.id === linkedId) || {};
  return {
    title: record.title || "未命名番剧",
    japaneseTitle: record.japaneseTitle || "",
    poster: cover,
    posterOriginal: cover,
    externalUrl: record.baikeUrl || "",
    externalSource: "萌娘百科",
    period: record.period || "未定档",
    score: record.score ?? null,
    studio: record.studio || "待补充",
    episodeCount: record.episodes || "待定",
    tags: record.tags || [],
    summary: record.description || "暂无剧情简介。",
    personalReview: record.review || "暂未记录个人评价。",
  };
}

export const featuredColumns = source.map(([slug, posterNumber, title, japaneseTitle, summary, author, period, score, tags], index) => {
  const cover = `/assets/posters/poster-${String(posterNumber).padStart(2, "0")}.webp`;
  return {
    id: slug,
    slug,
    label: ["EDITOR'S PICK", "DEEP DIVE", "YURI FILE", "VISUAL ESSAY", "SEASON FILE", "SOUND TRACK", "WARM NOTES", "WATCH LOG"][index],
    title,
    japaneseTitle,
    summary,
    author,
    authorAvatar: index % 2 === 0 ? "/assets/avatar.png" : `/assets/posters/poster-${String(((posterNumber + 3) % 16) + 1).padStart(2, "0")}.webp`,
    cover,
    period,
    year: period.split("-")[0],
    status: index === 7 ? "watching" : "completed",
    statusLabel: index === 7 ? "在看" : "看过",
    score,
    tags,
    body: articleBodies[index],
    relatedAnime: animeRecords.slice(index % 8, (index % 8) + 3),
    anime: buildFeaturedAnime(slug, posterNumber, cover),
  };
});

export function getFeaturedColumn(columnId) {
  return featuredColumns.find((column) => column.slug === String(columnId) || String(column.id) === String(columnId));
}

export function normalizeFeaturedApiColumn(column, index = 0) {
  const fallback = featuredColumns[index % featuredColumns.length];
  const published = column.published_at || column.updated_at || column.created_at;
  const year = published ? String(new Date(published).getFullYear()) : fallback.year;
  return {
    ...fallback,
    id: column.slug || column.id,
    slug: column.slug || String(column.id),
    title: column.title || fallback.title,
    japaneseTitle: fallback.japaneseTitle,
    summary: column.summary || fallback.summary,
    author: column.author_name || fallback.author,
    cover: column.cover || fallback.cover,
    period: published ? `${year}-${String(new Date(published).getMonth() + 1).padStart(2, "0")}` : fallback.period,
    year,
    body: column.body ? column.body.split(/\n{2,}/).filter(Boolean) : fallback.body,
    anime: column.anime ? { ...fallback.anime, ...column.anime } : fallback.anime,
    apiBacked: true,
  };
}
