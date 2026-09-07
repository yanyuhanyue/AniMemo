"""Read-only projections for public discovery, separate from owner mutations."""

from .serializers_entries import JournalEntrySerializer


class PublicCatalogEntrySerializer(JournalEntrySerializer):
    class Meta(JournalEntrySerializer.Meta):
        fields = (
            "id", "title", "japanese_title", "airing_period", "studio", "episodes",
            "description", "poster_url", "poster", "baike_url", "tags",
        )
        read_only_fields = fields


class PublicHomepageEntrySerializer(JournalEntrySerializer):
    class Meta(JournalEntrySerializer.Meta):
        fields = (
            *PublicCatalogEntrySerializer.Meta.fields,
            "tag_colors", "personal_score", "watch_status", "watch_status_display",
            "review", "visibility", "watch_history_count", "created_at", "updated_at",
        )
        read_only_fields = fields
