from __future__ import annotations

from datetime import date, timedelta

from django.db.models import Avg, Count, Q
from django.utils import timezone

from journal.models import JournalEntry, WatchHistoryRecord


class AnalyticsRangeError(ValueError):
    pass


def _parse_date(value, label):
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise AnalyticsRangeError(f"{label} 必须是 YYYY-MM-DD 日期。") from error


def parse_analytics_range(*, start=None, end=None):
    start_date = _parse_date(start, "start")
    end_date = _parse_date(end, "end")
    if start_date is not None and end_date is not None and start_date > end_date:
        raise AnalyticsRangeError("start 不能晚于 end。")
    return start_date, end_date


def _history_range(queryset, start, end):
    if start is not None:
        queryset = queryset.filter(watched_on__gte=start)
    if end is not None:
        queryset = queryset.filter(watched_on__lte=end)
    return queryset


def build_user_analytics(*, user, start=None, end=None):
    start, end = parse_analytics_range(start=start, end=end)
    entries = JournalEntry.objects.filter(user=user, deleted_at__isnull=True)
    aggregate = entries.aggregate(
        total=Count("id"),
        average_score=Avg("personal_score"),
        shared=Count("id", filter=~Q(visibility=JournalEntry.Visibility.PRIVATE)),
    )
    raw_statuses = {
        row["watch_status"]: row["count"]
        for row in entries.values("watch_status").annotate(count=Count("id"))
    }
    status_distribution = {
        value: raw_statuses.get(value, 0)
        for value, _label in JournalEntry.WatchStatus.choices
    }
    score_distribution = [
        {"score": str(row["personal_score"]), "count": row["count"]}
        for row in entries.exclude(personal_score__isnull=True)
        .values("personal_score")
        .annotate(count=Count("id"))
        .order_by("personal_score")
    ]

    base_history = WatchHistoryRecord.objects.filter(
        entry__user=user,
        entry__deleted_at__isnull=True,
    )
    history = _history_range(
        base_history,
        start,
        end,
    )
    today = timezone.localdate()
    week_start = today - timedelta(days=6)
    month_start = today.replace(day=1)
    history_summary = history.aggregate(
        watch_history_count=Count("id"),
        active_days=Count("watched_on", distinct=True),
        today_count=Count("id", filter=Q(watched_on=today)),
        seven_day_count=Count("id", filter=Q(watched_on__gte=week_start, watched_on__lte=today)),
        month_count=Count("id", filter=Q(watched_on__gte=month_start, watched_on__lte=today)),
    )
    monthly_activity = [
        {
            "month": f'{row["watched_on__year"]:04d}-{row["watched_on__month"]:02d}',
            "count": row["count"],
        }
        for row in history.values("watched_on__year", "watched_on__month")
        .annotate(count=Count("id"))
        .order_by("watched_on__year", "watched_on__month")
    ]

    average = aggregate["average_score"]
    recent_activity = [
        {
            "id": record.pk,
            "entry_id": record.entry_id,
            "title": record.entry.title,
            "watched_on": record.watched_on.isoformat(),
            "watched_label": record.watched_label,
            "brush_number": record.brush_number,
            "brush_label": record.brush_label,
            "episode_start": record.episode_start,
            "episode_end": record.episode_end,
            "notes": record.notes,
        }
        for record in history.select_related("entry").order_by("-watched_on", "-sequence", "-id")[:10]
    ]
    return {
        "summary": {
            "total": aggregate["total"],
            "average_score": round(float(average), 2) if average is not None else None,
            "shared": aggregate["shared"],
            "watch_history_count": history_summary["watch_history_count"],
            "active_days": history_summary["active_days"],
        },
        "status_distribution": status_distribution,
        "score_distribution": score_distribution,
        "monthly_activity": monthly_activity,
        "activity_summary": {
            "today": history_summary["today_count"],
            "last_7_days": history_summary["seven_day_count"],
            "current_month": history_summary["month_count"],
        },
        "recent_activity": recent_activity,
        "range": {
            "start": start.isoformat() if start is not None else None,
            "end": end.isoformat() if end is not None else None,
            "boundaries": "inclusive",
            "timezone": timezone.get_current_timezone_name(),
        },
        "generated_at": timezone.now().isoformat(),
    }
