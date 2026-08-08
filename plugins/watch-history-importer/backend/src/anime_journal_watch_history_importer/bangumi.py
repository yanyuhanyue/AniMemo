from __future__ import annotations

from difflib import SequenceMatcher
import re

import requests
from django.conf import settings

from .parser import EPISODE_MISMATCH_EXCEPTION_TITLES, normalize_title


SEARCH_URL = "https://api.bgm.tv/v0/search/subjects"
SEARCH_ALIASES = {
    normalize_title("无职"): "无职转生",
    normalize_title("无职转生到了异世界就拿出真本事"): "无职转生 ～到了异世界就拿出真本事～",
    normalize_title("末日三问"): "末日时在做什么？有没有空？可以来拯救吗？",
    normalize_title("地错"): "在地下城寻求邂逅是否搞错了什么",
    normalize_title("叹息的亡灵好想隐退"): "叹气的亡灵想隐退",
}
CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
UNICODE_ROMAN_NUMBERS = {
    "Ⅰ": 1,
    "Ⅱ": 2,
    "Ⅲ": 3,
    "Ⅳ": 4,
    "Ⅴ": 5,
    "Ⅵ": 6,
    "Ⅶ": 7,
    "Ⅷ": 8,
    "Ⅸ": 9,
    "Ⅹ": 10,
}
LATIN_ROMAN_NUMBERS = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}
SEASON_NUMBER = r"[0-9零〇一二两三四五六七八九十百]+"
SEASON_PATTERNS = (
    re.compile(rf"第\s*(?P<number>{SEASON_NUMBER})\s*(?:部分|クール|季|期|部)", re.IGNORECASE),
    re.compile(rf"(?P<number>{SEASON_NUMBER})\s*(?:季|期|部分|部|クール)", re.IGNORECASE),
    re.compile(r"(?P<number>\d+)(?:st|nd|rd|th)?\s*(?:season|part|cour)\b", re.IGNORECASE),
    re.compile(r"\b(?:season|part|cour)\s*(?P<number>\d+)\b", re.IGNORECASE),
)
UNICODE_ROMAN_SUFFIX_RE = re.compile(r"(?P<roman>[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ])\s*$")
LATIN_ROMAN_SUFFIX_RE = re.compile(r"(?:\s|[-:：])(?P<roman>I{1,3}|IV|V|VI{0,3}|IX|X)\s*$", re.IGNORECASE)
SPECIAL_TITLE_RE = re.compile(
    r"(?:(?<![a-z0-9])(?:ova|oad|special|movie)(?![a-z0-9])|剧场版|劇場版|映画|总集篇|總集篇|総集編|特别篇|特別篇|番外篇)",
    re.IGNORECASE,
)


class BangumiError(RuntimeError):
    pass


def _number_value(value):
    value = str(value or "").strip()
    if value.isdigit():
        return int(value)
    if value in CN_DIGITS:
        return CN_DIGITS[value]
    if value == "十":
        return 10
    if value == "百":
        return 100
    if "百" in value:
        left, right = value.split("百", 1)
        return CN_DIGITS.get(left, 1) * 100 + _number_value(right)
    if "十" in value:
        left, right = value.split("十", 1)
        return CN_DIGITS.get(left, 1) * 10 + CN_DIGITS.get(right, 0)
    return 0


def _split_season(title):
    raw = str(title or "").strip()
    for pattern in SEASON_PATTERNS:
        matches = list(pattern.finditer(raw))
        if not matches:
            continue
        match = matches[-1]
        season = _number_value(match.group("number"))
        if season:
            return f"{raw[:match.start()]} {raw[match.end():]}".strip(), season

    match = UNICODE_ROMAN_SUFFIX_RE.search(raw)
    if match:
        return raw[:match.start()].strip(), UNICODE_ROMAN_NUMBERS[match.group("roman")]
    match = LATIN_ROMAN_SUFFIX_RE.search(raw)
    if match:
        return raw[:match.start()].strip(), LATIN_ROMAN_NUMBERS[match.group("roman").upper()]
    return raw, None


def _canonical_title_parts(title):
    base, season = _split_season(title)
    normalized_base = normalize_title(base)
    canonical = SEARCH_ALIASES.get(normalized_base, base)
    return normalize_title(canonical), season


def _is_special_title(names):
    return any(SPECIAL_TITLE_RE.search(str(name or "")) for name in names)


def _search_queries(title):
    base, season = _split_season(title)
    query_base = SEARCH_ALIASES.get(normalize_title(base), base).strip()
    if not query_base:
        query_base = str(title or "").strip()
    queries = [query_base]
    if season and season > 1:
        queries.insert(0, f"{query_base} 第{season}季")
    return list(dict.fromkeys(query for query in queries if query))


def _headers():
    return {
        "User-Agent": getattr(
            settings,
            "BANGUMI_USER_AGENT",
            "AnimeJournal/1.0 watch-history-importer",
        ),
        "Accept": "application/json",
    }


