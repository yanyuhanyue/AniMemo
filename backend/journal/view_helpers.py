from django.contrib.auth import get_user_model

from .models import JournalEntry
from .staff_services import can_manage_user, get_security_profile, get_staff_role


User = get_user_model()


def _validation_detail(error):
    return error.messages[0] if getattr(error, "messages", None) else str(error)


def build_public_stats(summary_records):
    scored = [record for record in summary_records if record["personal_score"] is not None]
    average_score = sum(float(record["personal_score"]) for record in scored) / len(scored) if scored else 0

    def has_tag(record, tag):
        return tag in (record.get("tags") or [])

    return {
        "total": len(summary_records),
        "completed_count": sum(record["watch_status"] == JournalEntry.WatchStatus.COMPLETED for record in summary_records),
        "average_score": round(average_score, 2),
        "movie_count": sum(has_tag(record, "剧场版") for record in summary_records),
        "ova_count": sum(has_tag(record, "OVA") or "OVA" in record["title"].upper() for record in summary_records),
        "short_count": sum(has_tag(record, "泡面番") for record in summary_records),
        "masterpiece_count": sum(
            record["personal_score"] is not None and float(record["personal_score"]) >= 9.5
            for record in summary_records
        ),
        "pending_count": sum(
            record["watch_status"] == JournalEntry.WatchStatus.PLANNED
            or record["personal_score"] is None
            or "待定" in (record["airing_period"] or "")
            or record["airing_period"] == "未定档"
            for record in summary_records
        ),
    }


def build_staff_user_data(user, settings_obj=None, actor=None):
    security = get_security_profile(user)
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "is_active": user.is_active,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
        "staff_role": get_staff_role(user) if user.is_staff else "user",
        "email_verified": security.email_verified,
        "two_factor_enabled": security.two_factor_enabled,
        "last_login": user.last_login,
        "date_joined": user.date_joined,
        "entry_count": user.journal_entries.count(),
        "column_count": user.columns.count(),
        "nickname": settings_obj.nickname if settings_obj else "",
        "can_manage": can_manage_user(actor, user) if actor is not None else False,
    }

