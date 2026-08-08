from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date


DATE_RE = re.compile(
    r"^\s*(?P<month>\d{1,2})月(?P<day>\d{1,2})(?:日|号)?"
    r"(?P<range>\s*(?:-|–|—|至)\s*(?:(?P<end_month>\d{1,2})月)?(?P<end_day>\d{1,2})(?:日|号)?)?"
)
BRUSH_RE = re.compile(r"^\s*(?P<label>(?:首|二|三|四|五|六|七|八|九|十|\d+|x|X)刷)\s*")
EPISODE_PATTERNS = (
    (re.compile(r"第\s*(\d+)\s*集\s*(?:到|至|[-~～])\s*第?\s*(\d+)\s*集"), "range"),
    (re.compile(r"第\s*(\d+)\s*话\s*(?:到|至|[-~～])\s*第?\s*(\d+)\s*话"), "range"),
    (re.compile(r"第\s*(\d+)\s*[-~～至]\s*(\d+)\s*[集话]"), "range"),
    (re.compile(r"(?<!\d)(\d+)\s*[-~～至]\s*(\d+)\s*[集话]"), "range"),
    (re.compile(r"共\s*([0-9一二三四五六七八九十]+)\s*[集话]"), "total"),
    (re.compile(r"第\s*(\d+)\s*[集话]"), "single"),
    (re.compile(r"(?<!\d)(\d+)\s*集\s*$"), "total"),
)
SKIP_RE = re.compile(r"断更|未看完|暂弃|暂无兴趣|暂时不喜欢|后期补上|等放假后再补|暂停更新|没看|没看完|只看了?一集", re.IGNORECASE)
IGNORED_SECTION_RE = re.compile(r"^(?:未看完篇|PS\s*[:：]|年度最佳番)")
TIME_OF_DAY_RE = re.compile(r"^(?:凌晨|上午|中午|下午|傍晚|晚上?|早上?|早|午)(?:\s+|$)")
BRUSH_WORDS = {"首": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
CN_NUMBERS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
EPISODE_MISMATCH_EXCEPTION_TITLES = {"终物语上"}


@dataclass
class DateContext:
    label: str
    end: date


@dataclass
class Candidate:
    title: str
    source_title: str
    watch_date: str | None
    watch_date_label: str | None
    brush: int | None
    brush_label: str
    notes: list[str] = field(default_factory=list)
    episode_range: dict[str, int] | None = None
    claimed_episodes: int | None = None
    episode_claim_kind: str | None = None
    include: bool = True
    exclusion_reason: str | None = None
    source_file: str = ""
    source_line: int = 0

    @property
    def key(self):
        return f"{normalize_title(self.title)}::{self.brush_label}"


def normalize_title(title):
    return re.sub(r"[\s《》「」『』：:，。,、.!！?？·'\"（）()\[\]【】～~-]", "", str(title or "")).casefold()


def episode_number(value):
    if value.isdigit():
        return int(value)
    if value in CN_NUMBERS:
        return CN_NUMBERS[value]
    if value.startswith("十"):
        return 10 + CN_NUMBERS.get(value[1:], 0)
    if value.endswith("十"):
        return CN_NUMBERS.get(value[:-1], 0) * 10
    if "十" in value:
        left, right = value.split("十", 1)
        return CN_NUMBERS.get(left, 1) * 10 + CN_NUMBERS.get(right, 0)
    return 0


def parse_date_line(year, line):
    match = DATE_RE.match(line)
    if not match:
        return None, line.strip()
    month = int(match.group("month"))
    day = int(match.group("day"))
    end_month = int(match.group("end_month")) if match.group("end_month") else month
    end_day = int(match.group("end_day")) if match.group("end_day") else day
    try:
        end = date(year, end_month, end_day)
    except ValueError:
        return None, line.strip()
    return DateContext(line[: match.end()].strip(), end), line[match.end() :].strip()


def extract_brush(text):
    match = BRUSH_RE.match(text)
    if not match:
        return 1, "首刷", text.strip()
    label = match.group("label").lower().replace("x", "X")
    raw = label[:-1]
    brush = int(raw) if raw.isdigit() else None if raw == "X" else BRUSH_WORDS.get(raw, 1)
    return brush, label, text[match.end() :].strip()


def extract_episodes(text):
    for pattern, kind in EPISODE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        values = [episode_number(value) for value in match.groups() if value is not None]
        if len(values) == 1:
            episode_range = {"start": values[0], "end": values[0]}
            claimed = values[0] if kind == "total" else None
        else:
            episode_range = {"start": values[0], "end": values[1]}
            claimed = None
        return text[: match.start()] + text[match.end() :], episode_range, claimed, kind
    return text, None, None, None


def split_notes(text):
    notes = []

    def consume(match):
        value = match.group(1).strip()
        if not value:
            return ""
        notes.append(value)
        return ""

    text = re.sub(r"（([^（）]*)）", consume, text)
    text = re.sub(r"\(([^()]*)\)", consume, text)
    text = re.sub(r"[（(]\s*[）)]", "", text)
    if "--" in text:
        text, note = text.split("--", 1)
        if note.strip():
            notes.append(note.strip())
    return text.strip(" 《》。\t"), notes


def parse_document(filename, content):
    year_match = re.search(r"(20\d{2})", filename)
    if not year_match:
        return []
    year = int(year_match.group(1))
    current = None
    ignored_section = False
    candidates = []
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("忆往昔"):
            continue
        if IGNORED_SECTION_RE.match(line):
            ignored_section = True
            continue
        if ignored_section:
            continue
        context, remainder = parse_date_line(year, line)
        if context:
            current = context
            line = remainder
        line = TIME_OF_DAY_RE.sub("", line).strip()
        if not line:
            continue
        if line.startswith("（") and line.endswith("）"):
            continue
        if SKIP_RE.search(line):
            continue
        brush, brush_label, body = extract_brush(line)
        body, episode_range, claimed_episodes, claim_kind = extract_episodes(body)
        body, notes = split_notes(body)
        body = re.sub(r"\s+", " ", body).strip(" -—:：,，。\t")
        if len(body) < 2:
            continue
        missing_date = current is None or "忘了啥时候看的" in " ".join(notes)
        candidates.append(
            Candidate(
                title=body,
                source_title=raw_line.strip(),
                watch_date=current.end.isoformat() if current and not missing_date else None,
                watch_date_label=f"{year}年{current.label}" if current and not missing_date else None,
                brush=brush,
                brush_label=brush_label,
                notes=notes,
                episode_range=episode_range,
                claimed_episodes=claimed_episodes,
                episode_claim_kind=claim_kind,
                include=not missing_date,
                exclusion_reason="观看日期缺失，按规则丢弃" if missing_date else None,
                source_file=filename,
                source_line=line_number,
            )
        )
    return candidates


def build_preview(documents):
    candidates = []
    for filename, content in documents:
        candidates.extend(parse_document(filename, content))
    latest = {}
    for candidate in candidates:
        if not candidate.include:
            continue
        old = latest.get(candidate.key)
        if old is None or (candidate.watch_date or "") >= (old.watch_date or ""):
            latest[candidate.key] = candidate
    for candidate in candidates:
        if candidate.include and latest.get(candidate.key) is not candidate:
            candidate.include = False
            candidate.exclusion_reason = "同一番剧同一刷次保留最新记录"

    included = [candidate for candidate in candidates if candidate.include]
    groups = {}
    for candidate in included:
        groups.setdefault(normalize_title(candidate.title), []).append(candidate)
    anime_groups = []
    for key, records in groups.items():
        records.sort(key=lambda item: (item.watch_date or "", item.brush or 999))
        latest_record = records[-1]
        anime_groups.append(
            {
                "source_key": key,
                "source_title": latest_record.title,
                "latest_watch_date": latest_record.watch_date,
                "latest_watch_date_label": latest_record.watch_date_label,
                "records": [asdict(record) for record in records],
                "resolution": {"status": "pending"},
            }
        )
    anime_groups.sort(key=lambda item: item["latest_watch_date"] or "", reverse=True)
    excluded = [asdict(candidate) for candidate in candidates if not candidate.include]
    return {
        "groups": anime_groups,
        "excluded": excluded,
        "summary": {
            "parsed": len(candidates),
            "included_records": len(included),
            "anime_groups": len(anime_groups),
            "excluded": len(excluded),
            "pending": len(anime_groups),
            "matched": 0,
            "manual_review": 0,
        },
    }