def _text_values(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        values = []
        for item in value:
            values.extend(_text_values(item))
        return values
    if isinstance(value, dict):
        return _text_values(value.get("v") or value.get("value") or value.get("name"))
    return []


def _studio(item):
    preferred = {"动画制作", "動畫製作", "アニメーション制作"}
    for field in item.get("infobox") or []:
        if not isinstance(field, dict) or str(field.get("key") or "").strip() not in preferred:
            continue
        values = [value.strip() for value in _text_values(field.get("value")) if value.strip()]
        if values:
            return " / ".join(dict.fromkeys(values))
    return ""


def _person_studios(persons):
    accepted = {"动画制作", "動畫製作", "アニメーション制作"}
    studios = []
    for person in persons or []:
        if not isinstance(person, dict):
            continue
        relation = str(person.get("relation") or "").strip()
        name = str(person.get("name") or "").strip()
        if relation in accepted and name and name not in studios:
            studios.append(name)
    return " / ".join(studios)


def _tags(item):
    names = []
    for tag in item.get("tags") or []:
        name = str(tag.get("name") if isinstance(tag, dict) else tag).strip()
        if name and name not in names:
            names.append(name)
        if len(names) == 8:
            break
    return names


def _episodes(item):
    value = item.get("eps") or item.get("total_episodes")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def search_subject(title):
    merged = []
    seen_ids = set()
    last_error = None
    for query in _search_queries(title):
        try:
            response = requests.post(
                SEARCH_URL,
                params={"limit": 12, "offset": 0},
                json={"keyword": query, "sort": "match", "filter": {"type": [2]}},
                headers={**_headers(), "Content-Type": "application/json"},
                timeout=(4, 12),
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            last_error = error
            continue
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            last_error = BangumiError("Bangumi 返回了无法识别的数据。")
            continue
        for item in items:
            item_id = item.get("id") if isinstance(item, dict) else None
            key = item_id if item_id is not None else repr(item)
            if key in seen_ids:
                continue
            seen_ids.add(key)
            merged.append(item)
    if not merged and last_error:
        raise BangumiError(str(last_error)) from last_error
    return merged


def choose_subject(source_title, items):
    source, requested_season = _canonical_title_parts(source_title)
    ranked = []
    for item in items:
        names = [item.get("name_cn") or "", item.get("name") or ""]
        names = [name for name in names if name]
        if not names:
            continue
        title_parts = [_canonical_title_parts(name) for name in names]
        candidate_seasons = {season for _base, season in title_parts if season is not None}
        if requested_season is not None:
            if candidate_seasons:
                if requested_season not in candidate_seasons:
                    continue
            elif requested_season != 1 or _is_special_title(names):
                continue
        normalized_names = [base for base, _season in title_parts]
        score = max(SequenceMatcher(None, source, name).ratio() for name in normalized_names)
        if source in normalized_names:
            score = 1.0
        elif any(source in name or name in source for name in normalized_names):
            score = max(score, 0.88)
        ranked.append((score, item))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    if not ranked:
        return None, "season_mismatch" if requested_season is not None else "no_result", 0.0
    score, item = ranked[0]
    second = ranked[1][0] if len(ranked) > 1 else 0.0
    if score < 0.72:
        status = "low_confidence"
    elif second >= score - 0.03 and score < 1:
        status = "ambiguous"
    else:
        status = "matched"
    return item, status, round(score, 4)


def fetch_subject(subject_id):
    try:
        response = requests.get(
            f"https://api.bgm.tv/v0/subjects/{int(subject_id)}",
            headers=_headers(),
            timeout=(4, 12),
        )
        response.raise_for_status()
        item = response.json()
    except (requests.RequestException, ValueError, TypeError) as error:
        raise BangumiError(str(error)) from error
    if not isinstance(item, dict) or not item.get("id"):
        raise BangumiError("Bangumi 条目不存在。")
    return item


def fetch_person_studio(subject_id):
    try:
        response = requests.get(
            f"https://api.bgm.tv/v0/subjects/{int(subject_id)}/persons",
            headers=_headers(),
            timeout=(4, 12),
        )
        response.raise_for_status()
        persons = response.json()
    except (requests.RequestException, ValueError, TypeError):
        return ""
    return _person_studios(persons if isinstance(persons, list) else [])


def resolve_group(group):
    source_title = group["source_title"]
    item, status, confidence = choose_subject(source_title, search_subject(source_title))
    if not item:
        return {"status": status, "confidence": confidence, "source_title": source_title}
    subject = fetch_subject(item.get("id"))
    studio = fetch_person_studio(subject["id"]) or _studio(subject)
    return resolve_group_from_item(group, subject, confidence=confidence, initial_status=status, studio=studio)


def resolve_subject_id(group, subject_id):
    item = fetch_subject(subject_id)
    studio = fetch_person_studio(item["id"]) or _studio(item)
    resolution = resolve_group_from_item(group, item, confidence=1.0, studio=studio)
    resolution["manual_selection"] = True
    return resolution


def resolve_group_from_item(group, item, confidence=1.0, initial_status="matched", studio=None):
    source_title = group["source_title"]
    status = initial_status
    episodes = _episodes(item)
    claims = {record.get("claimed_episodes") for record in group.get("records", []) if record.get("claimed_episodes") is not None}
    mismatch = bool(claims and episodes is not None and any(claim != episodes for claim in claims))
    exception = normalize_title(source_title) in EPISODE_MISMATCH_EXCEPTION_TITLES
    if status == "matched" and mismatch and not exception:
        status = "episode_mismatch"
    images = item.get("images") or {}
    return {
        "status": "matched" if exception and status == "episode_mismatch" else status,
        "episode_exception": exception,
        "confidence": confidence,
        "bangumi_id": item.get("id"),
        "title": item.get("name_cn") or item.get("name") or source_title,
        "japanese_title": item.get("name") or "",
        "air_date": item.get("date") or "",
        "episodes": episodes,
        "studio": _studio(item) if studio is None else studio,
        "description": item.get("summary") or "",
        "poster_url": images.get("large") or images.get("common") or images.get("medium") or "",
        "tags": _tags(item),
        "source_url": f"https://bgm.tv/subject/{item.get('id')}" if item.get("id") else "",
        "claimed_episodes": sorted(claims),
    }
